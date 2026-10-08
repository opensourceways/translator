"""webapp.config / webapp.db 的单元测试。

全部使用 tmp_path 上的临时 SQLite 库，不触网、不依赖外部状态。
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from webapp import config, db as db_mod

TC_VARS = ["TC_DATA_DIR", "TC_PORT", "TC_POLL_INTERVAL", "TC_WEBHOOK_TOKEN",
           "TC_ADMIN_TOKEN", "TC_DASH_TOKEN", "TC_SHADOW"]


@pytest.fixture()
def db(tmp_path):
    """临时库句柄，测试结束关闭。"""
    d = db_mod.init_db(tmp_path / "data" / "test.db")
    yield d
    d.close()


def _clear_tc_env(monkeypatch):
    for name in TC_VARS:
        monkeypatch.delenv(name, raising=False)


# ── config.Settings ──────────────────────────────────────────


class TestSettings:
    def test_from_env_defaults(self, monkeypatch):
        _clear_tc_env(monkeypatch)
        s = config.Settings.from_env()
        assert s.data_dir == "./data"
        assert s.port == 8000
        assert s.poll_interval == 300
        assert s.webhook_token == ""
        assert s.admin_token == ""
        assert s.dash_token == ""
        assert s.shadow is True  # 默认影子模式，安全优先

    def test_from_env_values(self, monkeypatch):
        _clear_tc_env(monkeypatch)
        monkeypatch.setenv("TC_DATA_DIR", "/srv/tc")
        monkeypatch.setenv("TC_PORT", "9000")
        monkeypatch.setenv("TC_POLL_INTERVAL", "60")
        monkeypatch.setenv("TC_WEBHOOK_TOKEN", "wh")
        monkeypatch.setenv("TC_ADMIN_TOKEN", "adm")
        monkeypatch.setenv("TC_DASH_TOKEN", "dash")
        monkeypatch.setenv("TC_SHADOW", "false")
        s = config.Settings.from_env()
        assert s.data_dir == "/srv/tc"
        assert s.port == 9000
        assert s.poll_interval == 60
        assert (s.webhook_token, s.admin_token, s.dash_token) == \
            ("wh", "adm", "dash")
        assert s.shadow is False

    @pytest.mark.parametrize("raw,expected", [
        ("true", True), ("1", True), ("yes", True), ("ON", True),
        ("false", False), ("0", False), ("off", False),
        ("garbage", True),   # 无法识别回落默认 true
        ("", True),
    ])
    def test_shadow_parse(self, monkeypatch, raw, expected):
        _clear_tc_env(monkeypatch)
        monkeypatch.setenv("TC_SHADOW", raw)
        assert config.Settings.from_env().shadow is expected

    def test_invalid_int_falls_back(self, monkeypatch):
        _clear_tc_env(monkeypatch)
        monkeypatch.setenv("TC_PORT", "not-a-number")
        monkeypatch.setenv("TC_POLL_INTERVAL", "")
        s = config.Settings.from_env()
        assert s.port == 8000
        assert s.poll_interval == 300

    def test_paths(self):
        s = config.Settings(data_dir="/x")
        assert s.db_path == "/x/translator-ci.db"
        assert s.work_dir == "/x/work"


# ── shadow 开关（kv 存取） ────────────────────────────────────


class TestShadow:
    def test_default_true_when_unset(self, db):
        assert db_mod.is_shadow(db) is True

    def test_seed_only_when_absent(self, db):
        db_mod.init_shadow(db, default=False)
        assert db_mod.is_shadow(db) is False
        # 已有记录时播种不覆盖（env 只作初始值种子）
        db_mod.init_shadow(db, default=True)
        assert db_mod.is_shadow(db) is False

    def test_set_shadow_persists(self, tmp_path):
        path = tmp_path / "d" / "t.db"
        d1 = db_mod.init_db(path)
        db_mod.set_shadow(d1, False)
        d1.close()
        # 重开后仍然是关闭状态（运行时切换持久化）
        d2 = db_mod.init_db(path)
        assert db_mod.is_shadow(d2) is False
        d2.close()


# ── 建表与 kv ────────────────────────────────────────────────


class TestInitAndKv:
    def test_init_idempotent(self, tmp_path):
        path = tmp_path / "d" / "t.db"
        d1 = db_mod.init_db(path)
        d1.kv_set("k", "v")
        d1.close()
        # 再次 init_db 不报错且数据保留
        d2 = db_mod.init_db(path)
        assert d2.kv_get("k") == "v"
        d2.close()

    # 加派生状态信号列之前的 prs 建表语句（旧库形态）
    _OLD_PRS_SCHEMA = """
    CREATE TABLE prs (
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
        UNIQUE(org, repo, number)
    );
    """

    def test_migrate_old_prs_table(self, tmp_path):
        """旧库（prs 无信号列）init_db 后列补齐且已有数据不丢，可正常读写。"""
        path = tmp_path / "d" / "t.db"
        path.parent.mkdir(parents=True)
        conn = sqlite3.connect(path)
        conn.execute(self._OLD_PRS_SCHEMA)
        conn.execute("INSERT INTO prs (org, repo, number, state, mergeable)"
                     " VALUES ('o1', 'r1', 1, 'open', 1)")
        conn.commit()
        conn.close()

        d = db_mod.init_db(path)
        row = d.list_prs()[0]
        assert row["state"] == "open" and row["mergeable"] == 1  # 数据不丢
        assert row["labels_json"] is None and row["draft"] is None
        assert row["review_total"] is None
        assert row["review_accepted"] is None
        # 新列可正常写入
        d.apply_pr_state("o1", "r1", 1, "open",
                         labels=[{"name": "ci-pipeline-failed", "color": None}],
                         draft=False, review_total=2, review_accepted=1)
        row = d.list_prs()[0]
        assert json.loads(row["labels_json"])[0]["name"] == \
            "ci-pipeline-failed"
        assert (row["draft"], row["review_total"], row["review_accepted"]) == \
            (0, 2, 1)
        d.close()
        # 迁移幂等：重复 init 不报错、数据保留
        d2 = db_mod.init_db(path)
        assert d2.list_prs()[0]["review_total"] == 2
        d2.close()

    def test_kv_get_default(self, db):
        assert db.kv_get("missing") is None
        assert db.kv_get("missing", "d") == "d"

    def test_kv_overwrite(self, db):
        db.kv_set("a", "1")
        db.kv_set("a", "2")
        assert db.kv_get("a") == "2"


# ── run 生命周期 ─────────────────────────────────────────────


class TestRunLifecycle:
    def test_create_run_defaults(self, db):
        run_id = db.create_run("webhook", True, push_before="aaa",
                               push_after="bbb")
        run = db.get_run(run_id)
        assert run["trigger"] == "webhook"
        assert run["shadow"] == 1
        assert run["status"] == "running"
        assert run["push_before"] == "aaa"
        assert run["push_after"] == "bbb"
        assert run["finished_at"] is None
        # 时间戳为 UTC ISO8601 且可解析
        datetime.fromisoformat(run["created_at"])
        assert run["created_at"].endswith("+00:00")

    def test_mark_success_sets_finished(self, db):
        run_id = db.create_run("manual", False)
        db.mark_run(run_id, "success")
        run = db.get_run(run_id)
        assert run["status"] == "success"
        assert run["finished_at"] is not None

    def test_mark_failed_with_error(self, db):
        run_id = db.create_run("manual", False)
        db.mark_run(run_id, "failed", error="克隆来源仓失败")
        run = db.get_run(run_id)
        assert run["status"] == "failed"
        assert run["error"] == "克隆来源仓失败"
        assert run["finished_at"] is not None

    def test_mark_incremental(self, db):
        run_id = db.create_run("webhook", True)
        db.mark_run(run_id, incremental=True)
        assert db.get_run(run_id)["incremental"] == 1

    def test_interrupt_running_runs(self, db):
        r1 = db.create_run("webhook", True)
        r2 = db.create_run("webhook", True)
        db.mark_run(r2, "success")
        n = db.interrupt_running_runs()
        assert n == 1
        assert db.get_run(r1)["status"] == "interrupted"
        assert db.get_run(r1)["finished_at"] is not None
        assert db.get_run(r2)["status"] == "success"

    def test_list_runs_excludes_log(self, db):
        run_id = db.create_run("webhook", True)
        db.append_log(run_id, "some log\n")
        db.mark_run(run_id, "success")
        runs = db.list_runs()
        assert len(runs) == 1
        assert runs[0]["id"] == run_id
        assert "log" not in runs[0]

    def test_get_run_missing(self, db):
        assert db.get_run(999) is None


# ── 日志追加与截断 ────────────────────────────────────────────


class TestAppendLog:
    def test_append_accumulates(self, db):
        run_id = db.create_run("manual", False)
        db.append_log(run_id, "第一行\n")
        db.append_log(run_id, "第二行\n")  # 模拟 runner 批量刷写的一段
        log = db.get_run(run_id)["log"]
        assert log == "第一行\n第二行\n"

    def test_append_empty_noop(self, db):
        run_id = db.create_run("manual", False)
        db.append_log(run_id, "")
        assert db.get_run(run_id)["log"] == ""

    def test_truncation_keeps_tail(self, db):
        run_id = db.create_run("manual", False)
        chunk = "x" * (150 * 1024) + "\n"
        db.append_log(run_id, chunk)
        db.append_log(run_id, chunk)  # 累计 300KB，超过 200KB 上限
        log = db.get_run(run_id)["log"]
        assert len(log.encode("utf-8")) <= db_mod.LOG_TAIL_LIMIT
        assert log.endswith(chunk)  # 保留的是尾部

    def test_truncation_multibyte_safe(self, db):
        """截断不应切断 UTF-8 多字节字符产生乱码。"""
        run_id = db.create_run("manual", False)
        db.append_log(run_id, "译" * (120 * 1024))  # 360KB 全中文
        log = db.get_run(run_id)["log"]
        assert len(log.encode("utf-8")) <= db_mod.LOG_TAIL_LIMIT
        assert log  # 完整解码无替换字符
        assert "\ufffd" not in log


# ── 扫描/提交结果落库 ─────────────────────────────────────────


def _scan_result():
    """模拟 scan_changed 的返回。"""
    return {
        "ok": True,
        "changed": [{
            "project": {"path": "Ascend/26.1.0/MindStudio/msprof"},
            "org": "Ascend", "repo": "msprof", "branch": "26.1.0",
            "files": [{"target_path": "docs/en/a.md", "content_b64": "QQ=="},
                      {"target_path": "docs/en/b.md", "content_b64": "Qg=="}],
            "new_count": 1, "update_count": 1,
        }],
        "unchanged": ["Ascend/26.1.0/CANN/toolkit"],
        "skipped": [{"path": "Kunpeng/26.1.0/x/y",
                     "reason": "在拉黑名单中，业务侧暂不处理"}],
        "errors": [{"path": "Ascend/26.1.0/z/w", "error": "下载译稿失败"}],
        "incremental": False,
    }


class TestRecordScanResults:
    def test_rows_and_counts(self, db):
        run_id = db.create_run("webhook", True)
        counts = db.record_scan_results(run_id, _scan_result())
        assert counts == {
            "scanned_count": 4, "changed_count": 1,
            "unchanged_count": 1, "skipped_count": 1, "error_count": 1,
        }
        run = db.get_run(run_id)
        assert run["changed_count"] == 1
        assert run["scanned_count"] == 4

        repos = {r["path"]: r for r in db.get_run_repos(run_id)}
        changed = repos["Ascend/26.1.0/MindStudio/msprof"]
        assert changed["status"] == "changed"  # active 模式
        assert changed["org"] == "Ascend"
        assert changed["new_count"] == 1
        assert changed["update_count"] == 1
        # files_json 只存 target_path，剥离 content_b64
        assert changed["files"] == ["docs/en/a.md", "docs/en/b.md"]
        assert "content_b64" not in json.dumps(changed, ensure_ascii=False)

        assert repos["Ascend/26.1.0/CANN/toolkit"]["status"] == "unchanged"
        skipped = repos["Kunpeng/26.1.0/x/y"]
        assert skipped["status"] == "skipped"
        assert skipped["reason"] == "在拉黑名单中，业务侧暂不处理"
        err = repos["Ascend/26.1.0/z/w"]
        assert err["status"] == "error"
        assert err["error"] == "下载译稿失败"

    def test_shadow_marks_would_submit(self, db):
        run_id = db.create_run("webhook", True)
        db.record_scan_results(run_id, _scan_result(), shadow=True)
        repos = {r["path"]: r for r in db.get_run_repos(run_id)}
        assert repos["Ascend/26.1.0/MindStudio/msprof"]["status"] == \
            "would_submit"

    def test_empty_scan(self, db):
        run_id = db.create_run("webhook", True)
        counts = db.record_scan_results(run_id, {"ok": True})
        assert counts["scanned_count"] == 0
        assert db.get_run_repos(run_id) == []


class TestRecordSubmitResults:
    def _run_with_changed(self, db):
        run_id = db.create_run("webhook", False)
        db.record_scan_results(run_id, _scan_result())
        return run_id

    def test_submit_ok_with_pr(self, db):
        run_id = self._run_with_changed(db)
        db.record_submit_results(run_id, [{
            "path": "Ascend/26.1.0/MindStudio/msprof",
            "ok": True, "pr_existed": False, "pr_number": 12,
            "pr_url": "https://gitcode.com/Ascend/msprof/merge_requests/12",
            "head_branch": "translator/auto-26.1.0",
        }])
        row = db.get_run_repos(run_id)[0]
        assert row["status"] == "submit_ok"
        assert row["head_branch"] == "translator/auto-26.1.0"
        assert row["pr_number"] == 12
        assert row["pr_url"].endswith("/12")
        assert row["pr_existed"] == 0
        assert db.get_run(run_id)["submit_ok_count"] == 1

    def test_noop_and_pr_existed(self, db):
        run_id = self._run_with_changed(db)
        db.record_submit_results(run_id, [{
            "path": "Ascend/26.1.0/MindStudio/msprof",
            "ok": True, "noop": True,
            "head_branch": "translator/auto-26.1.0",
        }])
        row = db.get_run_repos(run_id)[0]
        assert row["status"] == "noop"
        assert row["head_branch"] == "translator/auto-26.1.0"
        assert row["pr_number"] is None
        assert row["pr_existed"] is None  # 未提供保持 NULL
        assert db.get_run(run_id)["noop_count"] == 1

    def test_submit_failed(self, db):
        run_id = self._run_with_changed(db)
        db.record_submit_results(run_id, [{
            "path": "Ascend/26.1.0/MindStudio/msprof",
            "ok": False, "stage": "create_pr", "error": "403 Forbidden",
            "head_branch": "translator/auto-26.1.0",
        }])
        row = db.get_run_repos(run_id)[0]
        assert row["status"] == "submit_failed"
        assert row["stage"] == "create_pr"
        assert row["error"] == "403 Forbidden"
        assert db.get_run(run_id)["failed_count"] == 1


# ── 每仓最新状态（/prs 面板） ─────────────────────────────────


def _changed_scan(org, repo, branch, new=1, upd=0):
    """模拟只含一个 changed 仓库的 scan_changed 返回。"""
    return {"ok": True, "changed": [{
        "project": {"path": f"{org}/{branch}/{repo}"},
        "org": org, "repo": repo, "branch": branch, "files": [],
        "new_count": new, "update_count": upd}],
        "unchanged": [], "skipped": [], "errors": []}


class TestLatestRepoStatuses:
    def test_empty(self, db):
        assert db.latest_repo_statuses() == []

    def test_null_org_repo_rows_skipped(self, db):
        """unchanged/skipped/error 行 org/repo 为 NULL，不产生状态行。"""
        run_id = db.create_run("webhook", True)
        db.record_scan_results(run_id, {
            "ok": True, "changed": [], "unchanged": ["Ascend/x/y"],
            "skipped": [{"path": "a/b", "reason": "黑名单"}],
            "errors": [{"path": "c/d", "error": "e"}]})
        assert db.latest_repo_statuses() == []

    def test_latest_across_runs(self, db):
        """同一 (org, repo, branch) 跨多次 run：取最近一次的状态；
        未参与新 run 的仓库保留其最近一次 run 的状态。"""
        r1 = db.create_run("webhook", True)
        db.record_scan_results(
            r1, _changed_scan("Ascend", "msprof", "26.1.0"), shadow=True)
        db.record_scan_results(
            r1, _changed_scan("Ascend", "toolkit", "26.1.0"), shadow=True)
        db.mark_run(r1, "success")
        r2 = db.create_run("webhook", True)
        db.record_scan_results(
            r2, _changed_scan("Ascend", "msprof", "26.1.0", new=2, upd=3))
        db.record_submit_results(r2, [{
            "path": "Ascend/26.1.0/msprof", "ok": True, "pr_existed": False,
            "pr_number": 12, "pr_url": "https://gitcode.com/x/12"}])
        db.mark_run(r2, "success")

        rows = {(row["repo"]): row for row in db.latest_repo_statuses()}
        assert set(rows) == {"msprof", "toolkit"}
        latest = rows["msprof"]
        assert latest["run_id"] == r2
        assert latest["status"] == "submit_ok"
        assert latest["new_count"] == 2 and latest["update_count"] == 3
        assert latest["pr_number"] == 12
        assert latest["pr_url"] == "https://gitcode.com/x/12"
        assert latest["finished_at"] == db.get_run(r2)["finished_at"]
        # toolkit 未参与 r2，保留 r1 的影子状态
        assert rows["toolkit"]["run_id"] == r1
        assert rows["toolkit"]["status"] == "would_submit"

    def test_branches_are_separate_rows(self, db):
        """同仓不同 branch 各自成行，按 (org, repo, branch) 稳定排序。"""
        r1 = db.create_run("webhook", True)
        db.record_scan_results(
            r1, _changed_scan("Ascend", "msprof", "master"), shadow=True)
        db.record_scan_results(
            r1, _changed_scan("Ascend", "msprof", "26.1.0"), shadow=True)
        rows = db.latest_repo_statuses()
        assert [(r["repo"], r["branch"]) for r in rows] == [
            ("msprof", "26.1.0"), ("msprof", "master")]


# ── PR 追踪 ──────────────────────────────────────────────────


class TestPrs:
    def test_upsert_insert(self, db):
        db.upsert_pr("Ascend", "msprof", 12, branch="26.1.0",
                     path="Ascend/26.1.0/MindStudio/msprof",
                     url="https://gitcode.com/x/12", title="译稿更新",
                     source="service", run_id=1)
        prs = db.open_prs()
        assert len(prs) == 1
        pr = prs[0]
        assert pr["number"] == 12
        assert pr["state"] == "open"
        assert pr["mergeable"] is None  # 三态：None 存 NULL
        assert pr["source"] == "service"
        assert pr["first_seen"] is not None
        assert pr["last_polled"] is not None

    def test_upsert_updates_keeps_first_seen(self, db):
        db.upsert_pr("Ascend", "msprof", 12, title="旧标题")
        first_seen = db.open_prs()[0]["first_seen"]
        db.upsert_pr("Ascend", "msprof", 12, title="新标题",
                     mergeable=True, source="jenkins")
        pr = db.open_prs()[0]
        assert pr["title"] == "新标题"
        assert pr["mergeable"] == 1
        assert pr["source"] == "jenkins"
        assert pr["first_seen"] == first_seen  # first_seen 不被覆盖

    def test_apply_pr_state_merged(self, db):
        db.upsert_pr("Ascend", "msprof", 12)
        db.apply_pr_state("Ascend", "msprof", 12, "merged",
                          merged_at="2026-09-29T08:00:00+00:00")
        pr = db.list_prs("merged")[0]
        assert pr["state"] == "merged"
        assert pr["merged_at"] == "2026-09-29T08:00:00+00:00"
        assert db.open_prs() == []

    def test_apply_pr_state_closed_default_timestamp(self, db):
        db.upsert_pr("Ascend", "msprof", 13)
        db.apply_pr_state("Ascend", "msprof", 13, "closed")
        pr = db.list_prs("closed")[0]
        assert pr["closed_at"] is not None  # 缺省写当前时间

    def test_apply_pr_state_mergeable_tristate(self, db):
        db.upsert_pr("Ascend", "msprof", 14, mergeable=False)
        assert db.open_prs()[0]["mergeable"] == 0
        db.apply_pr_state("Ascend", "msprof", 14, "open", mergeable=True)
        assert db.open_prs()[0]["mergeable"] == 1

    def test_open_to_closed_to_merged_keeps_timestamps(self, db):
        """状态迁移不丢已写入的时间戳（COALESCE 语义）。"""
        db.upsert_pr("Ascend", "msprof", 15)
        db.apply_pr_state("Ascend", "msprof", 15, "open")
        db.apply_pr_state("Ascend", "msprof", 15, "merged",
                          merged_at="2026-09-29T09:00:00+00:00")
        db.apply_pr_state("Ascend", "msprof", 15, "closed")  # 异常再迁移也不覆盖 merged_at
        pr = db.list_prs()[0]
        assert pr["merged_at"] == "2026-09-29T09:00:00+00:00"
        assert pr["closed_at"] is not None

    def test_same_number_across_repos_no_conflict(self, db):
        """回归：PR 编号按仓库计数，不同仓库相同 number 互不覆盖。"""
        db.upsert_pr("Ascend", "msprof", 12, title="仓 A 的 PR")
        db.upsert_pr("Kunpeng", "boostkit", 12, title="仓 B 的 PR")
        prs = {(p["org"], p["repo"]): p for p in db.open_prs()}
        assert len(prs) == 2
        assert prs[("Ascend", "msprof")]["title"] == "仓 A 的 PR"
        assert prs[("Kunpeng", "boostkit")]["title"] == "仓 B 的 PR"
        # 更新一个仓库的 PR 不影响另一个
        db.apply_pr_state("Ascend", "msprof", 12, "merged")
        assert db.list_prs("merged")[0]["repo"] == "msprof"
        open_prs = db.open_prs()
        assert len(open_prs) == 1
        assert open_prs[0]["repo"] == "boostkit"
        assert open_prs[0]["title"] == "仓 B 的 PR"


# ── 派生状态原始信号列 ────────────────────────────────────────


class TestPrSignals:
    def test_upsert_with_labels_and_draft(self, db):
        """upsert 带 labels/draft 落库；再 upsert 不带时不覆盖已有信号。"""
        db.upsert_pr("Ascend", "msprof", 12,
                     labels=[{"name": "ci-pipeline-failed",
                              "color": "#DB2828"}],
                     draft=True)
        row = db.list_prs()[0]
        labels = json.loads(row["labels_json"])
        assert labels == [{"name": "ci-pipeline-failed", "color": "#DB2828"}]
        assert row["draft"] == 1
        # 再次 upsert 不带信号：COALESCE 保留已有值
        db.upsert_pr("Ascend", "msprof", 12, title="新标题")
        row = db.list_prs()[0]
        assert row["title"] == "新标题"
        assert json.loads(row["labels_json"])[0]["name"] == \
            "ci-pipeline-failed"
        assert row["draft"] == 1

    def test_apply_pr_state_signals_roundtrip(self, db):
        """apply_pr_state 落 labels/draft/评审计数；缺省 None 不覆盖。"""
        db.upsert_pr("Ascend", "msprof", 12)
        db.apply_pr_state("Ascend", "msprof", 12, "open",
                          labels=[{"name": "cann-cla/no", "color": "#DB2828"}],
                          draft=False, review_total=3, review_accepted=1)
        row = db.list_prs()[0]
        assert json.loads(row["labels_json"])[0]["name"] == "cann-cla/no"
        assert (row["draft"], row["review_total"],
                row["review_accepted"]) == (0, 3, 1)
        # 普通状态更新不带信号：已有信号保留
        db.apply_pr_state("Ascend", "msprof", 12, "open", mergeable=True)
        row = db.list_prs()[0]
        assert row["mergeable"] == 1
        assert row["review_total"] == 3 and row["review_accepted"] == 1
        assert row["labels_json"] is not None
        # 评审计数可更新（部分通过 → 全部通过）
        db.apply_pr_state("Ascend", "msprof", 12, "open",
                          review_total=3, review_accepted=3)
        assert db.list_prs()[0]["review_accepted"] == 3


# ── push 事件 ────────────────────────────────────────────────


class TestPushEvents:
    def test_record_and_link(self, db):
        eid = db.record_push_event("refs/heads/main", "aaa", "bbb", True)
        run_id = db.create_run("webhook", True)
        db.link_push_event(eid, run_id)
        row = db._one("SELECT * FROM push_events WHERE id = ?", (eid,))
        assert row["accepted"] == 1
        assert row["run_id"] == run_id

    def test_rejected_event(self, db):
        eid = db.record_push_event("refs/heads/dev", "a", "b", False)
        row = db._one("SELECT * FROM push_events WHERE id = ?", (eid,))
        assert row["accepted"] == 0
        assert row["run_id"] is None


# ── 汇总与清理 ────────────────────────────────────────────────


class TestSummaryAndCleanup:
    def test_summary(self, db):
        r1 = db.create_run("webhook", True)
        db.mark_run(r1, "success")
        db.create_run("webhook", True)
        db.upsert_pr("Ascend", "msprof", 1)
        db.upsert_pr("Ascend", "msprof", 2, state="merged")
        s = db.summary()
        assert s["runs_total"] == 2
        assert s["runs_by_status"] == {"success": 1, "running": 1}
        assert s["prs_by_state"] == {"open": 1, "merged": 1}
        assert s["last_run"]["status"] == "running"

    def _backdate(self, db, run_id, days):
        """把 run 的 created_at 改到 days 天前（测试用）。"""
        old = (datetime.now(timezone.utc)
               - timedelta(days=days)).isoformat()
        with db._lock:
            db._conn.execute(
                "UPDATE runs SET created_at = ? WHERE id = ?",
                (old, run_id))
            db._conn.commit()

    def test_cleanup_cascades(self, db):
        old_run = db.create_run("webhook", True)
        db.record_scan_results(old_run, _scan_result())
        new_run = db.create_run("webhook", True)
        db.record_scan_results(new_run, _scan_result())
        eid_old = db.record_push_event("refs/heads/main", "a", "b", True,
                                       run_id=old_run)
        eid_new = db.record_push_event("refs/heads/main", "c", "d", True,
                                       run_id=new_run)
        self._backdate(db, old_run, 100)

        deleted = db.cleanup_old_runs(days=90)
        assert deleted["runs"] == 1
        assert deleted["run_repos"] == 4  # 旧 run 的 4 行级联删除
        assert deleted["push_events"] == 1  # orphan 事件顺带清掉
        assert db.get_run(old_run) is None
        assert db.get_run(new_run) is not None
        assert len(db.get_run_repos(new_run)) == 4
        assert db._one("SELECT id FROM push_events WHERE id = ?",
                       (eid_old,)) is None
        assert db._one("SELECT id FROM push_events WHERE id = ?",
                       (eid_new,)) is not None

    def test_cleanup_nothing_to_do(self, db):
        run_id = db.create_run("webhook", True)
        assert db.cleanup_old_runs() == {
            "runs": 0, "run_repos": 0, "push_events": 0}
        assert db.get_run(run_id) is not None


class TestRepoConfigs:
    """repo_configs 表：一个条目配齐一个仓库的开关与专属 Token（仓库粒度）。"""

    def test_upsert_and_list(self, db):
        entry_id = db.upsert_repo_config("CANN", "HCCL")
        entries = db.list_repo_configs()
        assert len(entries) == 1
        e = entries[0]
        assert e["id"] == entry_id
        assert (e["org"], e["repo"]) == ("cann", "hccl")  # 小写归一
        assert e["enabled"] == 1 and e["token"] == ""
        assert e["created_at"] and e["updated_at"]

    def test_upsert_partial_fields_keep_existing(self, db):
        """enabled/token 传 None 保持不变；冲突更新返回同一条目 id。"""
        entry_id = db.upsert_repo_config("cann", "hccl", enabled=True,
                                         token="tok-1")
        # 只改开关，token 保留
        assert db.upsert_repo_config("cann", "hccl", enabled=False) == entry_id
        e = db.list_repo_configs()[0]
        assert e["enabled"] == 0 and e["token"] == "tok-1"
        # 只改 token，开关保留
        assert db.upsert_repo_config("cann", "hccl", token="tok-2") == entry_id
        e = db.list_repo_configs()[0]
        assert e["enabled"] == 0 and e["token"] == "tok-2"
        assert len(db.list_repo_configs()) == 1

    def test_token_clear_with_empty_string(self, db):
        db.upsert_repo_config("cann", "hccl", token="tok-1")
        db.upsert_repo_config("cann", "hccl", token="")
        assert db.get_repo_token("cann", "hccl") is None

    def test_gate_default_deny_and_toggle(self, db):
        assert db.is_auto_pr_enabled("cann", "hccl") is False  # 默认拒绝
        entry_id = db.upsert_repo_config("cann", "hccl")
        assert db.is_auto_pr_enabled("CANN", "Hccl") is True  # 大小写不敏感
        # branch 参数保留兼容但不参与判定（仓库粒度）
        assert db.is_auto_pr_enabled("cann", "hccl", "dev") is True
        assert db.set_repo_config_enabled(entry_id, False) is True
        assert db.is_auto_pr_enabled("cann", "hccl") is False
        assert db.set_repo_config_enabled(999, True) is False
        assert db.delete_repo_config(entry_id) is True
        assert db.list_repo_configs() == []
        assert db.delete_repo_config(999) is False

    def test_get_repo_token(self, db):
        assert db.get_repo_token("cann", "hccl") is None  # 无条目
        db.upsert_repo_config("CANN", "HCCL", token="tok-secret-1")
        assert db.get_repo_token("Cann", "Hccl") == "tok-secret-1"
        assert db.get_repo_token("cann", "sip") is None

    def test_migrate_folds_legacy_tables(self, tmp_path):
        """旧库 auto_pr + repo_tokens 折叠进 repo_configs 后删表。

        auto_pr 分支粒度折叠为仓库粒度（任一分支启用则仓库启用，
        org/repo 顺带小写归一）；repo_tokens 的 token 并入同仓条目。
        """
        path = tmp_path / "data" / "test.db"
        d = db_mod.init_db(path)
        d._conn.executescript("""
            CREATE TABLE auto_pr (id INTEGER PRIMARY KEY AUTOINCREMENT,
                org TEXT, repo TEXT, branch TEXT DEFAULT '', enabled INTEGER);
            CREATE TABLE repo_tokens (id INTEGER PRIMARY KEY AUTOINCREMENT,
                org TEXT, repo TEXT, token TEXT);
        """)
        d._conn.execute("INSERT INTO auto_pr (org, repo, branch, enabled)"
                        " VALUES ('CANN', 'hccl', 'master', 1)")
        d._conn.execute("INSERT INTO auto_pr (org, repo, branch, enabled)"
                        " VALUES ('cann', 'HCCL', 'dev', 0)")
        d._conn.execute("INSERT INTO auto_pr (org, repo, branch, enabled)"
                        " VALUES ('cann', 'sip', 'master', 0)")
        d._conn.execute("INSERT INTO repo_tokens (org, repo, token)"
                        " VALUES ('CANN', 'HCCL', 'tok-hccl')")
        d._conn.execute("INSERT INTO repo_tokens (org, repo, token)"
                        " VALUES ('ascend', 'msprof', 'tok-msprof')")
        d._conn.commit()
        d.close()
        d = db_mod.init_db(path)  # 重开触发迁移
        try:
            entries = {e["repo"]: e for e in d.list_repo_configs()}
            assert set(entries) == {"hccl", "sip", "msprof"}
            # hccl：master 启用 + dev 禁用 → 仓库启用；token 并入；小写归一
            assert entries["hccl"]["org"] == "cann"
            assert entries["hccl"]["enabled"] == 1
            assert entries["hccl"]["token"] == "tok-hccl"
            # sip：唯一条目禁用 → 保持禁用
            assert entries["sip"]["enabled"] == 0
            # 仅 repo_tokens 出现的仓：迁移为启用条目
            assert entries["msprof"]["enabled"] == 1
            assert entries["msprof"]["token"] == "tok-msprof"
            # 旧表已删
            tables = {r["name"] for r in d._all(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            assert "auto_pr" not in tables and "repo_tokens" not in tables
        finally:
            d.close()
        # 幂等：再次重开不再迁移
        d = db_mod.init_db(path)
        try:
            assert len(d.list_repo_configs()) == 3
        finally:
            d.close()


class TestPathTokenRules:
    """path_token_rules 表：Token 规则的增删查（关键字小写归一、保序）。"""

    def test_upsert_list_delete(self, db):
        id1 = db.upsert_path_token_rule("Ascend", "tok-a")
        id2 = db.upsert_path_token_rule(" cann ", "tok-c")
        rules = db.list_path_token_rules()
        assert [r["keyword"] for r in rules] == ["ascend", "cann"]
        assert rules[0]["token"] == "tok-a"
        # 保序（resolve_token 取第一个命中，顺序即优先级）
        assert [r["id"] for r in rules] == [id1, id2]
        # 冲突更新（大小写不敏感关键字去重）
        assert db.upsert_path_token_rule("ASCEND", "tok-a2") == id1
        rules = db.list_path_token_rules()
        assert len(rules) == 2 and rules[0]["token"] == "tok-a2"
        # 删除
        assert db.delete_path_token_rule(id1) is True
        assert db.delete_path_token_rule(999) is False
        assert [r["keyword"] for r in db.list_path_token_rules()] == ["cann"]


class TestPathLists:
    """path_lists 表：黑/白名单条目的增删查（kind 隔离、路径归一、幂等）。"""

    def test_add_list_delete(self, db):
        id1 = db.add_path_list_entry("blacklist", " /CANN/hccl/ ")
        id2 = db.add_path_list_entry("blacklist", "Ascend/msprof")
        id3 = db.add_path_list_entry("whitelist", "CANN/hccl")
        assert db.list_path_list("blacklist") == [
            "CANN/hccl", "Ascend/msprof"]
        assert db.list_path_list("whitelist") == ["CANN/hccl"]
        # 重复添加幂等，返回已有 id
        assert db.add_path_list_entry("blacklist", "CANN/hccl") == id1
        assert len(db.list_path_list_entries("blacklist")) == 2
        # 删除
        assert db.delete_path_list_entry(id2) is True
        assert db.delete_path_list_entry(999) is False
        assert db.list_path_list("blacklist") == ["CANN/hccl"]
        assert db.list_path_list("whitelist") == ["CANN/hccl"]
