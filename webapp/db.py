"""SQLite 状态层：提交任务与逐仓库结果。

设计要点：
- 进程内单连接 + RLock 保护所有方法，WAL 模式；单实例部署前提下无需
  连接池
- 所有时间戳统一存 UTC ISO8601 字符串
- submissions.log 追加时截断保留尾部 200KB
- 调用方 token 永不落库：worker 只在内存中持有，日志/错误经脱敏后写入
"""

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

# submissions.log 尾部保留上限（200KB）
LOG_TAIL_LIMIT = 200 * 1024

_TERMINAL_STATUSES = ("success", "partial", "failed")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id  TEXT NOT NULL UNIQUE,
    status      TEXT NOT NULL,
    dry_run     INTEGER NOT NULL DEFAULT 0,
    path_count  INTEGER NOT NULL DEFAULT 0,
    paths_json  TEXT,
    error       TEXT,
    log         TEXT DEFAULT '',
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_submissions_created
    ON submissions(created_at);
CREATE INDEX IF NOT EXISTS idx_submissions_status
    ON submissions(status);
CREATE TABLE IF NOT EXISTS submission_items (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id     INTEGER NOT NULL,
    org               TEXT,
    repo              TEXT,
    branch            TEXT,
    status            TEXT NOT NULL DEFAULT 'queued',
    source_paths_json TEXT,
    files_json        TEXT,
    pr_number         INTEGER,
    pr_url            TEXT,
    pr_title          TEXT,
    pr_existed        INTEGER,
    stage             TEXT,
    error             TEXT,
    updated_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_submission_items_sub
    ON submission_items(submission_id);
"""

# update_submission_item 允许更新的列（防调用方拼接任意 SQL）
_ITEM_FIELDS = frozenset({
    "status", "pr_number", "pr_url", "pr_title", "pr_existed",
    "stage", "error", "files_json",
})


def _utcnow():
    """当前时间的 UTC ISO8601 字符串。"""
    return datetime.now(timezone.utc).isoformat()


def _tail(text, limit=LOG_TAIL_LIMIT):
    """按 UTF-8 字节数截断，保留尾部 limit 字节（不切断多字节字符）。"""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[-limit:].decode("utf-8", errors="ignore")


def _join(group):
    """把逐仓分组统一转为落库字段。"""
    return {
        "source_paths_json": json.dumps(group["source_paths"],
                                        ensure_ascii=False),
        "files_json": json.dumps(group["files"], ensure_ascii=False),
    }


class Database:
    """单连接 SQLite 封装，全部方法在 RLock 下执行，线程安全。"""

    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")

    def close(self):
        with self._lock:
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ── 内部工具 ─────────────────────────────────────────────

    def _one(self, sql, params=()):
        cur = self._conn.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row is not None else None

    def _all(self, sql, params=()):
        cur = self._conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    # ── 提交任务 ─────────────────────────────────────────────

    def create_submission(self, request_id, paths, groups, dry_run=False):
        """创建提交任务与逐仓库结果项（同一事务）。

        Args:
            request_id: 对外请求 ID（唯一）
            paths: 调用方传入的权威仓文件路径（已去重）
            groups: [{org, repo, branch, source_paths, files}]；
                files 为 [{"source_path", "target_path"}]
            dry_run: 是否只校验不创建 PR

        Returns:
            int: submissions 自增主键。
        """
        now = _utcnow()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO submissions"
                " (request_id, status, dry_run, path_count, paths_json,"
                "  created_at) VALUES (?, 'queued', ?, ?, ?, ?)",
                (request_id, int(bool(dry_run)), len(paths),
                 json.dumps(paths, ensure_ascii=False), now))
            submission_id = cur.lastrowid
            for group in groups:
                fields = _join(group)
                self._conn.execute(
                    "INSERT INTO submission_items"
                    " (submission_id, org, repo, branch, status,"
                    "  source_paths_json, files_json, updated_at)"
                    " VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)",
                    (submission_id, group["org"], group["repo"],
                     group["branch"], fields["source_paths_json"],
                     fields["files_json"], now))
            self._conn.commit()
        return submission_id

    def get_submission(self, submission_id):
        """按自增主键取任务（不含结果项）。"""
        with self._lock:
            return self._one(
                "SELECT * FROM submissions WHERE id = ?", (submission_id,))

    def get_submission_by_request_id(self, request_id):
        """按对外 request_id 取任务（不含结果项）。"""
        with self._lock:
            return self._one(
                "SELECT * FROM submissions WHERE request_id = ?",
                (request_id,))

    def get_submission_items(self, submission_id):
        """取任务的全部逐仓库结果项（source_paths/files 已解析为对象）。"""
        with self._lock:
            rows = self._all(
                "SELECT * FROM submission_items"
                " WHERE submission_id = ? ORDER BY id", (submission_id,))
        for row in rows:
            row["source_paths"] = _loads(row.pop("source_paths_json"))
            row["files"] = _loads(row.pop("files_json"))
        return rows

    def get_items_with_pr(self, submission_id):
        """取任务中已有 PR 号的结果项（供按 request_id 查 PR 状态）。"""
        with self._lock:
            return self._all(
                "SELECT * FROM submission_items"
                " WHERE submission_id = ? AND pr_number IS NOT NULL"
                " ORDER BY id", (submission_id,))

    def mark_submission_running(self, submission_id):
        """任务开始执行：status=running，记录 started_at。"""
        with self._lock:
            self._conn.execute(
                "UPDATE submissions SET status = 'running', started_at = ?"
                " WHERE id = ?", (_utcnow(), submission_id))
            self._conn.commit()

    def update_submission_item(self, item_id, **fields):
        """更新逐仓库结果项（仅允许白名单列）。"""
        updates = {k: v for k, v in fields.items() if k in _ITEM_FIELDS}
        if not updates:
            return
        updates["updated_at"] = _utcnow()
        marks = ", ".join(f"{k} = ?" for k in updates)
        with self._lock:
            self._conn.execute(
                f"UPDATE submission_items SET {marks} WHERE id = ?",
                (*updates.values(), item_id))
            self._conn.commit()

    def fail_queued_items(self, submission_id, error):
        """把仍处于 queued 的结果项置为 failed（不覆盖已有结果）。"""
        with self._lock:
            self._conn.execute(
                "UPDATE submission_items SET status = 'failed', error = ?,"
                " updated_at = ? WHERE submission_id = ? AND status = 'queued'",
                (error, _utcnow(), submission_id))
            self._conn.commit()

    def append_log(self, submission_id, text):
        """追加任务日志，保留尾部 LOG_TAIL_LIMIT 字节。"""
        if not text:
            return
        with self._lock:
            row = self._one(
                "SELECT log FROM submissions WHERE id = ?", (submission_id,))
            if row is None:
                return
            merged = (row.get("log") or "") + text
            self._conn.execute(
                "UPDATE submissions SET log = ? WHERE id = ?",
                (_tail(merged), submission_id))
            self._conn.commit()

    def get_submission_log(self, submission_id):
        """取任务日志尾部（已脱敏）。"""
        with self._lock:
            row = self._one(
                "SELECT log FROM submissions WHERE id = ?", (submission_id,))
        return (row or {}).get("log") or ""

    def finish_submission(self, submission_id, status, error=None):
        """任务结束：写终态与 finished_at。"""
        with self._lock:
            self._conn.execute(
                "UPDATE submissions SET status = ?, error = ?,"
                " finished_at = ? WHERE id = ?",
                (status, error, _utcnow(), submission_id))
            self._conn.commit()

    def is_terminal(self, status):
        """任务状态是否为终态。"""
        return status in _TERMINAL_STATUSES

    def fail_unfinished_submissions(self, reason):
        """启动恢复：queued/running 任务不可能继续执行，统一置 failed。

        Returns:
            int: 被标记的任务数。
        """
        now = _utcnow()
        with self._lock:
            rows = self._all(
                "SELECT id FROM submissions WHERE status IN"
                " ('queued', 'running')")
            for row in rows:
                self._conn.execute(
                    "UPDATE submissions SET status = 'failed',"
                    " error = ?, finished_at = ? WHERE id = ?",
                    (reason, now, row["id"]))
                self._conn.execute(
                    "UPDATE submission_items SET status = 'failed',"
                    " error = ?, updated_at = ?"
                    " WHERE submission_id = ? AND status = 'queued'",
                    (reason, now, row["id"]))
            self._conn.commit()
        return len(rows)

    def cleanup_old_submissions(self, days=90):
        """删除 days 天前创建的任务及其结果项。

        Returns:
            dict: {"submissions": 删除任务数, "items": 删除结果项数}
        """
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=max(int(days), 1))).isoformat()
        with self._lock:
            old_ids = [r["id"] for r in self._all(
                "SELECT id FROM submissions WHERE created_at < ?", (cutoff,))]
            if not old_ids:
                return {"submissions": 0, "items": 0}
            marks = ",".join("?" * len(old_ids))
            items = self._conn.execute(
                f"DELETE FROM submission_items WHERE submission_id IN"
                f" ({marks})", old_ids).rowcount
            submissions = self._conn.execute(
                f"DELETE FROM submissions WHERE id IN ({marks})",
                old_ids).rowcount
            self._conn.commit()
        return {"submissions": submissions, "items": items}

    def pending_count(self):
        """排队中的任务数（供 /healthz）。"""
        with self._lock:
            row = self._one(
                "SELECT COUNT(*) AS n FROM submissions WHERE status = 'queued'")
        return int((row or {}).get("n") or 0)

    def running_count(self):
        """执行中的任务数（供 /healthz）。"""
        with self._lock:
            row = self._one(
                "SELECT COUNT(*) AS n FROM submissions WHERE status = 'running'")
        return int((row or {}).get("n") or 0)


def _loads(raw):
    """解析 JSON 列；非法或空值返回空列表。"""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def init_db(path):
    """打开（必要时创建）数据库并建表，幂等；父目录自动创建。"""
    path = str(path)
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    db = Database(path)
    with db._lock:
        db._conn.executescript(_SCHEMA)
        db._conn.commit()
    return db
