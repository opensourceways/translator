"""webapp.api（webhook 入口 / Dashboard / 管理端点 / 启动恢复）的单元测试。

全部 mock 不触网：Worker/Poller 线程打桩为 MagicMock，停机补偿的引擎
client 调用（get_repo/list_branches）打桩；db 用 tmp_path 真库，
TestClient 上下文触发真实 lifespan（启动恢复 + 关停 join）。
"""

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from webapp import api, db as db_mod
from webapp.config import Settings


def _make_settings(tmp_path, **kw):
    """构造指向临时目录的 Settings。"""
    return Settings(data_dir=str(tmp_path / "data"), **kw)


@contextmanager
def _client(settings, monkeypatch):
    """构建 TestClient 并进入 lifespan；Worker/Poller 一律打桩不起线程。

    Yields:
        tuple: (TestClient, app, worker_mock, poller_mock)。
    """
    worker_cls = MagicMock(name="Worker")
    poller_cls = MagicMock(name="Poller")
    monkeypatch.setattr(api, "Worker", worker_cls)
    monkeypatch.setattr(api, "Poller", poller_cls)
    app = api.create_app(settings)
    with TestClient(app) as client:
        yield client, app, worker_cls.return_value, poller_cls.return_value
    # 关停：两个线程都被协作式停止并 join
    worker_cls.return_value.stop.assert_called_once()
    worker_cls.return_value.join.assert_called_once()
    poller_cls.return_value.stop.assert_called_once()
    poller_cls.return_value.join.assert_called_once()


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    """默认打桩：默认 token 固定；停机补偿的远端查询返回一致 sha（不补偿）。"""
    monkeypatch.setenv("GITCODE_TOKEN", "default-token")
    monkeypatch.setattr(api.client, "get_repo",
                        MagicMock(return_value={"default_branch": "main"}))
    monkeypatch.setattr(api.client, "list_branches",
                        MagicMock(return_value=[]))


def _seed_run(path, status="success", push_after=None, log_text=""):
    """启动前直接往库文件播种一条 run（返回 run id）。"""
    db = db_mod.init_db(path)
    run_id = db.create_run("push", shadow=True, push_before="b" * 40,
                           push_after=push_after)
    if log_text:
        db.append_log(run_id, log_text)
    db.mark_run(run_id, status=status)
    db.close()
    return run_id


def _events(app):
    """库中全部 push_events 行。"""
    return app.state.db._all("SELECT * FROM push_events ORDER BY id")


# ── webhook 鉴权 ──────────────────────────────────────────────


class TestWebhookAuth:
    def test_wrong_token_403(self, tmp_path, monkeypatch):
        settings = _make_settings(tmp_path, webhook_token="wh-secret")
        with _client(settings, monkeypatch) as (client, app, _, _):
            r = client.post("/webhook/gitcode?token=wrong", json={})
            assert r.status_code == 403
            r = client.post("/webhook/gitcode", json={})
            assert r.status_code == 403
            assert _events(app) == []  # 鉴权失败不落事件

    def test_no_webhook_token_fails_closed(self, tmp_path, monkeypatch):
        """webhook_token 未配置时任何 token 都 403（fail closed）。"""
        settings = _make_settings(tmp_path)
        with _client(settings, monkeypatch) as (client, app, _, _):
            r = client.post("/webhook/gitcode?token=anything", json={})
            assert r.status_code == 403
            assert _events(app) == []


# ── webhook ref 过滤与入队 ────────────────────────────────────


class TestWebhookEvents:
    SETTINGS_KW = {"webhook_token": "wh-secret"}
    URL = "/webhook/gitcode?token=wh-secret"

    def _post(self, client, **kw):
        return client.post(self.URL, **kw)

    def test_ref_filtered_not_enqueued(self, tmp_path, monkeypatch):
        """ref 不匹配：accepted=0 落库，不入队，200。"""
        settings = _make_settings(tmp_path, **self.SETTINGS_KW)
        with _client(settings, monkeypatch) as (client, app, _, _):
            r = self._post(client, json={
                "ref": "refs/heads/dev", "before": "b" * 40, "after": "a" * 40})
            assert r.status_code == 200
            assert r.json()["accepted"] is False
            (event,) = _events(app)
            assert event["accepted"] == 0
            assert event["ref"] == "refs/heads/dev"
            assert app.state.queue.pending_count() == 0

    def test_branch_deleted_rejected(self, tmp_path, monkeypatch):
        """after 全零（分支删除）：accepted=0 落库，不入队。"""
        settings = _make_settings(tmp_path, **self.SETTINGS_KW)
        with _client(settings, monkeypatch) as (client, app, _, _):
            r = self._post(client, json={
                "ref": "refs/heads/main", "before": "b" * 40,
                "after": "0" * 40})
            assert r.status_code == 200
            assert r.json()["accepted"] is False
            (event,) = _events(app)
            assert event["accepted"] == 0
            assert app.state.queue.pending_count() == 0

    def test_main_push_accepted(self, tmp_path, monkeypatch):
        """正常 main push：202 + 入队（带增量区间与事件 id）+ 落库。"""
        settings = _make_settings(tmp_path, **self.SETTINGS_KW)
        with _client(settings, monkeypatch) as (client, app, _, _):
            r = self._post(client, json={
                "ref": "refs/heads/main", "before": "b" * 40,
                "after": "a" * 40})
            assert r.status_code == 202
            body = r.json()
            assert body["accepted"] is True and body["queued"] is True
            (event,) = _events(app)
            assert event["accepted"] == 1
            assert event["id"] == body["event_id"]
            assert event["before_sha"] == "b" * 40
            assert app.state.queue.pending_count() == 1
            trigger = app.state.queue.pop(timeout=0)
            assert trigger.trigger == "push"
            assert trigger.before_sha == "b" * 40
            assert trigger.after_sha == "a" * 40
            assert trigger.push_event_ids == [event["id"]]

    def test_plain_main_ref_accepted(self, tmp_path, monkeypatch):
        """ref 为裸 "main"（无 refs/heads/ 前缀）同样命中。"""
        settings = _make_settings(tmp_path, **self.SETTINGS_KW)
        with _client(settings, monkeypatch) as (client, app, _, _):
            r = self._post(client, json={"ref": "main", "after": "a" * 40})
            assert r.status_code == 202

    def test_non_json_body_fallback(self, tmp_path, monkeypatch):
        """非 JSON body：ref 视为空串命中正则，sha 为空 → 入队全量语义。"""
        settings = _make_settings(tmp_path, **self.SETTINGS_KW)
        with _client(settings, monkeypatch) as (client, app, _, _):
            r = self._post(client, content=b"not-json",
                           headers={"content-type": "text/plain"})
            assert r.status_code == 202
            (event,) = _events(app)
            assert event["accepted"] == 1 and event["ref"] == ""
            trigger = app.state.queue.pop(timeout=0)
            assert trigger.before_sha is None and trigger.after_sha is None

    def test_json_missing_fields_fallback(self, tmp_path, monkeypatch):
        """JSON body 缺字段：同样走兜底（ref 空串触发，无 sha）。"""
        settings = _make_settings(tmp_path, **self.SETTINGS_KW)
        with _client(settings, monkeypatch) as (client, app, _, _):
            r = self._post(client, json={"hook_name": "push_hooks"})
            assert r.status_code == 202
            trigger = app.state.queue.pop(timeout=0)
            assert trigger.before_sha is None and trigger.after_sha is None


# ── 管理端点 ──────────────────────────────────────────────────


class TestAdminEndpoints:
    def test_admin_token_empty_open(self, tmp_path, monkeypatch):
        """admin_token 未配置：管理端点放开（内部工具，默认内网部署）。"""
        settings = _make_settings(tmp_path)
        with _client(settings, monkeypatch) as (client, app, _, _):
            r = client.post("/api/runs", json={"full_scan": True})
            assert r.status_code == 202
            r = client.post("/api/config/shadow", json={"enabled": False})
            assert r.status_code == 200
            assert app.state.queue.pending_count() == 1

    def test_admin_token_wrong_403(self, tmp_path, monkeypatch):
        """admin_token 配置后：token 错误/缺失一律 403。"""
        settings = _make_settings(tmp_path, admin_token="adm")
        with _client(settings, monkeypatch) as (client, app, _, _):
            r = client.post("/api/runs", json={"full_scan": True})
            assert r.status_code == 403
            r = client.post("/api/runs", json={"full_scan": True},
                            headers={"X-Admin-Token": "wrong"})
            assert r.status_code == 403
            assert app.state.queue.pending_count() == 0

    def test_manual_run_enqueued(self, tmp_path, monkeypatch):
        """token 正确：202 入队；关键字触发带 keyword（manual 语义非全量）。"""
        settings = _make_settings(tmp_path, admin_token="adm")
        headers = {"X-Admin-Token": "adm"}
        with _client(settings, monkeypatch) as (client, app, _, _):
            r = client.post("/api/runs", json={"full_scan": True},
                            headers=headers)
            assert r.status_code == 202
            trigger = app.state.queue.pop(timeout=0)
            assert trigger.trigger == "manual" and trigger.full_scan
            r = client.post("/api/runs", json={"keyword": "Ascend"},
                            headers=headers)
            assert r.status_code == 202
            trigger = app.state.queue.pop(timeout=0)
            assert trigger.keyword == "Ascend" and not trigger.full_scan
            # token 错误 403
            r = client.post("/api/runs", json={},
                            headers={"X-Admin-Token": "wrong"})
            assert r.status_code == 403

    def test_shadow_toggle_persisted(self, tmp_path, monkeypatch):
        """shadow 开关持久化：库里 config 值随切换变化。"""
        settings = _make_settings(tmp_path, admin_token="adm")
        headers = {"X-Admin-Token": "adm"}
        with _client(settings, monkeypatch) as (client, app, _, _):
            db = app.state.db
            assert db_mod.is_shadow(db) is True  # 默认 True
            r = client.post("/api/config/shadow", json={"enabled": False},
                            headers=headers)
            assert r.status_code == 200 and r.json()["shadow"] is False
            assert db.kv_get("shadow") == "0"
            assert db_mod.is_shadow(db) is False
            r = client.post("/api/config/shadow", json={"enabled": True},
                            headers=headers)
            assert r.json()["shadow"] is True
            assert db.kv_get("shadow") == "1"

    def test_shadow_toggle_bad_body_422(self, tmp_path, monkeypatch):
        settings = _make_settings(tmp_path, admin_token="adm")
        with _client(settings, monkeypatch) as (client, app, _, _):
            r = client.post("/api/config/shadow", json={},
                            headers={"X-Admin-Token": "adm"})
            assert r.status_code == 422


# ── dash token ────────────────────────────────────────────────


class TestDashToken:
    def test_open_when_unset(self, tmp_path, monkeypatch):
        """dash_token 未配置：只读端点放开。"""
        settings = _make_settings(tmp_path)
        with _client(settings, monkeypatch) as (client, app, _, _):
            assert client.get("/").status_code == 200
            assert client.get("/api/summary").status_code == 200

    def test_required_when_set(self, tmp_path, monkeypatch):
        """dash_token 配置后：无 token 403；query 或头任一正确 200。"""
        settings = _make_settings(tmp_path, dash_token="dash-secret")
        with _client(settings, monkeypatch) as (client, app, _, _):
            assert client.get("/").status_code == 403
            assert client.get("/api/summary").status_code == 403
            assert client.get("/?token=dash-secret").status_code == 200
            r = client.get("/api/summary",
                           headers={"X-Dash-Token": "dash-secret"})
            assert r.status_code == 200
            r = client.get("/api/summary",
                           headers={"X-Dash-Token": "wrong"})
            assert r.status_code == 403
            # healthz 永远无鉴权
            assert client.get("/healthz").status_code == 200


# ── healthz ───────────────────────────────────────────────────


class TestHealthz:
    def test_fields(self, tmp_path, monkeypatch):
        """字段齐全：status/shadow/busy/pending/last_run_at/last_poll_at。"""
        settings = _make_settings(tmp_path)
        with _client(settings, monkeypatch) as (client, app, _, _):
            r = client.get("/healthz")
            assert r.status_code == 200
            body = r.json()
            assert body["status"] == "ok"
            assert body["shadow"] is True
            assert body["busy"] is False
            assert body["pending"] == 0
            assert body["last_run_at"] is None
            assert body["last_poll_at"] is None

    def test_busy_from_db_running_run(self, tmp_path, monkeypatch):
        """busy 由库中 running run 判定（重启后也准确）；kv 时间戳透传。"""
        settings = _make_settings(tmp_path)
        with _client(settings, monkeypatch) as (client, app, _, _):
            db = app.state.db
            db.create_run("push", shadow=True)  # running
            db.kv_set("last_run_at", "2026-09-29T01:00:00+00:00")
            db.kv_set("last_poll_at", "2026-09-29T01:05:00+00:00")
            body = client.get("/healthz").json()
            assert body["busy"] is True
            assert body["last_run_at"] == "2026-09-29T01:00:00+00:00"
            assert body["last_poll_at"] == "2026-09-29T01:05:00+00:00"


# ── Dashboard 页面 ────────────────────────────────────────────


class TestPages:
    def test_index_shows_run(self, tmp_path, monkeypatch):
        """首页 200 且含 run 链接与状态。"""
        settings = _make_settings(tmp_path)
        _seed_run(settings.db_path, status="success")
        with _client(settings, monkeypatch) as (client, app, _, _):
            r = client.get("/")
            assert r.status_code == 200
            assert "/runs/1" in r.text and "success" in r.text

    def test_run_detail_shows_repos_and_log(self, tmp_path, monkeypatch):
        """run 详情页 200：含 run_repos 明细（PR 链接）与日志。"""
        settings = _make_settings(tmp_path)
        run_id = _seed_run(settings.db_path, log_text="扫描完成\n")
        db = db_mod.init_db(settings.db_path)
        db.record_scan_results(run_id, {
            "changed": [{"project": {"path": "Ascend/x"}, "org": "o1",
                         "repo": "r1", "branch": "master", "files": []}],
            "unchanged": [], "skipped": [], "errors": []})
        db.record_submit_results(run_id, [{
            "ok": True, "path": "Ascend/x", "org": "o1", "repo": "r1",
            "branch": "master", "pr_number": 7,
            "pr_url": "http://gitcode/o1/r1/pulls/7"}])
        db.close()
        with _client(settings, monkeypatch) as (client, app, _, _):
            r = client.get(f"/runs/{run_id}")
            assert r.status_code == 200
            assert "Ascend/x" in r.text
            assert "http://gitcode/o1/r1/pulls/7" in r.text
            assert "扫描完成" in r.text
            assert client.get("/runs/999").status_code == 404

    def _seed_repo_run(self, db_path, shadow=True):
        """播种一条含 o1/r1@master changed 记录的已完成 run，返回 run id。"""
        run_id = _seed_run(db_path)
        db = db_mod.init_db(db_path)
        db.record_scan_results(run_id, {
            "changed": [{"project": {"path": "Ascend/x"}, "org": "o1",
                         "repo": "r1", "branch": "master", "files": []}],
            "unchanged": [], "skipped": [], "errors": []}, shadow=shadow)
        db.close()
        return run_id

    def test_prs_page_grouped_with_latest_status(self, tmp_path, monkeypatch):
        """PR 页按 社区→仓库 分组：仓库卡片含最新 run 状态徽章与 PR 行。"""
        settings = _make_settings(tmp_path)
        run_id = self._seed_repo_run(settings.db_path)
        with _client(settings, monkeypatch) as (client, app, _, _):
            app.state.db.upsert_pr(
                "o1", "r1", 42, branch="master", path="Ascend/x",
                url="http://gitcode/o1/r1/pulls/42", title="t", state="open")
            r = client.get("/prs")
            assert r.status_code == 200
            assert "http://gitcode/o1/r1/pulls/42" in r.text
            assert "repo-card" in r.text
            assert "would_submit" in r.text  # 最新 run 状态徽章
            assert f"/runs/{run_id}" in r.text  # 徽章链接到 run 详情页
            assert "open 1" in r.text  # 社区 open 计数徽标
            # tab 结构：社区 tab 栏 + 仓库子 tab
            assert 'id="org-tabs"' in r.text
            assert 'class="tab active" data-org="o1"' in r.text
            assert 'data-repo="r1|master"' in r.text

    def test_prs_page_known_repo_without_pr(self, tmp_path, monkeypatch):
        """已知仓库（有 run 记录）但无任何 PR：显示「未发现 PR」。"""
        settings = _make_settings(tmp_path)
        self._seed_repo_run(settings.db_path)
        with _client(settings, monkeypatch) as (client, app, _, _):
            r = client.get("/prs")
            assert r.status_code == 200
            assert "repo-card" in r.text and "r1" in r.text
            assert "未发现 PR" in r.text

    def test_prs_page_empty_db(self, tmp_path, monkeypatch):
        """空库：友好空态，不报错。"""
        settings = _make_settings(tmp_path)
        with _client(settings, monkeypatch) as (client, app, _, _):
            r = client.get("/prs")
            assert r.status_code == 200
            assert "暂无数据" in r.text

    def test_prs_page_shows_derived_status(self, tmp_path, monkeypatch):
        """PR 行含派生状态徽章（颜色类）与指引小字。"""
        settings = _make_settings(tmp_path)
        self._seed_repo_run(settings.db_path)
        with _client(settings, monkeypatch) as (client, app, _, _):
            app.state.db.upsert_pr(
                "o1", "r1", 42, branch="master", state="open",
                labels=[{"name": "ci-pipeline-failed", "color": "#DB2828"}])
            r = client.get("/prs")
            assert r.status_code == 200
            assert "badge d-red" in r.text
            assert "CI 未通过" in r.text
            assert "点开 PR 查看 CI 日志" in r.text  # 指引文案
            assert "创建时间" in r.text  # PR 创建时间列

    def test_api_prs_derived(self, tmp_path, monkeypatch):
        """/api/prs 平铺与 ?group_by=org 的每个 PR 均带 derived 派生状态。"""
        settings = _make_settings(tmp_path)
        self._seed_repo_run(settings.db_path)
        with _client(settings, monkeypatch) as (client, app, _, _):
            db = app.state.db
            db.upsert_pr("o1", "r1", 2, branch="master", state="open",
                         mergeable=True)
            db.upsert_pr("o1", "r1", 3, branch="master", state="open")
            db.apply_pr_state("o1", "r1", 3, "open",
                              review_total=2, review_accepted=1)
            flat = client.get("/api/prs").json()["prs"]
            derived = {p["number"]: p["derived"] for p in flat}
            assert derived[2]["key"] == "mergeable"
            assert derived[2]["color"] == "green"
            assert derived[3]["key"] == "reviewing"
            assert "1/2" in derived[3]["guidance"]  # 评审进度计数
            grouped = client.get("/api/prs?group_by=org").json()
            prs = grouped["orgs"][0]["repos"][0]["prs"]
            assert all("derived" in p for p in prs)
            assert prs[0]["derived"]["guidance"]

    def test_api_prs_group_by_org(self, tmp_path, monkeypatch):
        """?group_by=org 返回分组结构；默认仍返回平铺（兼容）。"""
        settings = _make_settings(tmp_path)
        run_id = self._seed_repo_run(settings.db_path)
        with _client(settings, monkeypatch) as (client, app, _, _):
            db = app.state.db
            db.upsert_pr("o1", "r1", 2, branch="master", state="open")
            db.upsert_pr("o1", "r1", 5, branch="master", state="merged")
            body = client.get("/api/prs?group_by=org").json()
            (org,) = body["orgs"]
            assert org["org"] == "o1"
            assert org["counts"] == {"open": 1, "merged": 1, "closed": 0}
            (g,) = org["repos"]
            assert g["repo"] == "r1" and g["branch"] == "master"
            assert g["latest"]["status"] == "would_submit"
            assert g["latest"]["run_id"] == run_id
            assert g["latest"]["finished_at"] is not None
            assert [p["number"] for p in g["prs"]] == [5, 2]  # number 倒序
            # 默认平铺结构不变
            flat = client.get("/api/prs").json()
            assert "prs" in flat and len(flat["prs"]) == 2

    def test_group_by_org_case_insensitive(self, tmp_path, monkeypatch):
        """org/repo 大小写不敏感分组：run 状态（CANN/HCCL）与 PR
        （cann/hccl）合并为同一社区同一仓库组，显示名取先见者。"""
        settings = _make_settings(tmp_path)
        run_id = _seed_run(settings.db_path)
        db = db_mod.init_db(settings.db_path)
        db.record_scan_results(run_id, {
            "changed": [{"project": {"path": "CANN/x"}, "org": "CANN",
                         "repo": "HCCL", "branch": "master", "files": []}],
            "unchanged": [], "skipped": [], "errors": []}, shadow=True)
        db.close()
        with _client(settings, monkeypatch) as (client, app, _, _):
            app.state.db.upsert_pr("cann", "hccl", 9, branch="master",
                                   state="open")
            body = client.get("/api/prs?group_by=org").json()
            (org,) = body["orgs"]  # 只有一个社区
            assert org["counts"]["open"] == 1
            (g,) = org["repos"]  # 只有一个仓库组：latest 与 PR 合并
            assert g["latest"]["status"] == "would_submit"
            assert [p["number"] for p in g["prs"]] == [9]

    def test_api_readonly(self, tmp_path, monkeypatch):
        """只读 JSON：summary/runs/run 详情（含 repos，不含 log）/log/prs。"""
        settings = _make_settings(tmp_path)
        run_id = _seed_run(settings.db_path, log_text="hello\n")
        with _client(settings, monkeypatch) as (client, app, _, _):
            app.state.db.upsert_pr("o1", "r1", 1, url="http://x/1")
            assert client.get("/api/summary").json()["runs_total"] == 1
            runs = client.get("/api/runs").json()["runs"]
            assert runs[0]["id"] == run_id and "log" not in runs[0]
            detail = client.get(f"/api/runs/{run_id}").json()
            assert detail["run"]["id"] == run_id
            assert "log" not in detail["run"] and detail["repos"] == []
            assert client.get(f"/api/runs/{run_id}/log").json()["log"] \
                == "hello\n"
            assert client.get("/api/runs/999").status_code == 404
            prs = client.get("/api/prs").json()["prs"]
            assert prs[0]["number"] == 1


# ── 设置页与自动提 PR 开关端点 ────────────────────────────────


class TestRepoSettings:
    """仓库配置端点：一个条目配齐开关与专属 Token；token 明文永不出库。"""

    SECRET = "tok-plaintext-secret"

    def test_settings_page_lists_entries(self, tmp_path, monkeypatch):
        settings = _make_settings(tmp_path)
        with _client(settings, monkeypatch) as (client, app, _, _):
            app.state.db.upsert_repo_config("cann", "hccl")
            r = client.get("/settings")
            assert r.status_code == 200
            assert "仓库配置" in r.text
            assert "cann" in r.text and "hccl" in r.text
            assert 'href="/settings"' in client.get("/").text  # 导航入口

    def test_settings_page_shows_known_repo_suggestions(
            self, tmp_path, monkeypatch):
        """有 run 记录但未添加条目的仓库出现在快捷建议中。"""
        settings = _make_settings(tmp_path)
        TestPages()._seed_repo_run(settings.db_path)
        with _client(settings, monkeypatch) as (client, app, _, _):
            r = client.get("/settings")
            assert r.status_code == 200
            assert 'data-org="o1"' in r.text and 'data-repo="r1"' in r.text
            # 添加条目后不再出现在建议中
            app.state.db.upsert_repo_config("o1", "r1")
            assert 'class="btn small sugg"' not in client.get("/settings").text

    def test_mutations_open_when_admin_token_unset(
            self, tmp_path, monkeypatch):
        """admin_token 未配置：修改端点放开（内网部署模式）。"""
        settings = _make_settings(tmp_path)
        with _client(settings, monkeypatch) as (client, app, _, _):
            r = client.post("/api/settings/repos",
                            json={"org": "o", "repo": "r"})
            assert r.status_code == 200
            entry_id = r.json()["id"]
            assert client.post(f"/api/settings/repos/{entry_id}",
                               json={"enabled": False}).status_code == 200
            assert client.delete(
                f"/api/settings/repos/{entry_id}").status_code == 200
            # 只读列表走 dash 鉴权（未配置时放开）
            assert client.get("/api/settings/repos").status_code == 200

    def test_mutations_403_with_wrong_admin_token(
            self, tmp_path, monkeypatch):
        """admin_token 配置后：错误 token 一律 403。"""
        settings = _make_settings(tmp_path, admin_token="adm")
        with _client(settings, monkeypatch) as (client, app, _, _):
            assert client.post("/api/settings/repos",
                               json={"org": "o", "repo": "r"}
                               ).status_code == 403
            assert client.post("/api/settings/repos/1",
                               json={"enabled": True}).status_code == 403
            assert client.delete("/api/settings/repos/1").status_code == 403

    def test_add_toggle_delete_flow(self, tmp_path, monkeypatch):
        settings = _make_settings(tmp_path, admin_token="adm")
        headers = {"X-Admin-Token": "adm"}
        with _client(settings, monkeypatch) as (client, app, _, _):
            client.post("/api/settings/tokens",
                        json={"name": "hccl-bot", "token": self.SECRET},
                        headers=headers)
            r = client.post("/api/settings/repos",
                            json={"org": "CANN", "repo": "HCCL",
                                  "token_name": "hccl-bot"},
                            headers=headers)
            assert r.status_code == 200
            entry = r.json()
            assert entry["org"] == "cann" and entry["repo"] == "hccl"
            db = app.state.db
            assert db.is_auto_pr_enabled("cann", "hccl") is True
            assert db.get_repo_token("cann", "hccl") == self.SECRET
            # 列表回 token_name 引用，无 token 字段
            entries = client.get("/api/settings/repos").json()["entries"]
            assert entries[0]["token_name"] == "hccl-bot"
            assert "token" not in entries[0]
            # 不带 token_name 的 upsert 保留已有引用
            r = client.post("/api/settings/repos",
                            json={"org": "cann", "repo": "hccl"},
                            headers=headers)
            assert r.json()["id"] == entry["id"]
            assert db.get_repo_token("cann", "hccl") == self.SECRET
            # 引用不存在的命名 token：422
            assert client.post("/api/settings/repos",
                               json={"org": "cann", "repo": "hccl",
                                     "token_name": "nope"},
                               headers=headers).status_code == 422
            # 切换开关
            r = client.post(f"/api/settings/repos/{entry['id']}",
                            json={"enabled": False}, headers=headers)
            assert r.json()["enabled"] is False
            assert db.is_auto_pr_enabled("cann", "hccl") is False
            # 删除
            r = client.delete(f"/api/settings/repos/{entry['id']}",
                              headers=headers)
            assert r.json()["deleted"] is True
            assert db.list_repo_configs() == []

    def test_add_validation_422(self, tmp_path, monkeypatch):
        settings = _make_settings(tmp_path, admin_token="adm")
        headers = {"X-Admin-Token": "adm"}
        with _client(settings, monkeypatch) as (client, app, _, _):
            assert client.post("/api/settings/repos", json={"org": "o"},
                               headers=headers).status_code == 422
            assert client.post("/api/settings/repos", json={},
                               headers=headers).status_code == 422
            assert client.post("/api/settings/repos",
                               json={"org": "o", "repo": "r",
                                     "enabled": "yes"},
                               headers=headers).status_code == 422

    def test_toggle_404_and_422(self, tmp_path, monkeypatch):
        settings = _make_settings(tmp_path, admin_token="adm")
        headers = {"X-Admin-Token": "adm"}
        with _client(settings, monkeypatch) as (client, app, _, _):
            assert client.post("/api/settings/repos/999",
                               json={"enabled": True},
                               headers=headers).status_code == 404
            assert client.post("/api/settings/repos/1", json={},
                               headers=headers).status_code == 422
            assert client.delete("/api/settings/repos/999",
                                 headers=headers).status_code == 404


# ── 启动恢复与停机补偿 ────────────────────────────────────────


class TestStartupRecovery:
    def test_zombie_running_interrupted(self, tmp_path, monkeypatch):
        """残留的 running run 启动时被标记为 interrupted。"""
        settings = _make_settings(tmp_path)
        db = db_mod.init_db(settings.db_path)
        db.create_run("push", shadow=True)  # running（僵尸）
        db.close()
        with _client(settings, monkeypatch) as (client, app, _, _):
            run = app.state.db.get_run(1)
            assert run["status"] == "interrupted"

    def test_no_baseline_skips(self, tmp_path, monkeypatch):
        """无基线（全新库）：不查远端、不入队，正常启动。"""
        settings = _make_settings(tmp_path)
        with _client(settings, monkeypatch) as (client, app, _, _):
            api.client.get_repo.assert_not_called()
            assert app.state.queue.pending_count() == 0
            assert client.get("/healthz").status_code == 200

    def test_same_sha_skips(self, tmp_path, monkeypatch):
        """远端 HEAD 与基线一致：不入队。"""
        settings = _make_settings(tmp_path)
        _seed_run(settings.db_path, status="success", push_after="c" * 40)
        monkeypatch.setattr(api.client, "list_branches", MagicMock(
            return_value=[{"name": "main", "commit": {"sha": "c" * 40}}]))
        with _client(settings, monkeypatch) as (client, app, _, _):
            api.client.get_repo.assert_called_once()
            assert app.state.queue.pending_count() == 0

    def test_diverged_sha_enqueues_full_scan(self, tmp_path, monkeypatch):
        """远端 HEAD 领先基线：自动入队一次全量扫描。"""
        settings = _make_settings(tmp_path)
        _seed_run(settings.db_path, status="success", push_after="c" * 40)
        monkeypatch.setattr(api.client, "list_branches", MagicMock(
            return_value=[{"name": "main", "commit": {"sha": "d" * 40}}]))
        with _client(settings, monkeypatch) as (client, app, _, _):
            assert app.state.queue.pending_count() == 1
            trigger = app.state.queue.pop(timeout=0)
            assert trigger.full_scan and trigger.trigger == "manual"

    def test_network_error_never_blocks_startup(self, tmp_path, monkeypatch):
        """比对失败（网络异常）：跳过补偿，不影响启动。"""
        settings = _make_settings(tmp_path)
        _seed_run(settings.db_path, status="success", push_after="c" * 40)
        monkeypatch.setattr(api.client, "get_repo",
                            MagicMock(side_effect=ConnectionError("down")))
        with _client(settings, monkeypatch) as (client, app, _, _):
            assert app.state.queue.pending_count() == 0
            assert client.get("/healthz").json()["status"] == "ok"


class TestNamedTokenSettings:
    """命名 Token 池端点：明文永不出库（列表与页面均为掩码）。"""

    SECRET = "named-plaintext-secret"

    def test_add_list_masked_delete_flow(self, tmp_path, monkeypatch):
        settings = _make_settings(tmp_path)
        with _client(settings, monkeypatch) as (client, app, _, _):
            r = client.post("/api/settings/tokens",
                            json={"name": "Cann-Bot", "token": self.SECRET})
            assert r.status_code == 200
            assert r.json()["name"] == "cann-bot"  # 小写规范化
            entry_id = r.json()["id"]
            tokens = app.state.db.list_named_tokens()
            assert tokens[0]["token"] == self.SECRET
            # 列表只回掩码，明文不出现在响应里
            body = client.get("/api/settings/tokens")
            assert self.SECRET not in body.text
            entries = body.json()["entries"]
            assert entries[0]["token_masked"] == "name***"
            assert "token" not in entries[0]
            # 设置页同样只渲染掩码；仓库表单下拉包含该名称
            page = client.get("/settings")
            assert "Token 池" in page.text
            assert self.SECRET not in page.text
            assert "name***" in page.text
            assert 'value="cann-bot"' in page.text
            # 删除
            r = client.delete(f"/api/settings/tokens/{entry_id}")
            assert r.json()["deleted"] is True
            assert app.state.db.list_named_tokens() == []

    def test_add_validation_422_and_404(self, tmp_path, monkeypatch):
        settings = _make_settings(tmp_path)
        with _client(settings, monkeypatch) as (client, app, _, _):
            assert client.post("/api/settings/tokens",
                               json={"name": "n"}).status_code == 422
            assert client.post("/api/settings/tokens",
                               json={"token": "t"}).status_code == 422
            assert client.delete("/api/settings/tokens/999"
                                 ).status_code == 404

    def test_403_with_wrong_admin_token(self, tmp_path, monkeypatch):
        settings = _make_settings(tmp_path, admin_token="adm")
        with _client(settings, monkeypatch) as (client, app, _, _):
            assert client.post("/api/settings/tokens",
                               json={"name": "n", "token": "t"}
                               ).status_code == 403
            assert client.delete("/api/settings/tokens/1").status_code == 403
            assert client.get("/api/settings/tokens").status_code == 200




# ── 失败重试（POST /api/runs/{id}/retry） ─────────────────────


def _seed_failed_run(db_path, paths, run_status="partial"):
    """播种一个含失败仓库结果的 run：每个路径一行 submit_failed。

    走真实 record_scan_results + record_submit_results 落库路径。

    Returns:
        int: run id。
    """
    db = db_mod.init_db(db_path)
    run_id = db.create_run("manual", shadow=False)
    scan = {"changed": [
        {"project": {"path": p}, "org": "Org", "repo": "repo",
         "branch": "main", "files": [{"target_path": "a.md"}],
         "new_count": 1, "update_count": 0}
        for p in paths]}
    db.record_scan_results(run_id, scan, shadow=False)
    db.record_submit_results(run_id, [
        {"path": p, "ok": False, "stage": "push", "error": "boom"}
        for p in paths])
    db.mark_run(run_id, status=run_status)
    db.close()
    return run_id


class TestRetryRun:
    def test_retry_single_path_keyword(self, tmp_path, monkeypatch):
        """带 path 重试：入队该路径的关键字扫（非全量）。"""
        run_id = _seed_failed_run(tmp_path / "data" / "translator.db",
                                  ["Ascend/MindCluster", "Kunpeng/docs"])
        settings = _make_settings(tmp_path)
        with _client(settings, monkeypatch) as (client, app, _, _):
            r = client.post(f"/api/runs/{run_id}/retry",
                            json={"path": "Ascend/MindCluster"})
            assert r.status_code == 202
            assert r.json()["keyword"] == "Ascend/MindCluster"
            trigger = app.state.queue.pop(timeout=0)
            assert trigger.keyword == "Ascend/MindCluster"
            assert not trigger.full_scan

    def test_retry_all_single_failed_path(self, tmp_path, monkeypatch):
        """不带 path 且只有一个失败路径：入队关键字扫。"""
        run_id = _seed_failed_run(tmp_path / "data" / "translator.db",
                                  ["Ascend/MindCluster"])
        settings = _make_settings(tmp_path)
        with _client(settings, monkeypatch) as (client, app, _, _):
            r = client.post(f"/api/runs/{run_id}/retry", json={})
            assert r.status_code == 202
            trigger = app.state.queue.pop(timeout=0)
            assert trigger.keyword == "Ascend/MindCluster"

    def test_retry_all_multiple_paths_full_scan(self, tmp_path, monkeypatch):
        """多个失败路径：关键字互不覆盖，入队全量扫（宁多勿漏）。"""
        run_id = _seed_failed_run(tmp_path / "data" / "translator.db",
                                  ["Ascend/MindCluster", "Kunpeng/docs"])
        settings = _make_settings(tmp_path)
        with _client(settings, monkeypatch) as (client, app, _, _):
            r = client.post(f"/api/runs/{run_id}/retry")
            assert r.status_code == 202
            assert r.json()["full_scan"] is True
            trigger = app.state.queue.pop(timeout=0)
            assert trigger.full_scan and not trigger.keyword

    def test_retry_no_failed_rows_422(self, tmp_path, monkeypatch):
        """run 无失败项：422 且不入队。"""
        run_id = _seed_run(tmp_path / "data" / "translator.db")
        settings = _make_settings(tmp_path)
        with _client(settings, monkeypatch) as (client, app, _, _):
            r = client.post(f"/api/runs/{run_id}/retry", json={})
            assert r.status_code == 422
            assert app.state.queue.pending_count() == 0

    def test_retry_path_not_failed_422(self, tmp_path, monkeypatch):
        """path 不属于该 run 的失败项：422。"""
        run_id = _seed_failed_run(tmp_path / "data" / "translator.db",
                                  ["Ascend/MindCluster"])
        settings = _make_settings(tmp_path)
        with _client(settings, monkeypatch) as (client, app, _, _):
            r = client.post(f"/api/runs/{run_id}/retry",
                            json={"path": "Ascend/Other"})
            assert r.status_code == 422
            assert app.state.queue.pending_count() == 0

    def test_retry_running_conflict_409(self, tmp_path, monkeypatch):
        """run 仍在执行中：409 不入队。"""
        run_id = _seed_failed_run(tmp_path / "data" / "translator.db",
                                  ["Ascend/MindCluster"])
        settings = _make_settings(tmp_path)
        with _client(settings, monkeypatch) as (client, app, _, _):
            # 启动恢复会把播种的 running 标记为 interrupted，
            # 进入 lifespan 后再置回 running 模拟执行中
            app.state.db.mark_run(run_id, status="running")
            r = client.post(f"/api/runs/{run_id}/retry", json={})
            assert r.status_code == 409
            assert app.state.queue.pending_count() == 0

    def test_retry_unknown_run_404(self, tmp_path, monkeypatch):
        settings = _make_settings(tmp_path)
        with _client(settings, monkeypatch) as (client, _, _, _):
            assert client.post("/api/runs/999/retry",
                               json={}).status_code == 404

    def test_retry_requires_admin_token(self, tmp_path, monkeypatch):
        """配置 admin_token 后：缺/错 token 一律 403。"""
        run_id = _seed_failed_run(tmp_path / "data" / "translator.db",
                                  ["Ascend/MindCluster"])
        settings = _make_settings(tmp_path, admin_token="adm")
        with _client(settings, monkeypatch) as (client, app, _, _):
            assert client.post(f"/api/runs/{run_id}/retry",
                               json={}).status_code == 403
            assert app.state.queue.pending_count() == 0
            r = client.post(f"/api/runs/{run_id}/retry", json={},
                            headers={"X-Admin-Token": "adm"})
            assert r.status_code == 202
