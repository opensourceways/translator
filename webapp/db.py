"""SQLite 状态层：push 事件 / run / 仓库结果 / PR 追踪 / 开关配置。

设计要点（对应服务化改造计划「SQLite 核心表」节）：
- 进程内单连接 + threading.RLock 保护所有方法，WAL 模式；单实例部署
  前提下无需多连接池
- 所有时间戳统一存 UTC ISO8601 字符串（_utcnow）
- runs.log 追加时截断保留尾部 200KB（全量扫描日志可达数千行）；
  调用方（runner）按 50 行/2s 缓冲后批量调用 append_log
- prs.mergeable 三态：None 存 NULL（服务端尚未计算）
- prs.labels_json/draft/review_total/review_accepted 为原始信号，
  派生状态（CI 未通过/待评审等）由 webapp.prstatus 在展示层计算
- 任何 token 值不进日志/其他表：唯一的例外是 named_tokens 表
  （设置页自助配置的命名 token 池，按用户明确要求落库，见
  list/upsert_named_token）；repo_configs 只存 token_name 引用，
  本模块其他接口不接收 token 参数
"""

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

# runs.log 尾部保留上限（200KB）
LOG_TAIL_LIMIT = 200 * 1024

# run 终态（其余为 running/interrupted 等）
_RUN_TERMINAL = ("success", "partial", "failed", "interrupted")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS push_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT,
    ref         TEXT,
    before_sha  TEXT,
    after_sha   TEXT,
    accepted    INTEGER,
    run_id      INTEGER
);
CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger         TEXT,
    shadow          INTEGER,
    status          TEXT,
    keyword         TEXT,
    push_before     TEXT,
    push_after      TEXT,
    incremental     INTEGER DEFAULT 0,
    scanned_count   INTEGER DEFAULT 0,
    changed_count   INTEGER DEFAULT 0,
    unchanged_count INTEGER DEFAULT 0,
    skipped_count   INTEGER DEFAULT 0,
    error_count     INTEGER DEFAULT 0,
    submit_ok_count INTEGER DEFAULT 0,
    noop_count      INTEGER DEFAULT 0,
    failed_count    INTEGER DEFAULT 0,
    error           TEXT,
    log             TEXT DEFAULT '',
    created_at      TEXT,
    started_at      TEXT,
    finished_at     TEXT
);
CREATE TABLE IF NOT EXISTS run_repos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL,
    path         TEXT,
    org          TEXT,
    repo         TEXT,
    branch       TEXT,
    status       TEXT,
    new_count    INTEGER DEFAULT 0,
    update_count INTEGER DEFAULT 0,
    files_json   TEXT,
    reason       TEXT,
    stage        TEXT,
    error        TEXT,
    head_branch  TEXT,
    pr_number    INTEGER,
    pr_url       TEXT,
    pr_existed   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_run_repos_run ON run_repos(run_id);
CREATE TABLE IF NOT EXISTS prs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org         TEXT,
    repo        TEXT,
    branch      TEXT,
    number      INTEGER,
    path        TEXT,
    url         TEXT,
    title       TEXT,
    state       TEXT,
    mergeable   INTEGER,
    source      TEXT,
    run_id      INTEGER,
    first_seen  TEXT,
    last_polled TEXT,
    merged_at   TEXT,
    closed_at   TEXT,
    labels_json TEXT,
    draft       INTEGER,
    review_total    INTEGER,
    review_accepted INTEGER,
    created_at  TEXT,
    UNIQUE(org, repo, number)
);
CREATE TABLE IF NOT EXISTS named_tokens (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    token      TEXT NOT NULL,
    created_at TEXT,
    updated_at TEXT,
    UNIQUE(name)
);
CREATE TABLE IF NOT EXISTS repo_configs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    org        TEXT NOT NULL,
    repo       TEXT NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    token_name TEXT NOT NULL DEFAULT '',
    created_at TEXT,
    updated_at TEXT,
    UNIQUE(org, repo)
);
"""

# prs 表后加列（轻量迁移用）：列名 → 列定义；旧库缺列时 ALTER TABLE 补齐
_PRS_EXTRA_COLUMNS = {
    "labels_json": "TEXT",
    "draft": "INTEGER",
    "review_total": "INTEGER",
    "review_accepted": "INTEGER",
    "created_at": "TEXT",  # PR 在 GitCode 上的真实创建时间
}



def _utcnow():
    """当前时间的 UTC ISO8601 字符串。"""
    return datetime.now(timezone.utc).isoformat()


def _tail(text, limit=LOG_TAIL_LIMIT):
    """按 UTF-8 字节数截断，保留尾部 limit 字节（不切断多字节字符）。"""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[-limit:].decode("utf-8", errors="ignore")


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

    # ── push 事件 ────────────────────────────────────────────

    def record_push_event(self, ref, before_sha, after_sha, accepted,
                          run_id=None):
        """记录一次到达的 webhook 事件（含被 ref 过滤拒绝的）。

        分支删除事件（after 全零）由调用方记 accepted=0。

        Returns:
            int: 事件 id。
        """
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO push_events"
                " (received_at, ref, before_sha, after_sha, accepted, run_id)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (_utcnow(), ref, before_sha, after_sha,
                 1 if accepted else 0, run_id))
            self._conn.commit()
            return cur.lastrowid

    def link_push_event(self, event_id, run_id):
        """事件入队生成 run 后回填 run_id。"""
        with self._lock:
            self._conn.execute(
                "UPDATE push_events SET run_id = ? WHERE id = ?",
                (run_id, event_id))
            self._conn.commit()

    # ── run 生命周期 ─────────────────────────────────────────

    def create_run(self, trigger, shadow, keyword="", push_before=None,
                   push_after=None):
        """创建 run，状态 running，created_at/started_at 记为当前时间。

        Args:
            trigger: 触发来源（webhook / manual / recovery）
            shadow: 是否影子模式（本次 run 的开关快照）

        Returns:
            int: run id。
        """
        now = _utcnow()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO runs (trigger, shadow, status, keyword,"
                " push_before, push_after, created_at, started_at)"
                " VALUES (?, ?, 'running', ?, ?, ?, ?, ?)",
                (trigger, 1 if shadow else 0, keyword,
                 push_before, push_after, now, now))
            self._conn.commit()
            return cur.lastrowid

    def mark_run(self, run_id, status=None, error=None, incremental=None):
        """更新 run 状态；终态时自动写 finished_at。"""
        fields = []
        params = []
        if status is not None:
            fields.append("status = ?")
            params.append(status)
            if status in _RUN_TERMINAL:
                fields.append("finished_at = ?")
                params.append(_utcnow())
        if error is not None:
            fields.append("error = ?")
            params.append(error)
        if incremental is not None:
            fields.append("incremental = ?")
            params.append(1 if incremental else 0)
        if not fields:
            return
        params.append(run_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE runs SET {', '.join(fields)} WHERE id = ?", params)
            self._conn.commit()

    def interrupt_running_runs(self):
        """启动恢复：把残留的 running run 标记为 interrupted（僵尸恢复）。

        Returns:
            int: 被标记的 run 数。
        """
        now = _utcnow()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE runs SET status = 'interrupted', finished_at = ?"
                " WHERE status = 'running'", (now,))
            self._conn.commit()
            return cur.rowcount

    def append_log(self, run_id, text):
        """向 runs.log 追加一段日志（调用方已按 50 行/2s 缓冲批量刷写）。

        追加后截断只保留尾部 200KB。
        """
        if not text:
            return
        with self._lock:
            row = self._one("SELECT log FROM runs WHERE id = ?", (run_id,))
            if row is None:
                return
            merged = _tail((row["log"] or "") + text)
            self._conn.execute(
                "UPDATE runs SET log = ? WHERE id = ?", (merged, run_id))
            self._conn.commit()

    # ── 扫描/提交结果落库 ────────────────────────────────────

    def record_scan_results(self, run_id, scan, shadow=False):
        """把 scan_changed 的返回落为 run_repos 行并更新 run 计数。

        changed 清单在影子模式记 status='would_submit'，active 模式记
        'changed'（待 runner 逐仓提交后再由 record_submit_results 改写）；
        files_json 只存 target_path 列表，剥离 content_b64。

        Args:
            run_id: run id
            scan: scan_changed 返回的 dict（changed/unchanged/skipped/errors）
            shadow: 是否影子模式

        Returns:
            dict: 落库的计数汇总。
        """
        changed = scan.get("changed") or []
        unchanged = scan.get("unchanged") or []
        skipped = scan.get("skipped") or []
        errors = scan.get("errors") or []
        rows = []
        for item in changed:
            project = item.get("project") or {}
            files = [f.get("target_path") for f in item.get("files") or []
                     if f.get("target_path")]
            rows.append((run_id, project.get("path"), item.get("org"),
                         item.get("repo"), item.get("branch"),
                         "would_submit" if shadow else "changed",
                         item.get("new_count", 0),
                         item.get("update_count", 0),
                         json.dumps(files, ensure_ascii=False),
                         None, None, None))
        for path in unchanged:
            rows.append((run_id, path, None, None, None, "unchanged",
                         0, 0, None, None, None, None))
        for item in skipped:
            rows.append((run_id, item.get("path"), None, None, None,
                         "skipped", 0, 0, None, item.get("reason"),
                         None, None))
        for item in errors:
            rows.append((run_id, item.get("path"), None, None, None,
                         "error", 0, 0, None, None, None,
                         item.get("error")))
        counts = {
            "scanned_count": len(changed) + len(unchanged)
            + len(skipped) + len(errors),
            "changed_count": len(changed),
            "unchanged_count": len(unchanged),
            "skipped_count": len(skipped),
            "error_count": len(errors),
        }
        with self._lock:
            self._conn.executemany(
                "INSERT INTO run_repos (run_id, path, org, repo, branch,"
                " status, new_count, update_count, files_json, reason,"
                " stage, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows)
            sets = ", ".join(f"{k} = ?" for k in counts)
            self._conn.execute(
                f"UPDATE runs SET {sets} WHERE id = ?",
                (*counts.values(), run_id))
            self._conn.commit()
        return counts

    def record_submit_results(self, run_id, results):
        """把逐仓 submit_repo 的结果回写 run_repos 并更新 run 计数。

        runner 直接循环引擎 submit_repo，每项需带 path（用于匹配扫描阶段
        写入的行），其余字段原样取自 submit_repo 返回值，可落
        head_branch / noop / pr_existed 粒度：
        - ok + noop             → status='noop'
        - ok 且提了/并入 PR      → status='submit_ok'
        - 失败                   → status='submit_failed'（含 stage/error）

        Returns:
            dict: {submit_ok, noop, failed} 计数。
        """
        counts = {"submit_ok_count": 0, "noop_count": 0, "failed_count": 0}
        with self._lock:
            for r in results or []:
                if r.get("ok"):
                    status = "noop" if r.get("noop") else "submit_ok"
                else:
                    status = "submit_failed"
                key = ("noop_count" if status == "noop"
                       else "submit_ok_count" if status == "submit_ok"
                       else "failed_count")
                counts[key] += 1
                pr_existed = r.get("pr_existed")
                self._conn.execute(
                    "UPDATE run_repos SET status = ?, stage = ?, error = ?,"
                    " head_branch = ?, pr_number = ?, pr_url = ?,"
                    " pr_existed = ? WHERE run_id = ? AND path = ?",
                    (status, r.get("stage"), r.get("error"),
                     r.get("head_branch"), r.get("pr_number"),
                     r.get("pr_url"),
                     None if pr_existed is None else int(bool(pr_existed)),
                     run_id, r.get("path")))
            self._conn.execute(
                "UPDATE runs SET submit_ok_count = ?, noop_count = ?,"
                " failed_count = ? WHERE id = ?",
                (counts["submit_ok_count"], counts["noop_count"],
                 counts["failed_count"], run_id))
            self._conn.commit()
        return {"submit_ok": counts["submit_ok_count"],
                "noop": counts["noop_count"],
                "failed": counts["failed_count"]}

    # ── PR 追踪 ──────────────────────────────────────────────

    def upsert_pr(self, org, repo, number, branch=None, path=None, url=None,
                  title=None, state="open", mergeable=None,
                  source="service", run_id=None, labels=None, draft=None,
                  created_at=None):
        """按 (org, repo, number) 复合唯一约束插入或更新 PR 记录。

        GitCode 的 PR 编号按仓库计数，不同仓库可以有相同 number，
        因此唯一键必须是三元组而非 number 单列。

        首次插入记 first_seen；每次调用刷新 last_polled。mergeable 三态：
        None 存 NULL。labels 为 [{"name", "color"}] 列表（序列化为
        labels_json），与 draft 同为展示层派生状态的原始信号；冲突更新时
        两者取 COALESCE（新值缺省为 None 时不覆盖已有信号）。
        created_at 为 PR 在 GitCode 上的真实创建时间，冲突更新时不覆盖
        已有值（创建时间不会变）。
        解析后的 token 值一律不经由此处落库。
        """
        now = _utcnow()
        mb = None if mergeable is None else int(bool(mergeable))
        labels_json = (json.dumps(labels, ensure_ascii=False)
                       if labels is not None else None)
        dr = None if draft is None else int(bool(draft))
        with self._lock:
            self._conn.execute(
                "INSERT INTO prs (org, repo, branch, number, path, url,"
                " title, state, mergeable, source, run_id, first_seen,"
                " last_polled, labels_json, draft, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(org, repo, number) DO UPDATE SET"
                " org = excluded.org, repo = excluded.repo,"
                " branch = excluded.branch, path = excluded.path,"
                " url = excluded.url, title = excluded.title,"
                " state = excluded.state, mergeable = excluded.mergeable,"
                " source = excluded.source, last_polled = excluded.last_polled,"
                " labels_json = COALESCE(excluded.labels_json, labels_json),"
                " draft = COALESCE(excluded.draft, draft),"
                " created_at = COALESCE(prs.created_at, excluded.created_at)",
                (org, repo, branch, number, path, url, title, state, mb,
                 source, run_id, now, now, labels_json, dr, created_at))
            self._conn.commit()

    def apply_pr_state(self, org, repo, number, state, mergeable=None,
                       merged_at=None, closed_at=None, labels=None,
                       draft=None, review_total=None, review_accepted=None,
                       created_at=None):
        """Poller 轮询后更新 PR 状态；merged/closed 时写对应时间戳。

        按 (org, repo, number) 三元组定位（PR 编号按仓库计数）。
        时间戳缺省取当前时间；merged_at/closed_at 传 None 表示不更新该列
        （state 为 merged/closed 时缺省自动写当前时间）。
        labels/draft/review_total/review_accepted 为派生状态原始信号，
        传 None 表示不更新该列（响应未带对应字段时不覆盖已有值）。
        created_at 为 PR 真实创建时间，只补空不覆盖。
        """
        now = _utcnow()
        if state == "merged" and merged_at is None:
            merged_at = now
        if state == "closed" and closed_at is None:
            closed_at = now
        mb = None if mergeable is None else int(bool(mergeable))
        fields = ["state = ?", "mergeable = ?", "last_polled = ?",
                  "merged_at = COALESCE(?, merged_at)",
                  "closed_at = COALESCE(?, closed_at)",
                  "created_at = COALESCE(created_at, ?)"]
        params = [state, mb, now, merged_at, closed_at, created_at]
        if labels is not None:
            fields.append("labels_json = ?")
            params.append(json.dumps(labels, ensure_ascii=False))
        if draft is not None:
            fields.append("draft = ?")
            params.append(int(bool(draft)))
        if review_total is not None:
            fields.append("review_total = ?")
            params.append(int(review_total))
        if review_accepted is not None:
            fields.append("review_accepted = ?")
            params.append(int(review_accepted))
        params.extend((org, repo, number))
        with self._lock:
            self._conn.execute(
                f"UPDATE prs SET {', '.join(fields)}"
                " WHERE org = ? AND repo = ? AND number = ?", params)
            self._conn.commit()

    def open_prs(self):
        """所有 state='open' 的 PR（Poller 轮询对象）。"""
        with self._lock:
            return self._all(
                "SELECT * FROM prs WHERE state = 'open' ORDER BY number")

    def list_prs(self, state=None):
        """PR 列表，可按状态过滤，最近轮询的在前。"""
        with self._lock:
            if state:
                return self._all(
                    "SELECT * FROM prs WHERE state = ?"
                    " ORDER BY last_polled DESC", (state,))
            return self._all(
                "SELECT * FROM prs ORDER BY last_polled DESC")

    def distinct_repos(self, run_days=30):
        """已知仓库清单（去重的 org/repo/branch/path），供 Poller 影子模式发现用。

        来源：prs 表全部行 + 最近 run_days 天内 run 的 run_repos 行（org/repo
        非空者）。按 (org, repo, branch) 去重，path 取首个非空值
        （Token 路由用）。结果按 (org, repo, branch) 排序，保证稳定。
        """
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=run_days)).isoformat()
        with self._lock:
            rows = self._all(
                "SELECT org, repo, branch, path FROM prs"
                " WHERE org IS NOT NULL AND repo IS NOT NULL"
                " UNION"
                " SELECT rr.org, rr.repo, rr.branch, rr.path"
                " FROM run_repos rr JOIN runs r ON rr.run_id = r.id"
                " WHERE rr.org IS NOT NULL AND rr.repo IS NOT NULL"
                " AND r.created_at >= ?", (cutoff,))
        seen = {}
        for row in rows:
            key = (row["org"], row["repo"], row["branch"])
            if key not in seen or (not seen[key]["path"] and row["path"]):
                seen[key] = row
        return sorted(seen.values(),
                      key=lambda r: (r["org"], r["repo"], r["branch"] or ""))

    def latest_repo_statuses(self):
        """每个 (org, repo, branch) 取最近一次 run 中的仓库状态（/prs 面板用）。

        以 run_repos.id 最大者为准（id 随 run 递增，等价于最新一次 run）；
        org/repo 为 NULL 的行（unchanged/skipped/error 等无目标仓信息）
        跳过。结果按 (org, repo, branch) 排序，保证稳定。

        Returns:
            list[dict]: org/repo/branch/status/new_count/update_count/
            pr_number/pr_url/run_id/finished_at（所属 run 的结束时间）。
        """
        with self._lock:
            return self._all(
                "SELECT rr.org, rr.repo, rr.branch, rr.status,"
                " rr.new_count, rr.update_count, rr.pr_number, rr.pr_url,"
                " rr.run_id, r.finished_at"
                " FROM run_repos rr JOIN runs r ON rr.run_id = r.id"
                " WHERE rr.org IS NOT NULL AND rr.repo IS NOT NULL"
                " AND rr.id = (SELECT MAX(rr2.id) FROM run_repos rr2"
                "              WHERE rr2.org = rr.org AND rr2.repo = rr.repo"
                "              AND rr2.branch IS rr.branch)"
                " ORDER BY rr.org, rr.repo, rr.branch")

    # ── 查询 ─────────────────────────────────────────────────

    def list_runs(self, limit=50):
        """最近的 run 列表（不含 log 大字段）。"""
        with self._lock:
            return self._all(
                "SELECT id, trigger, shadow, status, keyword, push_before,"
                " push_after, incremental, scanned_count, changed_count,"
                " unchanged_count, skipped_count, error_count,"
                " submit_ok_count, noop_count, failed_count, error,"
                " created_at, started_at, finished_at"
                " FROM runs ORDER BY id DESC LIMIT ?", (limit,))

    def get_run(self, run_id):
        """单个 run 全字段（含 log）；不存在返回 None。"""
        with self._lock:
            return self._one("SELECT * FROM runs WHERE id = ?", (run_id,))

    def get_run_repos(self, run_id):
        """某次 run 的全部仓库结果；files_json 解析为 files 列表。"""
        with self._lock:
            rows = self._all(
                "SELECT * FROM run_repos WHERE run_id = ? ORDER BY id",
                (run_id,))
        for row in rows:
            row["files"] = (json.loads(row["files_json"])
                            if row.get("files_json") else [])
            row.pop("files_json", None)
        return rows

    def summary(self):
        """Dashboard 顶部汇总：run/PR 计数与最近一次 run 时间。"""
        with self._lock:
            run_counts = {
                r["status"]: r["n"] for r in self._all(
                    "SELECT status, COUNT(*) AS n FROM runs GROUP BY status")}
            pr_counts = {
                r["state"]: r["n"] for r in self._all(
                    "SELECT state, COUNT(*) AS n FROM prs GROUP BY state")}
            last = self._one(
                "SELECT created_at, finished_at, status FROM runs"
                " ORDER BY id DESC LIMIT 1")
        return {
            "runs_total": sum(run_counts.values()),
            "runs_by_status": run_counts,
            "prs_total": sum(pr_counts.values()),
            "prs_by_state": pr_counts,
            "last_run": last,
        }

    # ── 仓库配置（repo_configs 表，设置页维护） ──────────────
    # 一个条目配齐一个仓库：启用开关（active 模式是否自动提 PR）+
    # 可选命名 token 引用（token_name → named_tokens）。粒度到仓库：
    # 该仓所有分支统一受控。

    def list_repo_configs(self):
        """全部仓库配置条目，按 (org, repo) 排序。"""
        with self._lock:
            return self._all(
                "SELECT * FROM repo_configs ORDER BY org, repo")

    def upsert_repo_config(self, org, repo, enabled=None, token_name=None):
        """新增或更新仓库配置条目（org/repo 小写规范化）；返回条目 id。

        enabled / token_name 传 None 表示该字段保持不变（新条目分别
        默认 True 与空串）；token_name 传空串表示清除命名 token 引用
        （回退 token_rules.json / 默认 token）。
        """
        org, repo = org.strip().lower(), repo.strip().lower()
        enabled_val = None if enabled is None else (1 if enabled else 0)
        name_val = None if token_name is None else token_name.strip().lower()
        now = _utcnow()
        with self._lock:
            row = self._one(
                "SELECT id FROM repo_configs WHERE org = ? AND repo = ?",
                (org, repo))
            if row is None:
                cur = self._conn.execute(
                    "INSERT INTO repo_configs (org, repo, enabled,"
                    " token_name, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (org, repo,
                     1 if enabled_val is None else enabled_val,
                     name_val or "", now, now))
                self._conn.commit()
                return cur.lastrowid
            self._conn.execute(
                "UPDATE repo_configs SET"
                " enabled = COALESCE(?, enabled),"
                " token_name = COALESCE(?, token_name), updated_at = ?"
                " WHERE id = ?",
                (enabled_val, name_val, now, row["id"]))
            self._conn.commit()
            return row["id"]

    def set_repo_config_enabled(self, entry_id, enabled):
        """按 id 切换条目开关；条目不存在返回 False。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE repo_configs SET enabled = ?, updated_at = ?"
                " WHERE id = ?",
                (1 if enabled else 0, _utcnow(), entry_id))
            self._conn.commit()
            return cur.rowcount > 0

    def delete_repo_config(self, entry_id):
        """按 id 删除条目；不存在返回 False。"""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM repo_configs WHERE id = ?", (entry_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def is_auto_pr_enabled(self, org, repo, branch=None):
        """runner 提交前门控：条目存在且启用才允许自动提 PR。

        粒度到仓库（branch 参数保留仅为调用方兼容，不参与判定）；
        无条目返回 False（默认拒绝）。org/repo 大小写不敏感（条目已
        规范化为小写，这里对查询值同样小写化）。
        """
        with self._lock:
            row = self._one(
                "SELECT enabled FROM repo_configs WHERE org = ? AND repo = ?",
                ((org or "").lower(), (repo or "").lower()))
        return bool(row and row["enabled"])

    def get_repo_token(self, org, repo):
        """仓库配置引用的命名 token 实际值；无条目/未配置/引用的 token
        已删除时返回 None（调用方回退 token_rules.json / 默认 token）。"""
        with self._lock:
            row = self._one(
                "SELECT t.token FROM repo_configs c"
                " JOIN named_tokens t ON t.name = c.token_name"
                " WHERE c.org = ? AND c.repo = ?",
                ((org or "").lower(), (repo or "").lower()))
        return row["token"] if row else None

    def token_name_exists(self, name):
        """命名 token 是否存在（API 校验 token_name 引用用）。"""
        with self._lock:
            return self._one(
                "SELECT 1 AS x FROM named_tokens WHERE name = ?",
                ((name or "").strip().lower(),)) is not None

    # ── 命名 Token 池（named_tokens，设置页维护） ─────────────
    # token 起名字保存，仓库配置通过 token_name 下拉引用；替代原先的
    # 关键字匹配规则，控制精确到仓库。

    def list_named_tokens(self):
        """全部命名 token 条目，按名称排序（含明文 token，展示层负责
        脱敏）。"""
        with self._lock:
            return self._all("SELECT * FROM named_tokens ORDER BY name")

    def upsert_named_token(self, name, token):
        """新增或更新命名 token（name 小写规范化、唯一）；返回条目 id。"""
        name, token = (name or "").strip().lower(), (token or "").strip()
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                "INSERT INTO named_tokens (name, token, created_at,"
                " updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(name) DO UPDATE SET"
                " token = excluded.token, updated_at = excluded.updated_at",
                (name, token, now, now))
            self._conn.commit()
            # 不依赖 cur.lastrowid：ON CONFLICT 走 UPDATE 分支时它是陈旧值
            row = self._one(
                "SELECT id FROM named_tokens WHERE name = ?", (name,))
            return row["id"]

    def delete_named_token(self, entry_id):
        """按 id 删除命名 token；不存在返回 False。

        引用它的仓库配置不清空（get_repo_token 查不到即回退默认路由，
        设置页显示引用失效）。
        """
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM named_tokens WHERE id = ?", (entry_id,))
            self._conn.commit()
            return cur.rowcount > 0

    # ── kv 配置（shadow 开关等） ─────────────────────────────

    def kv_get(self, key, default=None):
        """读 config 表；键不存在返回 default。"""
        with self._lock:
            row = self._one("SELECT value FROM config WHERE key = ?", (key,))
        return row["value"] if row is not None else default

    def kv_set(self, key, value):
        """写 config 表（值统一按字符串存）。"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO config (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)))
            self._conn.commit()

    # ── 清理 ─────────────────────────────────────────────────

    def cleanup_old_runs(self, days=90):
        """删除 days 天前创建的 run，级联删 run_repos，顺带清 orphan
        push_events（run_id 指向已不存在 run 的事件）。

        Returns:
            dict: 各表删除行数。
        """
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=days)).isoformat()
        with self._lock:
            old_ids = [r["id"] for r in self._all(
                "SELECT id FROM runs WHERE created_at < ?", (cutoff,))]
            if not old_ids:
                return {"runs": 0, "run_repos": 0, "push_events": 0}
            marks = ",".join("?" * len(old_ids))
            repos = self._conn.execute(
                f"DELETE FROM run_repos WHERE run_id IN ({marks})",
                old_ids).rowcount
            runs = self._conn.execute(
                f"DELETE FROM runs WHERE id IN ({marks})", old_ids).rowcount
            events = self._conn.execute(
                "DELETE FROM push_events WHERE run_id IS NOT NULL"
                " AND run_id NOT IN (SELECT id FROM runs)").rowcount
            self._conn.commit()
        return {"runs": runs, "run_repos": repos, "push_events": events}


def init_db(path):
    """打开（必要时创建）数据库并建表，幂等；父目录自动创建。

    Returns:
        Database: 就绪的数据库句柄。
    """
    path = str(path)
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    db = Database(path)
    with db._lock:
        db._conn.executescript(_SCHEMA)
        _migrate(db)
        db._conn.commit()
    return db


def _migrate(db):
    """轻量迁移：旧库 prs 表缺的新列用 ALTER TABLE 补齐（已有数据不丢）；
    repo_configs 旧版内联 token 列重建为 token_name 引用（非空 token 迁入
    named_tokens，命名 migrated-<org>-<repo>）；auto_pr/repo_tokens/
    path_lists/path_token_rules 等废弃表折叠或删除（幂等）。

    新库的建表语句已含全部列，此处对每列做存在性检查，幂等。
    """
    cols = {r["name"] for r in db._all("PRAGMA table_info(prs)")}
    for name, ddl in _PRS_EXTRA_COLUMNS.items():
        if name not in cols:
            db._conn.execute(f"ALTER TABLE prs ADD COLUMN {name} {ddl}")
    tables = {r["name"] for r in db._all(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    # repo_configs 旧版（含内联 token 列）重建为 token_name 引用；
    # 须在 auto_pr/repo_tokens 折叠之前执行（折叠写入的是新结构）
    rc_cols = {r["name"] for r in db._all("PRAGMA table_info(repo_configs)")}
    if "token" in rc_cols and "token_name" not in rc_cols:
        now = _utcnow()
        for r in db._all(
                "SELECT org, repo, token FROM repo_configs WHERE token != ''"):
            db._conn.execute(
                "INSERT INTO named_tokens (name, token, created_at,"
                " updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(name) DO UPDATE SET token = excluded.token",
                (f"migrated-{r['org']}-{r['repo']}", r["token"], now, now))
        db._conn.execute("ALTER TABLE repo_configs RENAME TO repo_configs_old")
        db._conn.execute(
            "CREATE TABLE repo_configs ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " org TEXT NOT NULL, repo TEXT NOT NULL,"
            " enabled INTEGER NOT NULL DEFAULT 1,"
            " token_name TEXT NOT NULL DEFAULT '',"
            " created_at TEXT, updated_at TEXT, UNIQUE(org, repo))")
        db._conn.execute(
            "INSERT INTO repo_configs (id, org, repo, enabled, token_name,"
            " created_at, updated_at)"
            " SELECT id, org, repo, enabled,"
            " CASE WHEN token != '' THEN 'migrated-' || org || '-' || repo"
            " ELSE '' END, created_at, updated_at FROM repo_configs_old")
        db._conn.execute("DROP TABLE repo_configs_old")
    if "auto_pr" in tables:
        # auto_pr（org,repo,branch 粒度）折叠为仓库粒度：同仓任一分支
        # 条目启用则仓库启用；org/repo 顺带小写规范化
        now = _utcnow()
        for r in db._all("SELECT org, repo, MAX(enabled) AS enabled"
                         " FROM auto_pr GROUP BY LOWER(org), LOWER(repo)"):
            db._conn.execute(
                "INSERT INTO repo_configs (org, repo, enabled, token_name,"
                " created_at, updated_at) VALUES (?, ?, ?, '', ?, ?)"
                " ON CONFLICT(org, repo) DO UPDATE SET"
                " enabled = MAX(repo_configs.enabled, excluded.enabled)",
                (r["org"].lower(), r["repo"].lower(), r["enabled"],
                 now, now))
        db._conn.execute("DROP TABLE auto_pr")
    if "repo_tokens" in tables:
        # repo_tokens 的内联 token 迁入 named_tokens 并让 repo_configs 引用
        now = _utcnow()
        for r in db._all("SELECT org, repo, token FROM repo_tokens"):
            org, repo = r["org"].lower(), r["repo"].lower()
            name = f"migrated-{org}-{repo}"
            db._conn.execute(
                "INSERT INTO named_tokens (name, token, created_at,"
                " updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(name) DO UPDATE SET token = excluded.token",
                (name, r["token"], now, now))
            db._conn.execute(
                "INSERT INTO repo_configs (org, repo, enabled, token_name,"
                " created_at, updated_at) VALUES (?, ?, 1, ?, ?, ?)"
                " ON CONFLICT(org, repo) DO UPDATE SET"
                " token_name = CASE WHEN repo_configs.token_name = ''"
                " THEN excluded.token_name ELSE repo_configs.token_name END",
                (org, repo, name, now, now))
        db._conn.execute("DROP TABLE repo_tokens")
    if "path_lists" in tables:
        # 黑/白名单功能已移除（与仓库配置开关重复），删旧表
        db._conn.execute("DROP TABLE path_lists")
    if "path_token_rules" in tables:
        # 关键字匹配规则已移除（改为命名 token + 仓库配置下拉引用），删旧表
        db._conn.execute("DROP TABLE path_token_rules")


# ── shadow 开关（真值存 config 表，env 只作初始值种子） ────────

SHADOW_KEY = "shadow"


def init_shadow(db, default=True):
    """库中尚无 shadow 记录时播种初始值（来自 Settings.shadow/env）。"""
    if db.kv_get(SHADOW_KEY) is None:
        set_shadow(db, default)


def is_shadow(db):
    """读影子开关运行时真值；无记录视为 True（安全优先）。"""
    return db.kv_get(SHADOW_KEY) != "0"


def set_shadow(db, enabled):
    """切换影子开关并持久化。"""
    db.kv_set(SHADOW_KEY, "1" if enabled else "0")
