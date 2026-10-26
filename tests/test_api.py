"""webapp.api：提交/查询/PR 状态 API 与鉴权。"""

import threading
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from translator import client
from webapp import api, runner
from webapp.config import Settings

API_KEY = "test-key"
CALLER_TOKEN = "caller-token"
HEADERS = {"X-API-Key": API_KEY, "X-AtomGit-Token": CALLER_TOKEN}
PATH = "CANN/9.1.0/hccl/docs/en/a.md"


def _finish_fake(submission_id, token, settings, db):
    """Worker 假实现：立刻把任务与结果项置为成功（不触网）。"""
    submission = db.get_submission(submission_id)
    if submission is None or submission["status"] != "queued":
        return
    db.mark_submission_running(submission_id)
    for item in db.get_submission_items(submission_id):
        db.update_submission_item(
            item["id"], status="submitted", pr_number=1,
            pr_url=f"https://atomgit.com/{item['org']}/{item['repo']}"
                   f"/merge_requests/1")
    db.finish_submission(submission_id, "success")


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=str(tmp_path), api_key=API_KEY,
                    wait_timeout=5, max_paths=5, queue_max=10)


@pytest.fixture(autouse=True)
def _fake_tokens(monkeypatch):
    """PR 状态查询使用固定的服务侧 token（不读真实环境）。"""
    def fake_get(name, default=""):
        if name == "ATOMGIT_TOKEN":
            return "service-token"
        return default
    monkeypatch.setattr(api.secrets, "get", fake_get)


@pytest.fixture
def open_client(monkeypatch):
    """打开应用（触发 lifespan）并注入 Worker 假实现。"""
    @contextmanager
    def _open(settings=None, fake=None):
        monkeypatch.setattr(runner, "execute_submission",
                            fake or _finish_fake)
        app = api.create_app(settings)
        with TestClient(app) as test_client:
            yield test_client, app
    return _open


class TestAuth:
    def test_healthz_no_auth(self, open_client, settings):
        with open_client(settings) as (client_, _app):
            resp = client_.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert {"queue", "pending", "running"} <= set(resp.json())

    def test_missing_or_wrong_api_key_401(self, open_client, settings):
        with open_client(settings) as (client_, _app):
            assert client_.get("/api/v1/submissions/x").status_code == 401
            resp = client_.get("/api/v1/submissions/x",
                               headers={"X-API-Key": "wrong"})
            assert resp.status_code == 401
            resp = client_.post("/api/v1/submissions", json={"paths": [PATH]})
            assert resp.status_code == 401

    def test_api_key_required_at_startup(self, monkeypatch, tmp_path):
        monkeypatch.setattr(api.secrets, "get", lambda name, default="": default)
        with pytest.raises(RuntimeError, match="TR_API_KEY"):
            api.create_app(Settings(data_dir=str(tmp_path), api_key=""))

    def test_api_key_from_secrets(self, monkeypatch, tmp_path):
        """settings 未配置时回退用 Vault/secrets 文件中的 TR_API_KEY。"""
        monkeypatch.setattr(api.secrets, "get",
                            lambda name, default="": "file-key"
                            if name == "TR_API_KEY" else default)
        app = api.create_app(Settings(data_dir=str(tmp_path)))
        with TestClient(app) as test_client:
            assert test_client.get(
                "/api/v1/submissions/x",
                headers={"X-API-Key": "file-key"}).status_code == 404


class TestCreateSubmission:
    @pytest.mark.parametrize("body", [
        {},
        {"paths": None},
        {"paths": []},
        {"paths": "abc"},
        {"paths": [1]},
        {"paths": [""]},
        {"paths": [PATH], "dry_run": "yes"},
        {"paths": [PATH], "wait": "yes"},
        {"paths": [PATH], "wait_timeout": "10"},
        {"paths": [PATH], "request_id": "bad id"},
        {"paths": ["no/version/segment/file.md"]},
        [PATH],  # 请求体不是对象
    ])
    def test_bad_body_422(self, open_client, settings, body):
        with open_client(settings) as (client_, _app):
            resp = client_.post("/api/v1/submissions", json=body,
                                headers=HEADERS)
        assert resp.status_code == 422

    def test_missing_or_blank_atomgit_token_422(self, open_client, settings):
        with open_client(settings) as (client_, _app):
            resp = client_.post("/api/v1/submissions", json={"paths": [PATH]},
                                headers={"X-API-Key": API_KEY})
            assert resp.status_code == 422
            resp = client_.post(
                "/api/v1/submissions", json={"paths": [PATH]},
                headers={"X-API-Key": API_KEY,
                         "X-AtomGit-Token": "  "})
            assert resp.status_code == 422

    def test_token_with_inner_whitespace_422(self, open_client, settings):
        with open_client(settings) as (client_, _app):
            resp = client_.post(
                "/api/v1/submissions", json={"paths": [PATH]},
                headers={"X-API-Key": API_KEY, "X-AtomGit-Token": "a b"})
        assert resp.status_code == 422

    def test_too_many_paths_422(self, open_client, settings):
        paths = [f"CANN/9.1.0/hccl/docs/{i}.md" for i in range(6)]
        with open_client(settings) as (client_, _app):
            resp = client_.post("/api/v1/submissions",
                                json={"paths": paths}, headers=HEADERS)
        assert resp.status_code == 422
        assert "上限" in resp.json()["detail"]

    def test_duplicate_paths_deduped(self, open_client, settings):
        body = {"paths": [PATH, PATH], "dry_run": True}
        with open_client(settings) as (client_, _app):
            resp = client_.post("/api/v1/submissions", json=body,
                                headers=HEADERS)
            assert resp.status_code == 202
            payload = resp.json()
            assert payload["path_count"] == 1
            assert len(payload["items"]) == 1
            assert payload["items"][0]["source_paths"] == [PATH]

    def test_grouping_merges_same_repo(self, open_client, settings):
        body = {"paths": [PATH, "CANN/9.1.0/hccl/docs/en/b.md",
                          "CANN/9.1.0/sip/docs/en/a.md"]}
        with open_client(settings) as (client_, _app):
            resp = client_.post("/api/v1/submissions", json=body,
                                headers=HEADERS)
        assert resp.status_code == 202
        items = resp.json()["items"]
        assert len(items) == 2
        hccl = [i for i in items if i["repo"] == "hccl"][0]
        assert hccl["source_paths"] == [PATH, "CANN/9.1.0/hccl/docs/en/b.md"]
        assert hccl["target_paths"] == ["docs/en/a.md", "docs/en/b.md"]

    def test_returns_202_and_get_by_request_id(self, open_client, settings):
        release = threading.Event()

        def blocking(submission_id, token, settings_, db):
            release.wait(5)
            _finish_fake(submission_id, token, settings_, db)

        with open_client(settings, fake=blocking) as (client_, _app):
            resp = client_.post("/api/v1/submissions",
                                json={"paths": [PATH]}, headers=HEADERS)
            assert resp.status_code == 202
            request_id = resp.json()["request_id"]
            assert resp.json()["status"] == "queued"

            got = client_.get(f"/api/v1/submissions/{request_id}",
                              headers={"X-API-Key": API_KEY})
            assert got.status_code == 200
            assert got.json()["request_id"] == request_id
            assert got.json()["status"] in ("queued", "running")

            release.set()
            import time
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                done = client_.get(f"/api/v1/submissions/{request_id}",
                                   headers={"X-API-Key": API_KEY}).json()
                if done["status"] == "success":
                    break
                time.sleep(0.05)
            assert done["status"] == "success"
            assert done["items"][0]["pr_url"].endswith("/merge_requests/1")

    def test_request_id_idempotent(self, open_client, settings):
        body = {"paths": [PATH], "request_id": "abc-123"}
        with open_client(settings) as (client_, app):
            first = client_.post("/api/v1/submissions", json=body,
                                 headers=HEADERS)
            assert first.status_code == 202
            second = client_.post("/api/v1/submissions", json=body,
                                  headers=HEADERS)
            assert second.status_code == 200
            assert second.json()["request_id"] == "abc-123"
            db = app.state.db
            with db._lock:
                rows = db._conn.execute(
                    "SELECT COUNT(*) FROM submissions").fetchone()[0]
            assert rows == 1

    def test_wait_true_returns_terminal_200(self, open_client, settings):
        with open_client(settings) as (client_, _app):
            resp = client_.post(
                "/api/v1/submissions",
                json={"paths": [PATH], "wait": True}, headers=HEADERS)
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == "success"
        assert payload["items"][0]["status"] == "submitted"

    def test_wait_timeout_returns_202(self, open_client, settings):
        release = threading.Event()
        settings.wait_timeout = 1

        def blocking(submission_id, token, settings_, db):
            release.wait(5)
            _finish_fake(submission_id, token, settings_, db)

        with open_client(settings, fake=blocking) as (client_, _app):
            resp = client_.post(
                "/api/v1/submissions",
                json={"paths": [PATH], "wait": True},
                headers=HEADERS)
            assert resp.status_code == 202
            assert resp.json()["status"] == "queued"
            release.set()

    def test_queue_full_429(self, open_client, settings):
        settings.queue_max = 1
        started = threading.Event()
        release = threading.Event()

        def blocking(submission_id, token, settings_, db):
            started.set()
            release.wait(5)

        with open_client(settings, fake=blocking) as (client_, app):
            assert app.state.queue.push(900, "t") is True
            assert started.wait(2)
            assert app.state.queue.push(901, "t") is True  # 占满队列
            resp = client_.post("/api/v1/submissions",
                                json={"paths": [PATH]}, headers=HEADERS)
            assert resp.status_code == 429
            release.set()

    def test_dry_run_flag(self, open_client, settings):
        with open_client(settings) as (client_, _app):
            resp = client_.post(
                "/api/v1/submissions",
                json={"paths": [PATH], "dry_run": True}, headers=HEADERS)
        assert resp.status_code == 202
        assert resp.json()["dry_run"] is True


class TestGetSubmission:
    def test_not_found_404(self, open_client, settings):
        with open_client(settings) as (client_, _app):
            resp = client_.get("/api/v1/submissions/nope",
                               headers={"X-API-Key": API_KEY})
        assert resp.status_code == 404

    def test_include_log(self, open_client, settings):
        with open_client(settings) as (client_, app):
            resp = client_.post("/api/v1/submissions",
                                json={"paths": [PATH], "wait": True},
                                headers=HEADERS)
            request_id = resp.json()["request_id"]
            app.state.db.append_log(
                app.state.db.get_submission_by_request_id(request_id)["id"],
                "hello\n")
            plain = client_.get(f"/api/v1/submissions/{request_id}",
                                headers={"X-API-Key": API_KEY}).json()
            with_log = client_.get(f"/api/v1/submissions/{request_id}",
                                   params={"include_log": "true"},
                                   headers={"X-API-Key": API_KEY}).json()
        assert "log" not in plain
        assert "hello" in with_log["log"]


class TestPrStatus:
    def _pr(self, **overrides):
        data = {
            "number": 12,
            "title": "新增 a.md 英文版文档",
            "html_url": "https://atomgit.com/CANN/hccl/merge_requests/12",
            "state": "open",
            "mergeable": True,
            "draft": False,
            "labels": [],
            "assignees": [{"accept": True}, {"accept": False}],
            "created_at": "2026-10-01T00:00:00Z",
        }
        data.update(overrides)
        return data

    def test_by_pr_url(self, open_client, settings, monkeypatch):
        monkeypatch.setattr(api.client, "get_pull_request",
                            lambda org, repo, number, token: self._pr())
        with open_client(settings) as (client_, _app):
            resp = client_.get(
                "/api/v1/prs/status",
                params={"pr_url": "https://atomgit.com/CANN/hccl/"
                                  "merge_requests/12"},
                headers={"X-API-Key": API_KEY})
        assert resp.status_code == 200
        pr = resp.json()["prs"][0]
        assert (pr["org"], pr["repo"], pr["number"]) == ("CANN", "hccl", 12)
        assert pr["state"] == "open"
        assert pr["review_total"] == 2 and pr["review_accepted"] == 1
        assert pr["derived"]["key"] == "reviewing"

    def test_by_org_repo_number_merged(self, open_client, settings,
                                       monkeypatch):
        monkeypatch.setattr(
            api.client, "get_pull_request",
            lambda org, repo, number, token: self._pr(
                state="closed", merged_at="2026-10-02T00:00:00Z"))
        with open_client(settings) as (client_, _app):
            resp = client_.get("/api/v1/prs/status",
                               params={"org": "CANN", "repo": "hccl",
                                       "number": 12},
                               headers={"X-API-Key": API_KEY})
        pr = resp.json()["prs"][0]
        assert pr["state"] == "merged"
        assert pr["derived"]["key"] == "merged"
        assert resp.json()["errors"] == []

    def test_by_request_id(self, open_client, settings, monkeypatch):
        calls = []

        def fake_get(org, repo, number, token):
            calls.append((org, repo, number, token))
            return self._pr(number=number)

        monkeypatch.setattr(api.client, "get_pull_request", fake_get)
        with open_client(settings) as (client_, app):
            db = app.state.db
            sid = db.create_submission("rid", [PATH], [{
                "org": "CANN", "repo": "hccl", "branch": "9.1.0",
                "source_paths": [PATH],
                "files": [{"source_path": PATH,
                           "target_path": "docs/en/a.md"}]}])
            item_id = db.get_submission_items(sid)[0]["id"]
            db.update_submission_item(item_id, status="submitted",
                                      pr_number=12)
            resp = client_.get("/api/v1/prs/status",
                               params={"request_id": "rid"},
                               headers={"X-API-Key": API_KEY})
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["request_id"] == "rid"
        assert payload["status"] == "queued"
        assert len(payload["prs"]) == 1
        assert calls == [("CANN", "hccl", 12, "service-token")]

    def test_request_id_without_prs_returns_empty(self, open_client, settings):
        with open_client(settings) as (client_, app):
            app.state.db.create_submission("rid2", [PATH], [{
                "org": "CANN", "repo": "hccl", "branch": "9.1.0",
                "source_paths": [PATH],
                "files": [{"source_path": PATH,
                           "target_path": "docs/en/a.md"}]}])
            resp = client_.get("/api/v1/prs/status",
                               params={"request_id": "rid2"},
                               headers={"X-API-Key": API_KEY})
        assert resp.status_code == 200
        assert resp.json()["prs"] == []

    def test_request_id_not_found_404(self, open_client, settings):
        with open_client(settings) as (client_, _app):
            resp = client_.get("/api/v1/prs/status",
                               params={"request_id": "missing"},
                               headers={"X-API-Key": API_KEY})
        assert resp.status_code == 404

    @pytest.mark.parametrize("params", [
        {},
        {"org": "CANN", "repo": "hccl"},           # 缺 number
        {"org": "CANN", "number": 12},             # 缺 repo
        {"org": "A/B", "repo": "hccl", "number": 1},
    ])
    def test_bad_params_422(self, open_client, settings, params):
        with open_client(settings) as (client_, _app):
            resp = client_.get("/api/v1/prs/status", params=params,
                               headers={"X-API-Key": API_KEY})
        assert resp.status_code == 422

    def test_conflicting_params_422(self, open_client, settings):
        with open_client(settings) as (client_, _app):
            resp = client_.get(
                "/api/v1/prs/status",
                params={"pr_url": "https://atomgit.com/CANN/hccl/"
                                  "merge_requests/1",
                        "org": "CANN"},
                headers={"X-API-Key": API_KEY})
        assert resp.status_code == 422

    def test_invalid_pr_url_422(self, open_client, settings):
        with open_client(settings) as (client_, _app):
            resp = client_.get("/api/v1/prs/status",
                               params={"pr_url": "https://example.com/x"},
                               headers={"X-API-Key": API_KEY})
        assert resp.status_code == 422

    def test_pr_not_found_404(self, open_client, settings, monkeypatch):
        def raise_404(org, repo, number, token):
            raise client.AtomGitError(404, "Not Found")

        monkeypatch.setattr(api.client, "get_pull_request", raise_404)
        with open_client(settings) as (client_, _app):
            resp = client_.get("/api/v1/prs/status",
                               params={"org": "CANN", "repo": "hccl",
                                       "number": 1},
                               headers={"X-API-Key": API_KEY})
        assert resp.status_code == 404

    def test_upstream_error_502(self, open_client, settings, monkeypatch):
        def raise_500(org, repo, number, token):
            raise client.AtomGitError(500, "boom")

        monkeypatch.setattr(api.client, "get_pull_request", raise_500)
        with open_client(settings) as (client_, _app):
            resp = client_.get("/api/v1/prs/status",
                               params={"org": "CANN", "repo": "hccl",
                                       "number": 1},
                               headers={"X-API-Key": API_KEY})
        assert resp.status_code == 502

    def test_missing_service_token_503(self, open_client, settings,
                                       monkeypatch):
        monkeypatch.setattr(api.secrets, "get",
                            lambda name, default="": default)
        with open_client(settings) as (client_, _app):
            resp = client_.get("/api/v1/prs/status",
                               params={"org": "CANN", "repo": "hccl",
                                       "number": 1},
                               headers={"X-API-Key": API_KEY})
        assert resp.status_code == 503
