"""Worker 线程与 run 执行编排：克隆 → 加载规则 → 扫描 → 提交 → 落库。

对应服务化改造计划「runner 行」的展开：

- 全局唯一 Worker 线程消费 CoalescingQueue，逐 Trigger 执行 execute_run
- execute_run 复刻 Jenkinsfile 的本地化流程：克隆来源仓到
  work_dir/run-<id>/（blobs=True 全量浅克隆，等价
  ``git clone --depth=1 --no-checkout``），从本地克隆加载 Token 规则
  （token_rules.json，关键字→token_env 环境变量），scan_changed
  走本地化模式（支持增量 diff），影子模式只落
  would_submit 不提交，active 模式先过仓库配置门控（设置页维护，按
  仓库粒度，默认拒绝），再逐仓 resolve_token + submit_repo
  （复制 submit_all 的路由逻辑，换取 head_branch/noop/pr_existed 粒度）
- 日志缓冲落库（50 行/2 秒批量 append_log）+ 全链路 token 脱敏：
  localgit.clone 失败会把含 token 的 URL 包进 GitCodeError（Jenkins
  曾有 token 明文落构建日志的事故），所有落库文本必须先过 SecretMasker
- run 结束把解析好的 token_rules 存入 rules_cache 供 prsync 用；
  finally 阶段回写 last_run_at、回填 push 事件、清理 workdir
"""

import os
import shutil
import threading
import time
import traceback
from datetime import datetime, timezone

from translator_ci import common, localgit, rules, service

from webapp import rules_cache
from webapp.db import is_shadow

# 日志缓冲刷写阈值：满 50 行或距上次落库超过 2 秒
LOG_FLUSH_LINES = 50
LOG_FLUSH_INTERVAL = 2.0


def _utcnow():
    """当前时间的 UTC ISO8601 字符串（与落库时间戳口径一致）。"""
    return datetime.now(timezone.utc).isoformat()


class SecretMasker:
    """token 脱敏器：把集合中每个非空秘密值替换为 ``***``。

    脱敏集合动态包含：默认 token（GITCODE_TOKEN）+ 每条已加载规则的
    解析后 token + TC_ADMIN_TOKEN / TC_WEBHOOK_TOKEN / TC_DASH_TOKEN。
    规则 token 在每次 run 加载规则后才能确定，故支持 add_secrets 运行
    时追加。线程安全（Worker 写、log 回调读）。
    """

    def __init__(self, secrets=()):
        self._lock = threading.Lock()
        self._secrets = []
        self.add_secrets(secrets)

    def add_secrets(self, secrets):
        """追加秘密值（空值忽略）；长的先替换，避免短前缀先命中漏掉长值。"""
        with self._lock:
            for s in secrets or []:
                s = (s or "").strip()
                if s and s not in self._secrets:
                    self._secrets.append(s)
            self._secrets.sort(key=len, reverse=True)

    def mask(self, text):
        """返回脱敏后的文本（即计划文档中的 _mask()）。"""
        if not text:
            return text
        with self._lock:
            secrets = list(self._secrets)
        for s in secrets:
            text = text.replace(s, "***")
        return text


class LogBuffer:
    """引擎 log 回调：逐行收集，满 50 行或超过 2 秒批量 append_log 落库。

    每行先过脱敏器再缓冲；run 结束必须调用 flush() 强制刷库。
    """

    def __init__(self, db, run_id, masker):
        self._db = db
        self._run_id = run_id
        self._masker = masker
        self._lines = []
        self._last_flush = time.monotonic()
        self._lock = threading.Lock()

    def __call__(self, message):
        """引擎日志入口（print 风格单行调用）。"""
        line = self._masker.mask(str(message))
        with self._lock:
            self._lines.append(line)
            if len(self._lines) >= LOG_FLUSH_LINES or (
                    time.monotonic() - self._last_flush
                    >= LOG_FLUSH_INTERVAL):
                self._flush_locked()

    def flush(self):
        """强制把缓冲中剩余日志落库（run 收尾时调用）。"""
        with self._lock:
            self._flush_locked()

    def _flush_locked(self):
        if not self._lines:
            return
        text = "\n".join(self._lines) + "\n"
        self._lines.clear()
        self._db.append_log(self._run_id, text)
        self._last_flush = time.monotonic()


def execute_run(trigger, settings, db):
    """执行一次 run：克隆 → 加载规则 → 扫描 → 影子门控/逐仓提交 → 落库。

    任何异常都不向上抛（Worker 线程必须存活）：run 记 failed，error
    与日志一律脱敏后落库。GITCODE_TOKEN 为空时直接 failed，不克隆。
    """
    default_token = os.environ.get("GITCODE_TOKEN", "").strip()
    shadow = is_shadow(db)
    # 全量扫或手动触发时没有可信增量区间（Trigger 合并语义已保证手动
    # 触发 sha 为空，这里再兜一层）
    if trigger.full_scan or trigger.trigger != "push":
        push_before = push_after = None
    else:
        push_before, push_after = trigger.before_sha, trigger.after_sha
    run_id = db.create_run(trigger=trigger.trigger, shadow=shadow,
                           keyword=trigger.keyword or "",
                           push_before=push_before, push_after=push_after)
    masker = SecretMasker([default_token, settings.admin_token,
                           settings.webhook_token, settings.dash_token])
    log = LogBuffer(db, run_id, masker)
    workdir = os.path.join(settings.work_dir, f"run-{run_id}")
    try:
        if not default_token:
            log("GITCODE_TOKEN 未配置，无法执行扫描")
            db.mark_run(run_id, status="failed",
                        error="GITCODE_TOKEN 未配置，请检查服务环境变量")
            return

        # ── 克隆来源仓 + 加载规则/名单（本地化模式，零 API） ──
        owner, repo = common._source_repo()
        src_dir = os.path.join(workdir, "src")
        log(f"正在克隆来源仓库 {owner}/{repo} ...")
        localgit.clone(owner, repo, None, default_token, src_dir,
                       log=log, blobs=True)
        # Token 规则仍从来源仓 token_rules.json 加载（DB 不再有关键字
        # 规则：仓库配置通过命名 token 下拉引用，控制精确到仓库）
        token_rules = service.load_token_rules(
            default_token, log=log, source_repo_dir=src_dir)
        rules_cache.set_rules(token_rules)
        masker.add_secrets(r.get("token") for r in token_rules)
        # 设置页命名 token 池中的 token 也纳入脱敏集合
        masker.add_secrets(t.get("token") for t in db.list_named_tokens())

        # ── 扫描（本地化 + 可选增量 diff） ──
        scan = service.scan_changed(
            trigger.keyword, default_token,
            token_rules=token_rules, log=log,
            source_repo_dir=src_dir,
            clone_root=os.path.join(workdir, "clones"),
            diff_before=push_before, diff_after=push_after)
        if not scan.get("ok"):
            db.mark_run(run_id, status="failed",
                        error=masker.mask(scan.get("error") or "扫描失败"))
            return
        db.mark_run(run_id, incremental=scan.get("incremental"))
        # 扫描错误可能携带含 token 的 URL，脱敏后再落库
        scan = dict(scan)
        scan["errors"] = [
            {**e, "error": masker.mask(str(e.get("error") or ""))}
            for e in scan.get("errors") or []
        ]

        # ── 影子门控 / 逐仓提交 ──
        if shadow:
            db.record_scan_results(run_id, scan, shadow=True)
            log("影子模式：仅记录待提交清单，不创建 PR")
        else:
            db.record_scan_results(run_id, scan, shadow=False)
            counts = _submit_all(run_id, scan.get("changed") or [],
                                 token_rules, default_token, log, masker, db)
        status = "partial" if scan["errors"] or (
            not shadow and counts["failed"]) else "success"
        db.mark_run(run_id, status=status)
        log(f"run 结束，状态: {status}")
    except Exception as e:
        # clone 失败会把含 token 的 URL 包进 GitCodeError，必须脱敏
        log(f"run 异常终止: {masker.mask(str(e))}")
        db.mark_run(run_id, status="failed", error=masker.mask(str(e)))
    finally:
        log.flush()
        db.kv_set("last_run_at", _utcnow())
        for event_id in trigger.push_event_ids or []:
            db.link_push_event(event_id, run_id)
        shutil.rmtree(workdir, ignore_errors=True)


def _submit_all(run_id, changed, token_rules, default_token, log, masker,
                db):
    """active 模式逐仓提交：仓库配置门控 + resolve_token 路由 + submit_repo，结果落库。

    未在设置页启用（无条目或已禁用）的仓库直接记 noop
    （stage='auto_pr_off'）跳过，默认拒绝。复制引擎 submit_all 的路由逻辑（配置了有效规则时只提交命中规则的
    仓库），但保留 submit_repo 返回的完整粒度（head_branch / noop /
    pr_existed）供落库。单仓失败不中断；有 pr_number 的结果同步
    upsert 到 prs 表（source='service'，供 prsync 轮询）。

    Returns:
        dict: record_submit_results 的计数汇总。
    """
    restrict = bool(rules._effective_rules(token_rules))
    results = []
    for i, item in enumerate(changed, 1):
        path = item["project"]["path"]
        log(f"正在提交 ({i}/{len(changed)}): {item['org']}/{item['repo']}")
        if not db.is_auto_pr_enabled(item["org"], item["repo"],
                                     item["branch"]):
            # 设置页未添加条目或已禁用：默认拒绝，记 noop 不提交
            log("  仓库配置未启用（设置页无此条目或已禁用），跳过")
            result = {"ok": True, "noop": True, "stage": "auto_pr_off"}
        else:
            item_token, rule_keyword = rules_cache.resolve_repo_token(
                db, item["org"], item["repo"], path, token_rules,
                default_token)
            if restrict and not rule_keyword:
                result = {"ok": False, "stage": "token_rule",
                          "error": "未命中 Token 规则，按权限管控不处理"}
            else:
                if rule_keyword:
                    log(f"  使用「{rule_keyword}」的 Token 提交")
                try:
                    result = dict(service.submit_repo(
                        item["org"], item["repo"], item["branch"],
                        item["files"], item_token, log))
                except Exception as e:
                    result = {"ok": False, "stage": "exception",
                              "error": str(e)}
        if result.get("error"):
            result["error"] = masker.mask(str(result["error"]))
        result.update(path=path, org=item["org"], repo=item["repo"],
                      branch=item["branch"])
        results.append(result)
        if result.get("pr_number"):
            db.upsert_pr(item["org"], item["repo"], result["pr_number"],
                         branch=item["branch"], path=path,
                         url=result.get("pr_url"),
                         title=result.get("pr_title"),
                         state="open", source="service", run_id=run_id)
    return db.record_submit_results(run_id, results)


class Worker:
    """全局唯一的 run 执行线程：阻塞 pop 队列，逐 Trigger 执行 execute_run。

    协作式停止：stop() 只置退出标志，正在执行的 run 会跑完后线程才
    退出——引擎在 run 中途无法安全中断（克隆/推送可能处于半完成状态），
    因此不提供强杀；调用方用 join() 等待线程自然结束。
    """

    def __init__(self, queue, settings, db, poll_timeout=1.0):
        self._queue = queue
        self._settings = settings
        self._db = db
        self._poll_timeout = poll_timeout
        self._stopping = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name="translator-ci-worker", daemon=True)

    def start(self):
        """启动 Worker 线程。"""
        self._thread.start()

    def stop(self):
        """请求停止：置标志位（协作式，不打断进行中的 run）。"""
        self._stopping.set()

    def join(self, timeout=None):
        """等待 Worker 线程退出。"""
        self._thread.join(timeout)

    @property
    def alive(self):
        """Worker 线程是否在运行。"""
        return self._thread.is_alive()

    def _loop(self):
        while not self._stopping.is_set():
            trigger = self._queue.pop(timeout=self._poll_timeout)
            if trigger is None:
                continue
            try:
                execute_run(trigger, self._settings, self._db)
            except Exception:
                # execute_run 内部已兜底，这里是防御性保护：Worker 必须存活
                traceback.print_exc()
