"""webapp.runner：提交任务执行、脱敏日志与队列 Worker。"""

import base64
import threading
import time

import pytest

from webapp import runner
from webapp.db import init_db
from webapp.queueing import SubmissionQueue

CALLER_TOKEN = "caller-token-abc"
SOURCE_TOKEN = "source-token-xyz"


@pytest.fixture
def settings(tmp_path):
    from webapp.config import Settings
    return Settings(data_dir=str(tmp_path), wait_timeout=5, queue_max=10)


@pytest.fixture
def db(tmp_path):
    database = init_db(tmp_path / "translator.db")
    yield database
    database.close()


@pytest.fixture(autouse=True)
def _fake_secrets(monkeypatch):
    """服务侧 token 用固定值，权威仓不读 ATOMGIT_SOURCE_REPO 覆盖。"""
    def fake_get(name, default=""):
        if name == "ATOMGIT_TOKEN":
            return SOURCE_TOKEN
        return default
    monkeypatch.setattr(runner.secrets, "get", fake_get)


def _groups():
    return [
        {
            "org": "CANN", "repo": "hccl", "branch": "9.1.0",
            "source_paths": ["CANN/9.1.0/hccl/docs/a.md"],
            "files": [{"source_path": "CANN/9.1.0/hccl/docs/a.md",
                       "target_path": "docs/a.md"}],
        },
        {
            "org": "CANN", "repo": "sip", "branch": "9.1.0",
            "source_paths": ["CANN/9.1.0/sip/docs/b.md"],
            "files": [{"source_path": "CANN/9.1.0/sip/docs/b.md",
                       "target_path": "docs/b.md"}],
        },
    ]


def _prepare(db, request_id="req", dry_run=False, groups=None):
    groups = groups or _groups()
    paths = [f["source_path"] for g in groups for f in g["files"]]
    return db.create_submission(request_id, paths, groups, dry_run=dry_run)


def _mock_source(monkeypatch, tree_paths=(), content=b"hello"):
    """让克隆/文件树/读取全部走假实现（不触网、不落盘）。"""
    calls = {"clone": [], "read": []}

    def fake_clone(owner, repo, ref, token, dest_dir, log=print, blobs=False):
        calls["clone"].append({
            "owner": owner, "repo": repo, "ref": ref,
            "token": token, "dest": dest_dir, "blobs": blobs})
        return dest_dir

    def fake_read(repo_dir, path):
        calls["read"].append(path)
        return content

    monkeypatch.setattr(runner.localgit, "clone", fake_clone)
    monkeypatch.setattr(runner.localgit, "tree_map",
                        lambda repo_dir, ref="HEAD": {
                            p: "sha" for p in tree_paths})
    monkeypatch.setattr(runner.localgit, "read_file", fake_read)
    return calls


class TestSecretMasker:
    def test_masks_all_values_longest_first(self):
        masker = runner.SecretMasker(["abc", "abcdef", ""])
        assert masker.mask("x abcdef abc y") == "x *** *** y"

    def test_add_ignores_empty_and_duplicates(self):
        masker = runner.SecretMasker()
        masker.add(["t", " ", "t"])
        assert masker.mask("t") == "***"

    def test_mask_none_and_empty(self):
        masker = runner.SecretMasker(["t"])
        assert masker.mask(None) is None
        assert masker.mask("") == ""


class TestLogBuffer:
    def _buffer(self, masker=None, sink=None):
        sink = sink if sink is not None else []
        db = type("DB", (), {
            "append_log": staticmethod(
                lambda sid, text: sink.append((sid, text)))})
        masker = masker or runner.SecretMasker()
        return runner.LogBuffer(db, 1, masker), sink

    def test_flush_on_line_limit(self):
        buffer, sink = self._buffer(masker=runner.SecretMasker(["tok"]))
        for i in range(runner.LOG_FLUSH_LINES):
            buffer(f"line-{i} tok")
        assert len(sink) == 1
        assert sink[0][1].count("tok") == 0
        assert "***" in sink[0][1]

    def test_flush_on_interval(self, monkeypatch):
        buffer, sink = self._buffer()
        clock = {"now": 0.0}
        monkeypatch.setattr(runner.time, "monotonic", lambda: clock["now"])
        buffer._last_flush = 0.0
        clock["now"] = runner.LOG_FLUSH_INTERVAL + 1
        buffer("line")
        assert len(sink) == 1

    def test_explicit_flush(self):
        buffer, sink = self._buffer()
        buffer("a")
        assert sink == []
        buffer.flush()
        assert sink == [(1, "a\n")]
        buffer.flush()  # 无缓冲时不再写
        assert len(sink) == 1


class TestExecuteSubmission:
    def test_success_creates_prs(self, db, settings, monkeypatch):
        calls = _mock_source(monkeypatch, [
            "CANN/9.1.0/hccl/docs/a.md", "CANN/9.1.0/sip/docs/b.md"])
        submitted = []

        def fake_submit_repo(org, repo, branch, files, token, log=print):
            submitted.append((org, repo, branch, files, token))
            log(f"正在提交 {org}/{repo}，token={token}")
            if repo == "sip":
                return {"ok": True, "pr_existed": True, "pr_number": 2,
                        "pr_url": "https://atomgit.com/CANN/sip/merge_requests/2"}
            return {"ok": True, "pr_existed": False, "pr_number": 1,
                    "pr_url": "https://atomgit.com/CANN/hccl/merge_requests/1",
                    "pr_title": "新增 a.md 英文版文档"}

        monkeypatch.setattr(runner.service, "submit_repo", fake_submit_repo)
        sid = _prepare(db)

        runner.execute_submission(sid, CALLER_TOKEN, settings, db)

        submission = db.get_submission(sid)
        assert submission["status"] == "success"
        items = {i["repo"]: i for i in db.get_submission_items(sid)}
        assert items["hccl"]["status"] == "submitted"
        assert items["hccl"]["pr_number"] == 1
        assert items["hccl"]["pr_title"] == "新增 a.md 英文版文档"
        assert items["hccl"]["pr_existed"] == 0
        assert items["sip"]["status"] == "already_exists"
        assert items["sip"]["pr_existed"] == 1
        # 权威仓用服务侧 token、默认分支（ref 留空）、blobs=True
        assert calls["clone"][0]["token"] == SOURCE_TOKEN
        assert calls["clone"][0]["ref"] == ""
        assert calls["clone"][0]["blobs"] is True
        # 目标仓用调用方 token；文件内容按 base64 传入
        assert all(c[4] == CALLER_TOKEN for c in submitted)
        assert submitted[0][3][0]["target_path"] == "docs/a.md"
        assert base64.b64decode(
            submitted[0][3][0]["content_b64"]) == b"hello"
        # 调用方 token 不落库（日志已脱敏）
        log = db.get_submission_log(sid)
        assert CALLER_TOKEN not in log
        assert "***" in log
        # 工作目录清理
        from pathlib import Path
        assert not (Path(settings.work_dir) / "submissions" /
                    f"submission-{sid}").exists()

    def test_missing_source_file_fails_whole_submission(self, db, settings,
                                                       monkeypatch):
        _mock_source(monkeypatch, ["CANN/9.1.0/hccl/docs/a.md"])
        monkeypatch.setattr(
            runner.service, "submit_repo",
            lambda *a, **kw: pytest.fail("不应提交"))
        sid = _prepare(db)

        runner.execute_submission(sid, CALLER_TOKEN, settings, db)

        submission = db.get_submission(sid)
        assert submission["status"] == "failed"
        assert "不存在" in submission["error"]
        assert "CANN/9.1.0/sip/docs/b.md" in submission["error"]
        for item in db.get_submission_items(sid):
            assert item["status"] == "failed"
            assert item["error"] == submission["error"]

    def test_ascend_target_resolved_to_authority_path(self, db, settings,
                                                      monkeypatch):
        """目标坐标缺产品层时由索引补齐：Ascend/<产品>/<版本>/<仓库>/..."""
        calls = _mock_source(
            monkeypatch, ["Ascend/MindStudio/26.2.0/msagent/docs/en/a.md"])
        groups = [{
            "org": "Ascend", "repo": "msagent", "branch": "26.2.0",
            "source_paths": ["Ascend/msagent/26.2.0/docs/en/a.md"],
            "files": [{
                "source_path": "Ascend/msagent/26.2.0/docs/en/a.md",
                "rel": "docs/en/a.md",
                "target_path": "docs/en/a.md"}],
        }]
        monkeypatch.setattr(
            runner.service, "submit_repo",
            lambda *a, **kw: {"ok": True, "pr_number": 1, "pr_url": "u"})
        sid = _prepare(db, groups=groups)

        runner.execute_submission(sid, CALLER_TOKEN, settings, db)

        assert db.get_submission(sid)["status"] == "success"
        assert calls["read"] == [
            "Ascend/MindStudio/26.2.0/msagent/docs/en/a.md"]

    def test_ambiguous_source_fails_whole_submission(self, db, settings,
                                                    monkeypatch):
        """同一坐标命中多份权威仓文件：整单失败，不猜其中一份。"""
        _mock_source(monkeypatch, [
            "Ascend/alpha/26.2.0/kml/docs/a.md",
            "Ascend/beta/26.2.0/kml/docs/a.md"])
        groups = [{
            "org": "Ascend", "repo": "kml", "branch": "26.2.0",
            "source_paths": ["Ascend/kml/26.2.0/docs/a.md"],
            "files": [{
                "source_path": "Ascend/kml/26.2.0/docs/a.md",
                "rel": "docs/a.md",
                "target_path": "docs/a.md"}],
        }]
        monkeypatch.setattr(
            runner.service, "submit_repo",
            lambda *a, **kw: pytest.fail("歧义时不应提交"))
        sid = _prepare(db, groups=groups)

        runner.execute_submission(sid, CALLER_TOKEN, settings, db)

        submission = db.get_submission(sid)
        assert submission["status"] == "failed"
        assert "匹配到多个文件" in submission["error"]

    def test_dry_run_does_not_submit(self, db, settings, monkeypatch):
        _mock_source(monkeypatch, [
            "CANN/9.1.0/hccl/docs/a.md", "CANN/9.1.0/sip/docs/b.md"])
        monkeypatch.setattr(
            runner.service, "submit_repo",
            lambda *a, **kw: pytest.fail("dry_run 不应提交"))
        sid = _prepare(db, dry_run=True)

        runner.execute_submission(sid, CALLER_TOKEN, settings, db)

        assert db.get_submission(sid)["status"] == "success"
        assert [i["status"] for i in db.get_submission_items(sid)] == \
            ["would_submit", "would_submit"]

    def test_partial_failure(self, db, settings, monkeypatch):
        _mock_source(monkeypatch, [
            "CANN/9.1.0/hccl/docs/a.md", "CANN/9.1.0/sip/docs/b.md"])

        def fake_submit_repo(org, repo, branch, files, token, log=print):
            if repo == "sip":
                return {"ok": False, "stage": "create_pr", "error": "boom"}
            return {"ok": True, "pr_number": 1, "pr_url": "u"}

        monkeypatch.setattr(runner.service, "submit_repo", fake_submit_repo)
        sid = _prepare(db)

        runner.execute_submission(sid, CALLER_TOKEN, settings, db)

        assert db.get_submission(sid)["status"] == "partial"
        items = {i["repo"]: i for i in db.get_submission_items(sid)}
        assert items["hccl"]["status"] == "submitted"
        assert items["sip"]["status"] == "failed"
        assert items["sip"]["stage"] == "create_pr"
        assert items["sip"]["error"] == "boom"

    def test_all_failed(self, db, settings, monkeypatch):
        _mock_source(monkeypatch, [
            "CANN/9.1.0/hccl/docs/a.md", "CANN/9.1.0/sip/docs/b.md"])
        monkeypatch.setattr(
            runner.service, "submit_repo",
            lambda *a, **kw: {"ok": False, "stage": "fork_repo",
                              "error": "nope"})
        sid = _prepare(db)

        runner.execute_submission(sid, CALLER_TOKEN, settings, db)

        assert db.get_submission(sid)["status"] == "failed"
        assert all(i["status"] == "failed"
                   for i in db.get_submission_items(sid))

    def test_submit_exception_becomes_item_failure(self, db, settings,
                                                   monkeypatch):
        _mock_source(monkeypatch, [
            "CANN/9.1.0/hccl/docs/a.md", "CANN/9.1.0/sip/docs/b.md"])

        def boom(*a, **kw):
            raise RuntimeError(f"网络异常 token={CALLER_TOKEN}")

        monkeypatch.setattr(runner.service, "submit_repo", boom)
        sid = _prepare(db)

        runner.execute_submission(sid, CALLER_TOKEN, settings, db)

        submission = db.get_submission(sid)
        assert submission["status"] == "failed"
        for item in db.get_submission_items(sid):
            assert item["status"] == "failed"
            assert item["stage"] == "exception"
            assert CALLER_TOKEN not in (item["error"] or "")
        assert CALLER_TOKEN not in db.get_submission_log(sid)

    def test_noop_item(self, db, settings, monkeypatch):
        _mock_source(monkeypatch, [
            "CANN/9.1.0/hccl/docs/a.md", "CANN/9.1.0/sip/docs/b.md"])
        monkeypatch.setattr(
            runner.service, "submit_repo",
            lambda *a, **kw: {"ok": True, "noop": True})
        sid = _prepare(db)

        runner.execute_submission(sid, CALLER_TOKEN, settings, db)

        assert db.get_submission(sid)["status"] == "success"
        assert [i["status"] for i in db.get_submission_items(sid)] == \
            ["noop", "noop"]

    @pytest.mark.parametrize("token", ["", "   "])
    def test_empty_caller_token_fails(self, db, settings, token):
        sid = _prepare(db)
        runner.execute_submission(sid, token, settings, db)
        submission = db.get_submission(sid)
        assert submission["status"] == "failed"
        assert "调用方 AtomGit token 为空" in submission["error"]

    def test_missing_source_token_fails(self, db, settings, monkeypatch):
        monkeypatch.setattr(runner.secrets, "get",
                            lambda name, default="": default)
        sid = _prepare(db)
        runner.execute_submission(sid, CALLER_TOKEN, settings, db)
        submission = db.get_submission(sid)
        assert submission["status"] == "failed"
        assert "ATOMGIT_TOKEN" in submission["error"]

    def test_only_queued_submission_executes(self, db, settings, monkeypatch):
        sid = _prepare(db)
        db.finish_submission(sid, "success")
        calls = _mock_source(monkeypatch)
        runner.execute_submission(sid, CALLER_TOKEN, settings, db)
        assert calls["clone"] == []          # 未重新执行
        assert db.get_submission(sid)["status"] == "success"

    def test_unknown_submission_is_noop(self, db, settings):
        runner.execute_submission(999, CALLER_TOKEN, settings, db)


class TestSubmissionWorker:
    def test_executes_pushed_tasks_then_stops(self, db, settings, monkeypatch):
        executed = []
        done = threading.Event()

        def fake_execute(submission_id, token, settings_, db_):
            executed.append((submission_id, token))
            done.set()

        monkeypatch.setattr(runner, "execute_submission", fake_execute)
        queue = SubmissionQueue(maxsize=2)
        worker = runner.SubmissionWorker(queue, settings, db,
                                         poll_timeout=0.01)
        worker.start()
        assert worker.alive
        assert queue.push(7, "tok") is True
        assert done.wait(2)
        worker.stop()
        worker.join(timeout=2)
        assert not worker.alive
        assert executed == [(7, "tok")]
