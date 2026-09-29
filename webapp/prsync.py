"""PR 状态同步：Poller 线程轮询已知 open PR，影子模式下发现 Jenkins 建的 PR。

对应服务化改造计划 prsync 行：
- 每 settings.poll_interval 秒执行一轮（启动后立即执行首轮，快速收敛）；
  轮询日志只进 logging，不写 runs 表
- 已知 open PR 逐仓查询 merged/closed/mergeable，单轮上限
  MAX_REPOS_PER_POLL 个仓（按 last_polled 最旧优先）防限流；引擎 client
  的进程级限流（45 次/60s）兜底
- 影子模式下对已知仓库（prs 表 + 近期 run_repos 去重）拉全量 open PR，
  按 head.ref 前缀 translator/auto- + base 过滤（口径同引擎
  submit._pr_matches）发现 Jenkins 建的 PR，落库 source='jenkins'；
  发现与轮询共享单轮仓库预算
- 单 PR/单仓失败（GitCodeError）只告警不中断本轮其余处理；整轮异常由
  Poller.run 兜底，告警后下轮继续
"""

import logging
import os
import threading
from datetime import datetime, timezone

from translator_ci import client, service
from translator_ci.common import AUTO_BRANCH_PREFIX
from translator_ci.rules import resolve_token

from webapp import rules_cache
from webapp.db import is_shadow

log = logging.getLogger("webapp.prsync")

# 单轮处理仓库数上限（轮询与发现共享），避免打满 GitCode 账号限流
MAX_REPOS_PER_POLL = 20

# 每轮顺带清理多少天前的 run（级联删 run_repos）
CLEANUP_DAYS = 90


def _utcnow():
    """当前时间的 UTC ISO8601 字符串（与落库时间戳口径一致）。"""
    return datetime.now(timezone.utc).isoformat()


class Poller(threading.Thread):
    """PR 状态轮询线程（协作式停止）。

    start() 继承自 Thread；stop() 置退出标志，主循环在 poll_interval
    等待处即时响应退出。首轮立即执行，便于服务启动后快速收敛。
    """

    def __init__(self, db, settings, name="prsync-poller"):
        super().__init__(name=name, daemon=True)
        self._db = db
        self._settings = settings
        self._stop_event = threading.Event()

    def run(self):
        while not self._stop_event.is_set():
            try:
                _poll_once(self._db, self._settings)
            except Exception:
                log.exception("轮询一轮失败，下轮继续")
            self._stop_event.wait(self._settings.poll_interval)

    def stop(self):
        """请求停止：主循环在本轮结束或等待唤醒后退出。"""
        self._stop_event.set()


def _poll_once(db, settings):
    """执行一轮轮询：规则 → 已知 open PR 状态 → 影子模式 PR 发现 → 收尾。"""
    default_token = os.environ.get("GITCODE_TOKEN", "")
    rules = _ensure_rules(default_token)
    used, polled = _poll_open_prs(db, rules, default_token,
                                  MAX_REPOS_PER_POLL)
    if is_shadow(db) and used < MAX_REPOS_PER_POLL:
        _discover_jenkins_prs(db, rules, default_token,
                              MAX_REPOS_PER_POLL - used, polled)
    db.kv_set("last_poll_at", _utcnow())
    db.cleanup_old_runs(CLEANUP_DAYS)


def _ensure_rules(default_token):
    """取 Token 规则：优先进程内缓存（runner 每次 run 后写入）。

    缓存为空（如服务重启后尚无 run）时回退 API 模式加载一次并回填缓存；
    加载失败只告警，本轮全部使用默认 Token 继续。
    """
    rules = rules_cache.get_rules()
    if rules:
        return rules
    try:
        rules = service.load_token_rules(default_token, log=log.info)
    except Exception as e:
        log.warning("加载 Token 规则失败，本轮全部使用默认 Token: %s", e)
        return []
    rules_cache.set_rules(rules)
    return rules


def _poll_open_prs(db, rules, default_token, budget):
    """轮询已知 open PR 状态，单轮最多 budget 个仓（按 last_polled 最旧优先）。

    Returns:
        tuple: (实际处理的仓库数, 本轮处理过的 (org, repo) 集合)。
    """
    groups = {}
    for row in db.open_prs():
        groups.setdefault((row["org"], row["repo"]), []).append(row)
    ordered = sorted(
        groups.items(),
        key=lambda kv: min(r["last_polled"] or "" for r in kv[1]))
    used = 0
    polled = set()
    for (org, repo), rows in ordered:
        if used >= budget:
            break
        used += 1
        polled.add((org, repo))
        for row in rows:
            _poll_one_pr(db, row, rules, default_token)
    return used, polled


def _extract_labels(pr):
    """从 PR 响应提取标签（名字+颜色）；无 labels 字段返回 None（不覆盖库中已有值）。"""
    if "labels" not in pr:
        return None
    return [{"name": l.get("name"), "color": l.get("color")}
            for l in pr.get("labels") or [] if isinstance(l, dict)]


def _poll_one_pr(db, row, rules, default_token):
    """查询单个 PR 最新状态并落库；失败只告警不中断。

    除 state/mergeable 外，顺带落派生状态原始信号：labels（名字+颜色）、
    draft、评审人计数（assignees 中 accept 为真记为已通过）。
    """
    org, repo, number = row["org"], row["repo"], row["number"]
    token, _ = resolve_token(row.get("path") or "", rules, default_token)
    try:
        pr = client.get_pull_request(org, repo, number, token)
    except client.GitCodeError as e:
        log.warning("查询 PR 状态失败 %s/%s#%s: %s", org, repo, number, e)
        return
    assignees = None
    if "assignees" in pr:
        assignees = [a for a in pr.get("assignees") or []
                     if isinstance(a, dict)]
    # apply_pr_state 同时刷新 last_polled；失败的 PR 不刷新，下轮优先重试
    db.apply_pr_state(org, repo, number, _map_state(pr),
                      mergeable=pr.get("mergeable"),
                      merged_at=pr.get("merged_at"),
                      closed_at=pr.get("closed_at"),
                      labels=_extract_labels(pr),
                      draft=pr.get("draft"),
                      review_total=(len(assignees)
                                    if assignees is not None else None),
                      review_accepted=(sum(1 for a in assignees
                                           if a.get("accept"))
                                       if assignees is not None else None))


def _map_state(pr):
    """把 GitCode PR 响应映射为落库状态（merged/closed/open）。

    GitCode（Gitee 系）PR 的 state 通常只有 open/closed，"已合并"看
    merged 字段为真或 merged_at 非空，两者任一成立即 merged。
    """
    if pr.get("merged") or pr.get("merged_at"):
        return "merged"
    if pr.get("state") == "closed":
        return "closed"
    return "open"


def _discover_jenkins_prs(db, rules, default_token, budget, polled):
    """影子模式：对已知仓库拉全量 open PR，发现 Jenkins 建的自动翻译 PR。

    候选仓库为 db.distinct_repos()（prs 表 + 近期 run_repos 去重），跳过
    本轮已轮询的仓库；从未轮询过的仓库优先。单仓失败只告警。
    """
    last_polled = {}
    for row in db.list_prs():
        key = (row["org"], row["repo"])
        ts = row.get("last_polled") or ""
        if key not in last_polled or ts < last_polled[key]:
            last_polled[key] = ts
    candidates = [info for info in db.distinct_repos()
                  if (info["org"], info["repo"]) not in polled]
    # 从未出现在 prs 表的仓库（空串）排最前，其后按最旧 last_polled
    candidates.sort(key=lambda i: (last_polled.get((i["org"], i["repo"]), ""),
                                   i["org"], i["repo"], i["branch"] or ""))
    for info in candidates[:budget]:
        _discover_repo(db, info, rules, default_token)


def _discover_repo(db, info, rules, default_token):
    """单仓发现：list_pull_requests 拉全量 open PR 后客户端过滤落库。"""
    org, repo, branch = info["org"], info["repo"], info["branch"]
    token, _ = resolve_token(info.get("path") or "", rules, default_token)
    try:
        prs = client.list_pull_requests(org, repo, token, state="open")
    except client.GitCodeError as e:
        log.warning("列出 PR 失败 %s/%s: %s", org, repo, e)
        return
    for pr in prs:
        number = pr.get("number")
        if number is None or not _is_auto_pr(pr, branch):
            continue
        db.upsert_pr(org, repo, number, branch=branch,
                     path=info.get("path"),
                     url=pr.get("html_url") or pr.get("url"),
                     title=pr.get("title"), state="open",
                     mergeable=pr.get("mergeable"), source="jenkins",
                     labels=_extract_labels(pr), draft=pr.get("draft"))


def _is_auto_pr(pr, branch):
    """判断是否为自动翻译 PR：head 分支带 translator/auto- 前缀且 base 匹配。

    head/base 的 ref 缺失时回退 label 冒号后段（口径同引擎
    submit._pr_matches）。
    """
    head_info = pr.get("head") or {}
    base_info = pr.get("base") or {}
    head_ref = (head_info.get("ref")
                or (head_info.get("label") or "").split(":")[-1])
    base_ref = (base_info.get("ref")
                or (base_info.get("label") or "").split(":")[-1])
    if not head_ref.startswith(AUTO_BRANCH_PREFIX):
        return False
    return not base_ref or base_ref == branch
