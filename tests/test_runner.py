"""runner（execute_run / Worker / 日志脱敏）的单元测试。

全部 mock 不触网：patch 到 runner 模块引用点的
localgit.clone 与 service.load_*/scan_changed/submit_repo；
db 用 tmp_path 真库。
"""

import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from translator_ci import client
from webapp import runner
from webapp.config import Settings
from webapp.db import init_db, set_shadow
from webapp.queueing import CoalescingQueue, Trigger
from webapp.runner import LogBuffer, SecretMasker, Worker, execute_run

DEFAULT_TOKEN = "default-secret-token"
RULE_TOKEN = "rule-secret-token"


@pytest.fixture
def settings(tmp_path):
    """tmp_path 数据目录 + 三个已配置的 TC_* token（脱敏断言用）。"""
    return Settings(data_dir=str(tmp_path), admin_token="admin-secret",
                    webhook_token="webhook-secret", dash_token="dash-secret")


@pytest.fixture
def db(settings):
    with init_db(settings.db_path) as db:
        yield db


@pytest.fixture
def token_env(monkeypatch):
    """注入默认 token。"""
    monkeypatch.setenv("GITCODE_TOKEN", DEFAULT_TOKEN)


def _changed_item(path="Ascend/26.1.0/MindStudio/msprof", org="Ascend",
                  repo="msprof", branch="26.1.0"):
    """scan_changed changed 清单的一项（结构与引擎返回一致）。"""
    return {
        "project": {"path": path},
        "org": org, "repo": repo, "branch": branch,
        "files": [{"target_path": "docs/en/a.md", "content_b64": "YQ=="}],
        "new_count": 1, "update_count": 0,
    }


def _scan(changed=None, errors=None, incremental=False, ok=True,
          error=None):
    scan = {"ok": ok, "changed": changed or [], "unchanged": [],
            "skipped": [], "errors": errors or [], "incremental": incremental}
    if not ok:
        scan.update(stage="load_index", error=error or "扫描失败")
    return scan


@pytest.fixture
def engine():
    """把引擎入口全部 mock 到 runner 模块引用点，返回各 mock 句柄。"""
    with patch.object(runner.localgit, "clone") as clone, \
            patch.object(runner.service, "load_token_rules") as load_rules, \
            patch.object(runner.service, "load_blacklist") as load_bl, \
            patch.object(runner.service, "load_whitelist") as load_wl, \
            patch.object(runner.service, "scan_changed") as scan, \
            patch.object(runner.service, "submit_repo") as submit:
        clone.side_effect = lambda *a, **kw: os.makedirs(a[4], exist_ok=True)
        load_rules.return_value = []
        load_bl.return_value = []
        load_wl.return_value = []
        scan.return_value = _scan()
        submit.return_value = {"ok": True, "noop": True,
                               "head_branch": "translator/auto-26.1.0"}
        yield SimpleNamespace(clone=clone, load_rules=load_rules,
                              load_blacklist=load_bl, load_whitelist=load_wl,
                              scan_changed=scan, submit_repo=submit)


@pytest.fixture
def auto_pr_on(db):
    """为测试用的两个目标仓开启自动提 PR 通配条目（runner 门控放行）。"""
    db.upsert_auto_pr_entry("Ascend", "msprof")
    db.upsert_auto_pr_entry("CANN", "cann")


def _push_trigger(**kw):
    return Trigger(trigger="push", ref="refs/heads/main",
                   before_sha="b01", after_sha="a02", **kw)


class TestShadowMode:
    """影子模式：扫描照常执行，绝不调用 submit_repo，落 would_submit。"""

    def test_shadow_records_would_submit_without_submit(
            self, settings, db, token_env, engine):
        set_shadow(db, True)
        engine.scan_changed.return_value = _scan(changed=[
            _changed_item(),
            _changed_item(path="CANN/9.1.0/sip/cann", org="CANN",
                          repo="cann", branch="9.1.0"),
        ])
        execute_run(_push_trigger(), settings, db)

        engine.submit_repo.assert_not_called()
        run = db.list_runs()[0]
        assert run["status"] == "success"
        assert run["shadow"] == 1
        repos = db.get_run_repos(run["id"])
        assert {r["status"] for r in repos} == {"would_submit"}
        assert repos[0]["files"] == ["docs/en/a.md"]

    def test_shadow_scan_errors_mark_partial(
            self, settings, db, token_env, engine):
        set_shadow(db, True)
        engine.scan_changed.return_value = _scan(
            errors=[{"path": "A/b/c/d", "error": "对比失败"}])
        execute_run(_push_trigger(), settings, db)
        run = db.list_runs()[0]
        assert run["status"] == "partial"
        assert run["error_count"] == 1


class TestActiveMode:
    """active 模式：逐仓 resolve_token + submit_repo，完整粒度落库。"""

    def test_submit_each_repo_with_granularity(
            self, settings, db, token_env, engine, auto_pr_on):
        set_shadow(db, False)
        engine.scan_changed.return_value = _scan(changed=[
            _changed_item(),
            _changed_item(path="CANN/9.1.0/sip/cann", org="CANN",
                          repo="cann", branch="9.1.0"),
        ])
        engine.submit_repo.side_effect = [
            {"ok": True, "pr_url": "https://gitcode.com/Ascend/msprof/merge_requests/42",
             "pr_number": 42, "pr_existed": False,
             "head_branch": "translator/auto-26.1.0", "pr_title": "同步译稿"},
            {"ok": True, "noop": True, "head_branch": "translator/auto-9.1.0"},
        ]
        execute_run(_push_trigger(), settings, db)

        assert engine.submit_repo.call_count == 2
        run = db.list_runs()[0]
        assert run["status"] == "success"
        repos = {r["path"]: r for r in db.get_run_repos(run["id"])}
        pr_repo = repos["Ascend/26.1.0/MindStudio/msprof"]
        assert pr_repo["status"] == "submit_ok"
        assert pr_repo["pr_number"] == 42
        assert pr_repo["pr_existed"] == 0
        assert pr_repo["head_branch"] == "translator/auto-26.1.0"
        noop_repo = repos["CANN/9.1.0/sip/cann"]
        assert noop_repo["status"] == "noop"
        assert noop_repo["head_branch"] == "translator/auto-9.1.0"

        prs = db.list_prs()
        assert len(prs) == 1
        assert prs[0]["number"] == 42
        assert prs[0]["org"] == "Ascend" and prs[0]["repo"] == "msprof"
        assert prs[0]["state"] == "open"
        assert prs[0]["source"] == "service"
        assert prs[0]["run_id"] == run["id"]
        assert prs[0]["branch"] == "26.1.0"

    def test_pr_existed_upserted(self, settings, db, token_env, engine, auto_pr_on):
        set_shadow(db, False)
        engine.scan_changed.return_value = _scan(changed=[_changed_item()])
        engine.submit_repo.return_value = {
            "ok": True, "pr_number": 7, "pr_existed": True,
            "pr_url": "https://gitcode.com/Ascend/msprof/merge_requests/7",
            "head_branch": "translator/auto-26.1.0"}
        execute_run(_push_trigger(), settings, db)
        run = db.list_runs()[0]
        repo = db.get_run_repos(run["id"])[0]
        assert repo["status"] == "submit_ok"
        assert repo["pr_existed"] == 1
        assert db.list_prs()[0]["number"] == 7

    def test_token_rule_routing(self, settings, db, token_env, engine, auto_pr_on):
        """命中规则的仓库用规则 token 提交；配置了有效规则时未命中的
        仓库按权限管控不提交（记 token_rule 失败，与 submit_all 一致）。"""
        set_shadow(db, False)
        engine.load_rules.return_value = [
            {"keyword": "msprof", "token": RULE_TOKEN}]
        engine.scan_changed.return_value = _scan(changed=[
            _changed_item(),
            _changed_item(path="CANN/9.1.0/sip/cann", org="CANN",
                          repo="cann", branch="9.1.0"),
        ])
        execute_run(_push_trigger(), settings, db)
        tokens = [c.args[4] for c in engine.submit_repo.call_args_list]
        assert tokens == [RULE_TOKEN]
        run = db.list_runs()[0]
        repos = {r["path"]: r for r in db.get_run_repos(run["id"])}
        skipped = repos["CANN/9.1.0/sip/cann"]
        assert skipped["status"] == "submit_failed"
        assert skipped["stage"] == "token_rule"

    def test_submit_failure_marks_partial(
            self, settings, db, token_env, engine, auto_pr_on):
        set_shadow(db, False)
        engine.scan_changed.return_value = _scan(changed=[
            _changed_item(),
            _changed_item(path="CANN/9.1.0/sip/cann", org="CANN",
                          repo="cann", branch="9.1.0"),
        ])
        engine.submit_repo.side_effect = [
            {"ok": True, "noop": True},
            {"ok": False, "stage": "create_pr", "error": "API 500"},
        ]
        execute_run(_push_trigger(), settings, db)
        run = db.list_runs()[0]
        assert run["status"] == "partial"
        assert run["failed_count"] == 1
        repos = {r["status"] for r in db.get_run_repos(run["id"])}
        assert repos == {"noop", "submit_failed"}

    def test_submit_repo_exception_contained(
            self, settings, db, token_env, engine, auto_pr_on):
        """submit_repo 抛异常时按单仓失败处理，不中断其余仓库。"""
        set_shadow(db, False)
        engine.scan_changed.return_value = _scan(changed=[
            _changed_item(),
            _changed_item(path="CANN/9.1.0/sip/cann", org="CANN",
                          repo="cann", branch="9.1.0"),
        ])
        engine.submit_repo.side_effect = [
            client.GitCodeError(500, "boom"),
            {"ok": True, "noop": True},
        ]
        execute_run(_push_trigger(), settings, db)
        run = db.list_runs()[0]
        assert run["status"] == "partial"
        assert run["failed_count"] == 1
        repos = {r["path"]: r for r in db.get_run_repos(run["id"])}
        assert repos["Ascend/26.1.0/MindStudio/msprof"]["stage"] == "exception"


class TestDbTokenRouting:
    """设置页仓库专属 token：优先级高于关键字规则与默认 token，且入脱敏集合。"""

    DB_TOKEN = "db-secret-token"

    def test_db_token_takes_precedence(
            self, settings, db, token_env, engine, auto_pr_on):
        set_shadow(db, False)
        db.upsert_repo_token("Ascend", "msprof", self.DB_TOKEN)
        engine.load_rules.return_value = [
            {"keyword": "msprof", "token": RULE_TOKEN}]
        engine.scan_changed.return_value = _scan(changed=[
            _changed_item(),
            _changed_item(path="CANN/9.1.0/sip/cann", org="CANN",
                          repo="cann", branch="9.1.0"),
        ])
        execute_run(_push_trigger(), settings, db)
        tokens = [c.args[4] for c in engine.submit_repo.call_args_list]
        assert tokens == [self.DB_TOKEN]  # msprof 用 DB token
        # cann 未命中 msprof 关键字规则，按权限管控不提交（与既有语义一致）
        repos = {r["path"]: r for r in db.get_run_repos(db.list_runs()[0]["id"])}
        assert repos["CANN/9.1.0/sip/cann"]["stage"] == "token_rule"

    def test_db_token_masked_in_submit_error(
            self, settings, db, token_env, engine, auto_pr_on):
        """submit 错误文本带仓库专属 token：run_repos.error 与日志脱敏。"""
        set_shadow(db, False)
        db.upsert_repo_token("Ascend", "msprof", self.DB_TOKEN)
        engine.scan_changed.return_value = _scan(changed=[_changed_item()])
        engine.submit_repo.return_value = {
            "ok": False, "stage": "create_pr",
            "error": f"推送失败: https://oauth2:{self.DB_TOKEN}@gitcode.com/x"}
        execute_run(_push_trigger(), settings, db)
        run = db.list_runs()[0]
        repo = db.get_run_repos(run["id"])[0]
        assert repo["status"] == "submit_failed"
        assert self.DB_TOKEN not in repo["error"]
        assert self.DB_TOKEN not in db.get_run(run["id"])["log"]


class TestAutoPrGate:
    """自动提 PR 开关门控：设置页无条目或已禁用的仓库默认拒绝提交。"""

    def test_no_entry_skips_submit(self, settings, db, token_env, engine):
        set_shadow(db, False)
        engine.scan_changed.return_value = _scan(changed=[_changed_item()])
        execute_run(_push_trigger(), settings, db)
        engine.submit_repo.assert_not_called()
        run = db.list_runs()[0]
        assert run["status"] == "success"  # 跳过不算失败
        repo = db.get_run_repos(run["id"])[0]
        assert repo["status"] == "noop"
        assert repo["stage"] == "auto_pr_off"
        assert "开关未启用" in db.get_run(run["id"])["log"]
        assert db.list_prs() == []

    def test_disabled_entry_skips(self, settings, db, token_env, engine):
        set_shadow(db, False)
        entry_id = db.upsert_auto_pr_entry("Ascend", "msprof", "26.1.0")
        db.set_auto_pr_enabled(entry_id, False)
        engine.scan_changed.return_value = _scan(changed=[_changed_item()])
        execute_run(_push_trigger(), settings, db)
        engine.submit_repo.assert_not_called()
        repo = db.get_run_repos(db.list_runs()[0]["id"])[0]
        assert repo["stage"] == "auto_pr_off"

    def test_exact_branch_overrides_wildcard(
            self, settings, db, token_env, engine):
        """精确分支条目优先于通配条目：通配启用但 26.1.0 禁用 → 仍跳过。"""
        set_shadow(db, False)
        db.upsert_auto_pr_entry("Ascend", "msprof")  # 通配启用
        exact = db.upsert_auto_pr_entry("Ascend", "msprof", "26.1.0")
        db.set_auto_pr_enabled(exact, False)
        engine.scan_changed.return_value = _scan(changed=[_changed_item()])
        execute_run(_push_trigger(), settings, db)
        engine.submit_repo.assert_not_called()

    def test_mixed_gate_per_repo(self, settings, db, token_env, engine):
        """同一 run 内：启用条目的仓提交，无条目的仓跳过。"""
        set_shadow(db, False)
        db.upsert_auto_pr_entry("Ascend", "msprof", "26.1.0")
        engine.scan_changed.return_value = _scan(changed=[
            _changed_item(),
            _changed_item(path="CANN/9.1.0/sip/cann", org="CANN",
                          repo="cann", branch="9.1.0"),
        ])
        execute_run(_push_trigger(), settings, db)
        assert engine.submit_repo.call_count == 1
        assert engine.submit_repo.call_args.args[1] == "msprof"
        repos = {r["path"]: r for r in db.get_run_repos(db.list_runs()[0]["id"])}
        assert repos["Ascend/26.1.0/MindStudio/msprof"]["status"] == "noop"
        assert repos["CANN/9.1.0/sip/cann"]["stage"] == "auto_pr_off"


class TestStatusMapping:
    """终态映射：scan 失败→failed；有 errors/提交失败→partial；否则 success。"""

    def test_scan_failure_marks_failed(
            self, settings, db, token_env, engine):
        engine.scan_changed.return_value = _scan(
            ok=False, error="读取目录索引失败")
        execute_run(_push_trigger(), settings, db)
        run = db.list_runs()[0]
        assert run["status"] == "failed"
        assert "读取目录索引失败" in run["error"]

    def test_exception_marks_failed_and_workdir_cleaned(
            self, settings, db, token_env, engine):
        engine.scan_changed.side_effect = RuntimeError("unexpected")
        execute_run(_push_trigger(), settings, db)
        run = db.list_runs()[0]
        assert run["status"] == "failed"
        assert "unexpected" in run["error"]
        assert not os.path.exists(
            os.path.join(settings.work_dir, f"run-{run['id']}"))

    def test_all_success(self, settings, db, token_env, engine):
        engine.scan_changed.return_value = _scan(changed=[_changed_item()])
        execute_run(_push_trigger(), settings, db)
        assert db.list_runs()[0]["status"] == "success"

    def test_missing_token_fails_without_clone(
            self, settings, db, monkeypatch, engine):
        monkeypatch.delenv("GITCODE_TOKEN", raising=False)
        execute_run(_push_trigger(), settings, db)
        engine.clone.assert_not_called()
        run = db.list_runs()[0]
        assert run["status"] == "failed"
        assert "GITCODE_TOKEN" in run["error"]


class TestIncrementalArgs:
    """增量参数传递：push 触发带 sha，全量/手动触发一律传 None。"""

    def test_push_trigger_passes_diff_sha(
            self, settings, db, token_env, engine):
        engine.scan_changed.return_value = _scan(incremental=True)
        execute_run(_push_trigger(), settings, db)
        _, kwargs = engine.scan_changed.call_args
        assert kwargs["diff_before"] == "b01"
        assert kwargs["diff_after"] == "a02"
        run = db.list_runs()[0]
        assert run["push_before"] == "b01"
        assert run["push_after"] == "a02"
        assert run["incremental"] == 1

    def test_full_scan_trigger_drops_sha(
            self, settings, db, token_env, engine):
        trigger = Trigger.manual(full_scan=True)
        execute_run(trigger, settings, db)
        _, kwargs = engine.scan_changed.call_args
        assert kwargs["diff_before"] is None
        assert kwargs["diff_after"] is None
        run = db.list_runs()[0]
        assert run["push_before"] is None
        assert run["push_after"] is None
        assert run["trigger"] == "manual"


class TestMasking:
    """脱敏：默认/规则/TC_* token 一律替换为 ***，落库文本无明文。"""

    def test_masker_replaces_all_kinds(self, settings):
        masker = SecretMasker([DEFAULT_TOKEN, settings.admin_token,
                               settings.webhook_token, settings.dash_token])
        masker.add_secrets([RULE_TOKEN, ""])
        text = (f"{DEFAULT_TOKEN} {RULE_TOKEN} admin-secret "
                "webhook-secret dash-secret")
        masked = masker.mask(text)
        assert "***" in masked
        for secret in (DEFAULT_TOKEN, RULE_TOKEN, "admin-secret",
                       "webhook-secret", "dash-secret"):
            assert secret not in masked

    def test_clone_error_masked_in_run_error_and_log(
            self, settings, db, token_env, engine):
        """localgit.clone 抛含 token URL 的 GitCodeError：落库无明文。"""
        url = f"https://oauth2:{DEFAULT_TOKEN}@gitcode.com/org/repo.git"
        engine.clone.side_effect = client.GitCodeError(
            200, f"克隆 org/repo@默认分支 失败: fatal: {url} 鉴权失败")
        execute_run(_push_trigger(), settings, db)
        run = db.list_runs()[0]
        assert run["status"] == "failed"
        assert DEFAULT_TOKEN not in run["error"]
        assert "oauth2:***@gitcode.com" in run["error"]
        assert DEFAULT_TOKEN not in db.get_run(run["id"])["log"]

    def test_rule_token_masked_in_submit_error(
            self, settings, db, token_env, engine, auto_pr_on):
        """submit_repo 错误文本带规则 token：run_repos.error 脱敏。"""
        set_shadow(db, False)
        engine.load_rules.return_value = [
            {"keyword": "msprof", "token": RULE_TOKEN}]
        engine.scan_changed.return_value = _scan(changed=[_changed_item()])
        engine.submit_repo.return_value = {
            "ok": False, "stage": "create_pr",
            "error": f"推送失败: https://oauth2:{RULE_TOKEN}@gitcode.com/x"}
        execute_run(_push_trigger(), settings, db)
        run = db.list_runs()[0]
        repo = db.get_run_repos(run["id"])[0]
        assert repo["status"] == "submit_failed"
        assert RULE_TOKEN not in repo["error"]
        assert RULE_TOKEN not in db.get_run(run["id"])["log"]

    def test_scan_errors_masked(self, settings, db, token_env, engine):
        engine.scan_changed.return_value = _scan(
            errors=[{"path": "A/b/c/d",
                     "error": f"克隆失败: {DEFAULT_TOKEN}"}])
        execute_run(_push_trigger(), settings, db)
        run = db.list_runs()[0]
        repo = db.get_run_repos(run["id"])[0]
        assert repo["status"] == "error"
        assert DEFAULT_TOKEN not in repo["error"]


class TestLogBuffer:
    """日志缓冲：50 行阈值刷库、超时刷库、flush 兜底。"""

    def test_flush_on_line_threshold(self, settings, db):
        run_id = db.create_run("push", True)
        buf = LogBuffer(db, run_id, SecretMasker())
        for i in range(runner.LOG_FLUSH_LINES):
            buf(f"line {i}")
        assert "line 0" in db.get_run(run_id)["log"]
        buf("tail")
        assert "tail" not in db.get_run(run_id)["log"]
        buf.flush()
        assert "tail" in db.get_run(run_id)["log"]

    def test_flush_on_time_interval(self, settings, db):
        run_id = db.create_run("push", True)
        buf = LogBuffer(db, run_id, SecretMasker())
        buf("first")
        buf._last_flush -= runner.LOG_FLUSH_INTERVAL + 1
        buf("second")
        assert "second" in db.get_run(run_id)["log"]


class TestRunFinalizer:
    """收尾：last_run_at 回写、push 事件回填 run_id、workdir 清理。"""

    def test_finalize_on_success(self, settings, db, token_env, engine):
        eid = db.record_push_event("refs/heads/main", "b01", "a02", True)
        trigger = _push_trigger(push_event_ids=[eid])
        execute_run(trigger, settings, db)
        run = db.list_runs()[0]
        assert db.kv_get("last_run_at") is not None
        row = db._one("SELECT run_id FROM push_events WHERE id = ?", (eid,))
        assert row["run_id"] == run["id"]
        assert not os.path.exists(
            os.path.join(settings.work_dir, f"run-{run['id']}"))

    def test_finalize_on_exception(self, settings, db, token_env, engine):
        eid = db.record_push_event("refs/heads/main", "b01", "a02", True)
        engine.clone.side_effect = RuntimeError("disk full")
        execute_run(_push_trigger(push_event_ids=[eid]), settings, db)
        run = db.list_runs()[0]
        assert run["status"] == "failed"
        assert db.kv_get("last_run_at") is not None
        row = db._one("SELECT run_id FROM push_events WHERE id = ?", (eid,))
        assert row["run_id"] == run["id"]
        assert not os.path.exists(settings.work_dir) or \
            not os.listdir(settings.work_dir)


class TestWorker:
    """Worker 线程：消费队列、协作式停止。"""

    def test_worker_executes_queued_trigger(
            self, settings, db, token_env, engine):
        queue = CoalescingQueue()
        worker = Worker(queue, settings, db, poll_timeout=0.05)
        worker.start()
        try:
            queue.push(_push_trigger())
            deadline = time.monotonic() + 5
            while not db.list_runs() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert db.list_runs()[0]["status"] == "success"
        finally:
            worker.stop()
            worker.join(timeout=5)
        assert not worker.alive

    def test_stop_is_cooperative(self, settings, db):
        """stop() 不打断进行中的 run：run 跑完后线程才退出。"""
        started = threading.Event()
        release = threading.Event()

        def slow_run(trigger, s, d):
            started.set()
            release.wait(timeout=5)

        queue = CoalescingQueue()
        worker = Worker(queue, settings, db, poll_timeout=0.05)
        with patch.object(runner, "execute_run", side_effect=slow_run):
            worker.start()
            try:
                queue.push(_push_trigger())
                assert started.wait(timeout=5)
                worker.stop()
                time.sleep(0.2)
                assert worker.alive  # run 未结束，线程不退出
                release.set()
                worker.join(timeout=5)
                assert not worker.alive
            finally:
                release.set()
                worker.stop()
                worker.join(timeout=5)

    def test_stop_on_idle_exits(self, settings, db):
        queue = CoalescingQueue()
        worker = Worker(queue, settings, db, poll_timeout=0.05)
        worker.start()
        worker.stop()
        worker.join(timeout=5)
        assert not worker.alive
