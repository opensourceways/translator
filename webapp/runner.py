"""提交任务执行：克隆权威仓 → 校验/读取指定文件 → 逐仓幂等提 PR。

流程：
1. API 已把路径解析为 (org, repo, branch) 分组并落库；Worker 只消费
   (submission_id, caller_token)，token 仅存在于内存中
2. 克隆权威仓默认分支（blobs=True）到任务工作目录，把每个目标仓坐标
   （组织/仓库/分支/相对路径）精确解析为权威仓内文件路径；找不到或命中
   多个即整单失败，避免部分 PR 被误创建
3. 读取文件内容并做单文件/累计大小校验，按目标仓分组后调用
   translator.submit.submit_repo（Fork → 固定分支 → 单 commit → PR 去重）
4. 权威仓读取使用服务侧默认 ATOMGIT_TOKEN；目标仓 Fork/PR 使用调用方
   token
5. dry_run 只校验并记录 would_submit，不创建 PR
6. 任何落库/日志文本先过 SecretMasker，调用方 token 永不落库
"""

import base64
import json
import os
import shutil
import threading
import traceback
import time
from datetime import datetime, timezone

from translator import common, localgit, secrets, service
from translator.common import MAX_FILE_SIZE, MAX_TOTAL_SIZE
from webapp.paths import build_source_index

LOG_FLUSH_LINES = 50
LOG_FLUSH_INTERVAL = 2.0

# 权威仓读取分支：留空表示克隆仓库默认分支（当前为 main）。
# 不硬编码 master：权威仓默认分支已改为 main，写死会导致整单失败。
SOURCE_REF = ""


def _utcnow():
    """当前时间的 UTC ISO8601 字符串（与落库时间戳口径一致）。"""
    return datetime.now(timezone.utc).isoformat()


class SecretMasker:
    """token 脱敏器：把集合中每个非空秘密值替换为 ``***``。

    每个提交任务独立构造，集合包含服务侧默认 token 与本次调用方 token；
    调用方 token 不落库，只在任务执行期间存在于内存与脱敏集合中。
    """

    def __init__(self, values=()):
        self._lock = threading.Lock()
        self._values = []
        self.add(values)

    def add(self, values):
        """追加秘密值（空值忽略）；长的先替换，避免短前缀先命中漏掉长值。"""
        with self._lock:
            for value in values or []:
                value = (value or "").strip()
                if value and value not in self._values:
                    self._values.append(value)
            self._values.sort(key=len, reverse=True)

    def mask(self, text):
        """返回脱敏后的文本。"""
        if not text:
            return text
        with self._lock:
            values = list(self._values)
        for value in values:
            text = text.replace(value, "***")
        return text


class LogBuffer:
    """任务日志缓冲：满 50 行或超过 2 秒批量落库，行先脱敏。"""

    def __init__(self, db, submission_id, masker):
        self._db = db
        self._submission_id = submission_id
        self._masker = masker
        self._lines = []
        self._last_flush = time.monotonic()
        self._lock = threading.Lock()

    def __call__(self, message):
        line = self._masker.mask(str(message))
        with self._lock:
            self._lines.append(line)
            if len(self._lines) >= LOG_FLUSH_LINES or (
                    time.monotonic() - self._last_flush
                    >= LOG_FLUSH_INTERVAL):
                self._flush_locked()

    def flush(self):
        """强制把缓冲中剩余日志落库。"""
        with self._lock:
            self._flush_locked()

    def _flush_locked(self):
        if not self._lines:
            return
        text = "\n".join(self._lines) + "\n"
        self._lines.clear()
        self._db.append_log(self._submission_id, text)
        self._last_flush = time.monotonic()


class ValidationError(Exception):
    """提交前校验失败（路径不存在/文件过大等），整单失败。"""


def _file_rel(meta):
    """文件相对路径：优先用分组时写入的 rel，旧记录回退按坐标解析。"""
    rel = meta.get("rel")
    if rel:
        return rel
    segments = str(meta.get("source_path") or "").split("/")
    return "/".join(segments[3:])


def _resolve_sources(items, index):
    """把每个文件的目标仓坐标解析为权威仓内路径（精确匹配，不猜）。

    Args:
        items: DB 中的提交结果项（含 org/repo/branch/files）。
        index: ``webapp.paths.SourceIndex``。

    Returns:
        (resolved, missing, ambiguous)：resolved 为
        {(item_id, source_path): 权威仓路径}；missing 为「权威仓中找不到」的
        说明；ambiguous 为「命中多个、无法确定」的说明。
    """
    resolved, missing, ambiguous = {}, [], []
    for item in items:
        label = f"{item['org']}/{item['repo']}@{item['branch']}"
        for meta in item["files"]:
            rel = _file_rel(meta)
            hits = index.candidates(item["org"], item["repo"],
                                    item["branch"], rel)
            if len(hits) == 1:
                resolved[(item["id"], meta["source_path"])] = hits[0]
            elif not hits:
                missing.append(f"{meta['source_path']}（{label} 下无 {rel}）")
            else:
                ambiguous.append(
                    f"{meta['source_path']} -> " + " / ".join(hits))
    return resolved, missing, ambiguous


def _read_files(items, src_dir, resolved, log):
    """按解析出的权威仓路径读取并校验任务全部文件。

    Args:
        items: 提交结果项。
        src_dir: 权威仓克隆目录。
        resolved: {(item_id, source_path): 权威仓路径}。
        log: 日志函数。

    Returns:
        dict: {item_id: [{"target_path", "content_b64"}]}

    Raises:
        ValidationError: 任一文件读取失败、超过单文件上限或累计大小超限。
    """
    prepared = {}
    total_size = 0
    for item in items:
        files = []
        for meta in item["files"]:
            source_path = meta["source_path"]
            authority_path = resolved[(item["id"], source_path)]
            try:
                content = localgit.read_file(src_dir, authority_path)
            except Exception as exc:
                raise ValidationError(
                    f"读取权威仓文件失败: {authority_path}: {exc}") from exc
            if len(content) > MAX_FILE_SIZE:
                raise ValidationError(
                    f"文件 {source_path} 大小 "
                    f"{len(content) / 1024 / 1024:.1f} MB 超过单文件上限 "
                    f"{MAX_FILE_SIZE / 1024 / 1024:.0f} MB")
            total_size += len(content)
            if total_size > MAX_TOTAL_SIZE:
                raise ValidationError(
                    f"累计文件大小 {total_size / 1024 / 1024:.1f} MB 超过"
                    f"上限 {MAX_TOTAL_SIZE / 1024 / 1024:.0f} MB")
            log(f"  读取文件: {source_path} -> {authority_path}"
                f"（目标 {meta['target_path']}）")
            files.append({
                "target_path": meta["target_path"],
                "content_b64": base64.b64encode(content).decode("ascii"),
            })
        prepared[item["id"]] = files
    return prepared


def _item_status(result):
    """把 submit_repo 返回值映射为结果项状态。"""
    if not result.get("ok"):
        return "failed"
    if result.get("noop"):
        return "noop"
    if result.get("pr_existed"):
        return "already_exists"
    return "submitted"


def _save_item(db, item_id, status, result=None):
    """把单仓提交结果写回 submission_items。"""
    result = result or {}
    db.update_submission_item(
        item_id,
        status=status,
        pr_number=result.get("pr_number"),
        pr_url=result.get("pr_url"),
        pr_title=result.get("pr_title"),
        pr_existed=(None if result.get("pr_existed") is None
                    else int(bool(result.get("pr_existed")))),
        stage=result.get("stage"),
        error=result.get("error"),
    )


def _finish(db, submission_id, status, error=None):
    """写任务终态。"""
    db.finish_submission(submission_id, status, error=error)


def execute_submission(submission_id, caller_token, settings, db):
    """执行一次提交任务；任何异常都不向 Worker 线程抛出。"""
    submission = db.get_submission(submission_id)
    if submission is None or submission["status"] != "queued":
        return

    db.mark_submission_running(submission_id)
    caller_token = (caller_token or "").strip()
    source_token = secrets.get("ATOMGIT_TOKEN", "").strip()
    masker = SecretMasker([source_token, caller_token])
    log = LogBuffer(db, submission_id, masker)
    workdir = os.path.join(settings.work_dir, "submissions",
                           f"submission-{submission_id}")
    items = db.get_submission_items(submission_id)
    try:
        if not caller_token:
            raise ValidationError("调用方 AtomGit token 为空")
        if not source_token:
            raise ValidationError(
                "服务未配置 ATOMGIT_TOKEN，无法读取权威仓")
        if not items:
            raise ValidationError("提交任务没有待处理文件")

        owner, repo = common._source_repo()
        src_dir = os.path.join(workdir, "src")
        log(f"正在克隆权威仓 {owner}/{repo}（默认分支）...")
        localgit.clone(owner, repo, SOURCE_REF, source_token, src_dir,
                       log=log, blobs=True)

        tree = localgit.tree_map(src_dir)
        index = build_source_index(tree)
        resolved, missing, ambiguous = _resolve_sources(items, index)
        if ambiguous:
            raise ValidationError(
                "以下文件在权威仓中匹配到多个文件，无法确定提交哪一份: "
                + "; ".join(ambiguous))
        if missing:
            raise ValidationError(
                "以下文件在权威仓默认分支中不存在: " + ", ".join(missing))

        prepared = _read_files(items, src_dir, resolved, log)

        if submission["dry_run"]:
            log("dry_run=true：仅校验路径与大小，不创建 PR")
            for item in items:
                _save_item(db, item["id"], "would_submit")
            _finish(db, submission_id, "success")
            return

        statuses = []
        for item in items:
            label = f"{item['org']}/{item['repo']}@{item['branch']}"
            log(f"正在提交 {label}（{len(item['files'])} 个文件）...")
            try:
                result = dict(service.submit_repo(
                    item["org"], item["repo"], item["branch"],
                    prepared[item["id"]], caller_token, log))
            except Exception as exc:
                result = {"ok": False, "stage": "exception",
                          "error": str(exc)}
            if result.get("error"):
                result["error"] = masker.mask(str(result["error"]))
            status = _item_status(result)
            statuses.append(status)
            _save_item(db, item["id"], status, result)

        if statuses and all(s == "failed" for s in statuses):
            _finish(db, submission_id, "failed",
                    error="全部仓库提交失败，详见各结果项")
        elif any(s == "failed" for s in statuses):
            _finish(db, submission_id, "partial",
                    error="部分仓库提交失败，详见各结果项")
        else:
            _finish(db, submission_id, "success")
        log(f"任务结束，状态: {db.get_submission(submission_id)['status']}")
    except ValidationError as exc:
        error = masker.mask(str(exc))
        log(f"任务校验失败: {error}")
        db.fail_queued_items(submission_id, error)
        _finish(db, submission_id, "failed", error=error)
    except Exception as exc:
        error = masker.mask(str(exc))
        log(f"任务异常终止: {error}")
        db.fail_queued_items(submission_id, error)
        _finish(db, submission_id, "failed", error=error)
    finally:
        log.flush()
        shutil.rmtree(workdir, ignore_errors=True)


class SubmissionWorker:
    """全局唯一的提交任务执行线程（协作式停止）。"""

    def __init__(self, queue, settings, db, poll_timeout=1.0):
        self._queue = queue
        self._settings = settings
        self._db = db
        self._poll_timeout = poll_timeout
        self._stopping = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name="translator-submission-worker",
            daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        """请求停止：置标志位（不打断进行中的任务）。"""
        self._stopping.set()

    def join(self, timeout=None):
        self._thread.join(timeout)

    @property
    def alive(self):
        return self._thread.is_alive()

    def _loop(self):
        while not self._stopping.is_set():
            entry = self._queue.pop(timeout=self._poll_timeout)
            if entry is None:
                continue
            submission_id, token = entry
            try:
                execute_submission(
                    submission_id, token, self._settings, self._db)
            except Exception:
                # execute_submission 内部已兜底，这里是防御性保护
                traceback.print_exc()
