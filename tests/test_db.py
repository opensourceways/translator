"""webapp.config / webapp.db：配置加载与提交任务状态层。"""

import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from webapp import config, db as db_mod


# ── Settings ─────────────────────────────────────────────────


class TestSettings:
    """环境变量加载与默认值。"""

    def test_defaults(self):
        s = config.Settings.from_env(env={})
        assert s.data_dir == "./data"
        assert s.port == 8000
        assert s.wait_timeout == 30
        assert s.max_paths == 200
        assert s.queue_max == 100
        assert s.retention_days == 90
        assert s.api_key == ""

    def test_from_env(self):
        s = config.Settings.from_env(env={
            "TR_DATA_DIR": "/data",
            "TR_PORT": "9000",
            "TR_WAIT_TIMEOUT": "5",
            "TR_MAX_PATHS": "10",
            "TR_QUEUE_MAX": "2",
            "TR_SUBMISSION_RETENTION_DAYS": "7",
            "TR_API_KEY": "  secret-key  ",
        })
        assert s.data_dir == "/data"
        assert (s.port, s.wait_timeout, s.max_paths, s.queue_max,
                s.retention_days) == (9000, 5, 10, 2, 7)
        assert s.api_key == "secret-key"

    @pytest.mark.parametrize("raw, expected", [
        ("abc", 30), (None, 30), ("", 30), (" 12 ", 12), ("-3", -3),
    ])
    def test_invalid_int_falls_back(self, raw, expected):
        s = config.Settings.from_env(env={"TR_WAIT_TIMEOUT": raw})
        assert s.wait_timeout == expected

    def test_paths(self):
        s = config.Settings(data_dir="/tmp/d")
        assert s.db_path == os.path.join("/tmp/d", "translator.db")
        assert s.work_dir == os.path.join("/tmp/d", "work")


# ── Database ─────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    database = db_mod.init_db(tmp_path / "translator.db")
    yield database
    database.close()


def _groups():
    return [{
        "org": "CANN", "repo": "hccl", "branch": "9.1.0",
        "source_paths": ["CANN/9.1.0/hccl/docs/a.md"],
        "files": [{"source_path": "CANN/9.1.0/hccl/docs/a.md",
                   "target_path": "docs/a.md"}],
    }]


class TestSubmissions:
    """任务与逐仓结果项的读写。"""

    def test_create_and_get(self, db):
        sid = db.create_submission(
            "req-1", ["CANN/9.1.0/hccl/docs/a.md"], _groups(), dry_run=False)
        assert sid > 0
        submission = db.get_submission(sid)
        assert submission["request_id"] == "req-1"
        assert submission["status"] == "queued"
        assert submission["dry_run"] == 0
        assert submission["path_count"] == 1
        assert json.loads(submission["paths_json"]) == [
            "CANN/9.1.0/hccl/docs/a.md"]
        assert submission["created_at"]
        assert submission["started_at"] is None
        assert db.get_submission_by_request_id("req-1") == submission
        assert db.get_submission_by_request_id("req-x") is None
        assert db.get_submission(999) is None

    def test_create_dry_run(self, db):
        sid = db.create_submission("req-2", ["CANN/9.1.0/hccl/docs/a.md"],
                                   _groups(), dry_run=True)
        assert db.get_submission(sid)["dry_run"] == 1

    def test_request_id_unique(self, db):
        db.create_submission("dup", ["CANN/9.1.0/hccl/docs/a.md"], _groups())
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError):
            db.create_submission("dup", ["CANN/9.1.0/hccl/docs/a.md"],
                                 _groups())

    def test_items_parse_json(self, db):
        sid = db.create_submission(
            "req-3", ["CANN/9.1.0/hccl/docs/a.md"], _groups())
        items = db.get_submission_items(sid)
        assert len(items) == 1
        item = items[0]
        assert (item["org"], item["repo"], item["branch"]) == \
            ("CANN", "hccl", "9.1.0")
        assert item["status"] == "queued"
        assert item["source_paths"] == ["CANN/9.1.0/hccl/docs/a.md"]
        assert item["files"] == [{
            "source_path": "CANN/9.1.0/hccl/docs/a.md",
            "target_path": "docs/a.md"}]
        assert "source_paths_json" not in item
        assert "files_json" not in item

    def test_mark_running(self, db):
        sid = db.create_submission("req-4", ["CANN/9.1.0/hccl/docs/a.md"],
                                   _groups())
        db.mark_submission_running(sid)
        assert db.get_submission(sid)["status"] == "running"
        assert db.get_submission(sid)["started_at"]

    def test_update_item_whitelist(self, db):
        sid = db.create_submission("req-5", ["CANN/9.1.0/hccl/docs/a.md"],
                                   _groups())
        item_id = db.get_submission_items(sid)[0]["id"]
        db.update_submission_item(
            item_id, status="submitted", pr_number=12,
            pr_url="https://atomgit.com/CANN/hccl/merge_requests/12",
            pr_title="标题", pr_existed=0, stage=None, error=None,
            unknown_field="ignored")
        item = db.get_submission_items(sid)[0]
        assert item["status"] == "submitted"
        assert item["pr_number"] == 12
        assert item["pr_existed"] == 0
        assert item["pr_title"] == "标题"
        assert db.get_items_with_pr(sid)[0]["pr_number"] == 12

    def test_update_item_without_fields_is_noop(self, db):
        sid = db.create_submission("req-6", ["CANN/9.1.0/hccl/docs/a.md"],
                                   _groups())
        item_id = db.get_submission_items(sid)[0]["id"]
        before = db.get_submission_items(sid)[0]
        db.update_submission_item(item_id, nope=1)
        assert db.get_submission_items(sid)[0] == before

    def test_get_items_with_pr_filters(self, db):
        groups = _groups() + [{
            "org": "CANN", "repo": "sip", "branch": "9.1.0",
            "source_paths": ["CANN/9.1.0/sip/docs/a.md"],
            "files": [{"source_path": "CANN/9.1.0/sip/docs/a.md",
                       "target_path": "docs/a.md"}],
        }]
        sid = db.create_submission(
            "req-7", ["CANN/9.1.0/hccl/docs/a.md",
                      "CANN/9.1.0/sip/docs/a.md"], groups)
        items = db.get_submission_items(sid)
        db.update_submission_item(items[0]["id"], status="submitted",
                                  pr_number=1)
        assert [r["repo"] for r in db.get_items_with_pr(sid)] == ["hccl"]

    def test_fail_queued_items_keeps_done_items(self, db):
        sid = db.create_submission("req-8", ["CANN/9.1.0/hccl/docs/a.md"],
                                   _groups())
        item_id = db.get_submission_items(sid)[0]["id"]
        db.update_submission_item(item_id, status="submitted")
        db.fail_queued_items(sid, "boom")
        assert db.get_submission_items(sid)[0]["status"] == "submitted"
        sid2 = db.create_submission("req-9", ["CANN/9.1.0/hccl/docs/a.md"],
                                    _groups())
        db.fail_queued_items(sid2, "boom")
        item = db.get_submission_items(sid2)[0]
        assert item["status"] == "failed"
        assert item["error"] == "boom"

    def test_finish_submission(self, db):
        sid = db.create_submission("req-10", ["CANN/9.1.0/hccl/docs/a.md"],
                                   _groups())
        db.finish_submission(sid, "failed", error="x")
        submission = db.get_submission(sid)
        assert submission["status"] == "failed"
        assert submission["error"] == "x"
        assert submission["finished_at"]

    @pytest.mark.parametrize("status, expected", [
        ("success", True), ("partial", True), ("failed", True),
        ("queued", False), ("running", False),
    ])
    def test_is_terminal(self, db, status, expected):
        assert db.is_terminal(status) is expected

    def test_append_log_and_get(self, db):
        sid = db.create_submission("req-11", ["CANN/9.1.0/hccl/docs/a.md"],
                                   _groups())
        db.append_log(sid, "line1\n")
        db.append_log(sid, "line2\n")
        assert db.get_submission_log(sid) == "line1\nline2\n"
        db.append_log(sid, "")  # 空串不写
        assert db.get_submission_log(sid) == "line1\nline2\n"

    def test_append_log_truncates_tail(self, db):
        sid = db.create_submission("req-12", ["CANN/9.1.0/hccl/docs/a.md"],
                                   _groups())
        db.append_log(sid, "a" * (db_mod.LOG_TAIL_LIMIT + 1000))
        log = db.get_submission_log(sid)
        assert len(log.encode()) <= db_mod.LOG_TAIL_LIMIT
        assert log == "a" * db_mod.LOG_TAIL_LIMIT

    def test_append_log_unknown_submission_is_noop(self, db):
        db.append_log(999, "x")

    def test_counts(self, db):
        assert (db.pending_count(), db.running_count()) == (0, 0)
        s1 = db.create_submission("req-13", ["CANN/9.1.0/hccl/docs/a.md"],
                                  _groups())
        s2 = db.create_submission("req-14", ["CANN/9.1.0/hccl/docs/a.md"],
                                  _groups())
        assert db.pending_count() == 2
        db.mark_submission_running(s1)
        assert (db.pending_count(), db.running_count()) == (1, 1)
        db.finish_submission(s2, "success")
        assert (db.pending_count(), db.running_count()) == (0, 1)

    def test_fail_unfinished_submissions(self, db):
        queued = db.create_submission("req-15", ["CANN/9.1.0/hccl/docs/a.md"],
                                      _groups())
        running = db.create_submission("req-16", ["CANN/9.1.0/hccl/docs/a.md"],
                                       _groups())
        done = db.create_submission("req-17", ["CANN/9.1.0/hccl/docs/a.md"],
                                    _groups())
        db.mark_submission_running(running)
        db.finish_submission(done, "success")
        assert db.fail_unfinished_submissions("restart") == 2
        for sid in (queued, running):
            submission = db.get_submission(sid)
            assert submission["status"] == "failed"
            assert submission["error"] == "restart"
            assert submission["finished_at"]
        assert db.get_submission(done)["status"] == "success"
        assert db.get_submission_items(queued)[0]["status"] == "failed"

    def test_cleanup_old_submissions(self, db):
        old = db.create_submission("old", ["CANN/9.1.0/hccl/docs/a.md"],
                                   _groups())
        fresh = db.create_submission("fresh", ["CANN/9.1.0/hccl/docs/a.md"],
                                     _groups())
        old_time = (datetime.now(timezone.utc)
                    - timedelta(days=120)).isoformat()
        with db._lock:
            db._conn.execute("UPDATE submissions SET created_at = ?"
                             " WHERE id = ?", (old_time, old))
            db._conn.commit()
        result = db.cleanup_old_submissions(days=90)
        assert result == {"submissions": 1, "items": 1}
        assert db.get_submission(old) is None
        assert db.get_submission_items(old) == []
        assert db.get_submission(fresh) is not None
        assert db.cleanup_old_submissions(days=90) == \
            {"submissions": 0, "items": 0}


class TestInitDb:
    def test_creates_parent_and_idempotent(self, tmp_path):
        path = tmp_path / "nested" / "dir" / "translator.db"
        db1 = db_mod.init_db(path)
        db1.create_submission("req", ["CANN/9.1.0/hccl/docs/a.md"], _groups())
        db1.close()
        db2 = db_mod.init_db(path)  # 重复初始化不报错，数据保留
        assert db2.get_submission_by_request_id("req") is not None
        db2.close()

    def test_wal_mode(self, db):
        with db._lock:
            mode = db._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
