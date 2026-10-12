"""webapp.prsync（PR 状态轮询与影子模式 Jenkins PR 发现）的单元测试。

全部 mock 不触网：client.get_pull_request / list_pull_requests 打桩；
命名 token 经环境变量注入；db 用 tmp_path 真库。
"""

import json
import time
from unittest.mock import MagicMock

import pytest

from translator import client
from webapp import db as db_mod
from webapp import prsync
from webapp.config import Settings

SETTINGS = Settings()


@pytest.fixture()
def db(tmp_path):
    """临时库句柄，测试结束关闭。"""
    d = db_mod.init_db(tmp_path / "data" / "test.db")
    yield d
    d.close()


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    """默认打桩：默认 token 固定、API 不触网。"""
    monkeypatch.setenv("ATOMGIT_TOKEN", "default-token")
    monkeypatch.setattr(prsync.client, "get_pull_request",
                        MagicMock(return_value={"state": "open"}))
    monkeypatch.setattr(prsync.client, "list_pull_requests",
                        MagicMock(return_value=[]))


def _seed_pr(db, org="o1", repo="r1", number=1, path="Ascend/x",
             branch="master", state="open"):
    """种一条 PR 记录（默认 open，供轮询）。"""
    db.upsert_pr(org, repo, number, branch=branch, path=path,
                 url=f"http://x/{org}/{repo}/{number}", title="t",
                 state=state)


def _mock_get(monkeypatch, payload=None, side_effect=None):
    """替换 get_pull_request 桩并返回该 Mock。"""
    get = MagicMock(return_value=payload if side_effect is None else None,
                    side_effect=side_effect)
    monkeypatch.setattr(prsync.client, "get_pull_request", get)
    return get


# ── 状态映射 ──────────────────────────────────────────────────


class TestStateMapping:
    """AtomGit PR 响应 → 落库状态映射（merged/closed/open）。"""

    @pytest.mark.parametrize("payload,expected", [
        ({"state": "closed", "merged": True}, "merged"),
        ({"state": "closed", "merged_at": "2026-09-20T01:02:03+00:00"},
         "merged"),
        ({"state": "closed"}, "closed"),
        ({"state": "open"}, "open"),
    ])
    def test_mapping(self, db, monkeypatch, payload, expected):
        _seed_pr(db)
        _mock_get(monkeypatch, payload=payload)
        prsync._poll_once(db, SETTINGS)
        assert db.list_prs()[0]["state"] == expected

    def test_merged_at_from_response(self, db, monkeypatch):
        """merged 判定时 merged_at 取响应里的时间。"""
        _seed_pr(db)
        ts = "2026-09-20T01:02:03+00:00"
        _mock_get(monkeypatch, payload={"state": "closed", "merged_at": ts})
        prsync._poll_once(db, SETTINGS)
        row = db.list_prs()[0]
        assert row["state"] == "merged"
        assert row["merged_at"] == ts

    def test_mergeable_passthrough(self, db, monkeypatch):
        """mergeable 三态原样透传（None=服务端未计算，存 NULL）。"""
        _seed_pr(db)
        _mock_get(monkeypatch, payload={"state": "open", "mergeable": True})
        prsync._poll_once(db, SETTINGS)
        assert db.list_prs()[0]["mergeable"] == 1
        _mock_get(monkeypatch, payload={"state": "open"})
        prsync._poll_once(db, SETTINGS)
        assert db.list_prs()[0]["mergeable"] is None


# ── Token 路由 ────────────────────────────────────────────────


class TestTokenRouting:
    """仓库配置的命名 token（名称 → Vault）优先于默认 token。"""

    def test_named_token_used_for_configured_repo(self, db, monkeypatch):
        db_mod.set_shadow(db, False)
        monkeypatch.setenv("TR_TOKEN_R1_BOT", "db-token")
        db.upsert_repo_config("O1", "R1", token_name="r1_bot")
        _seed_pr(db, org="o1", repo="r1", number=1, path="Ascend/a")
        _seed_pr(db, org="o2", repo="r2", number=2, path="Ascend/b")
        get = _mock_get(monkeypatch, payload={"state": "open"})
        prsync._poll_once(db, SETTINGS)
        tokens = {(c.args[0], c.args[3]) for c in get.call_args_list}
        assert tokens == {("o1", "db-token"), ("o2", "default-token")}

    def test_missing_named_token_skips_pr(self, db, monkeypatch):
        """配置引用的名称在 Vault 缺失：跳过该 PR（下轮重试），不报错。"""
        db_mod.set_shadow(db, False)
        db.upsert_repo_config("O1", "R1", token_name="r1_bot")
        _seed_pr(db, org="o1", repo="r1", number=1, path="Ascend/a")
        get = _mock_get(monkeypatch, payload={"state": "open"})
        prsync._poll_once(db, SETTINGS)
        get.assert_not_called()


# ── 单轮 20 仓上限 ────────────────────────────────────────────


class TestRepoBudget:
    def test_twenty_repo_limit_per_round(self, db, monkeypatch):
        """21 个仓的 open PR：首轮只处理 20 仓，第 21 仓下轮再处理。"""
        db_mod.set_shadow(db, False)
        for i in range(21):
            _seed_pr(db, org="o", repo=f"r{i:02d}", number=1)
        get = _mock_get(monkeypatch, payload={"state": "open"})

        prsync._poll_once(db, SETTINGS)
        round1 = {c.args[1] for c in get.call_args_list}
        assert len(round1) == prsync.MAX_REPOS_PER_POLL == 20
        skipped = {f"r{i:02d}" for i in range(21)} - round1
        assert len(skipped) == 1

        get.reset_mock()
        prsync._poll_once(db, SETTINGS)
        round2 = {c.args[1] for c in get.call_args_list}
        assert skipped <= round2  # 上轮落下的仓本轮最旧优先处理


# ── 派生状态原始信号提取 ──────────────────────────────────────


class TestSignalExtraction:
    """get_pull_request 真实形态响应中的 labels/draft/assignees 落库。"""

    def test_labels_draft_review_counts_persisted(self, db, monkeypatch):
        _seed_pr(db)
        _mock_get(monkeypatch, payload={
            "state": "open", "mergeable": False, "draft": False,
            "labels": [{"name": "ci-pipeline-failed", "color": "#DB2828"},
                       {"name": "cann-cla/yes", "color": "#21BA45"}],
            "assignees": [{"login": "m1", "name": "评审甲", "accept": True},
                          {"login": "m2", "name": "评审乙", "accept": False}]})
        prsync._poll_once(db, SETTINGS)
        row = db.list_prs()[0]
        labels = json.loads(row["labels_json"])
        assert labels == [{"name": "ci-pipeline-failed", "color": "#DB2828"},
                          {"name": "cann-cla/yes", "color": "#21BA45"}]
        assert row["draft"] == 0
        assert row["review_total"] == 2
        assert row["review_accepted"] == 1

    def test_missing_signal_fields_keep_existing(self, db, monkeypatch):
        """响应缺 labels/draft/assignees 字段时不覆盖库中已有信号。"""
        _seed_pr(db)
        db.apply_pr_state("o1", "r1", 1, "open",
                          labels=[{"name": "ci-pipeline-failed",
                                   "color": None}],
                          draft=True, review_total=2, review_accepted=1)
        _mock_get(monkeypatch, payload={"state": "open"})
        prsync._poll_once(db, SETTINGS)
        row = db.list_prs()[0]
        assert json.loads(row["labels_json"])[0]["name"] == \
            "ci-pipeline-failed"
        assert row["draft"] == 1
        assert row["review_total"] == 2 and row["review_accepted"] == 1

    def test_discovery_carries_labels(self, db, monkeypatch):
        """影子发现：列表项带 labels/draft 时顺带落库。"""
        db_mod.set_shadow(db, True)
        _seed_pr(db, number=99, state="closed")  # 提供已知仓库线索
        monkeypatch.setattr(prsync.client, "list_pull_requests",
                            MagicMock(return_value=[{
                                "number": 5,
                                "head": {"ref": "translator/auto-master"},
                                "base": {"ref": "master"}, "draft": False,
                                "labels": [{"name": "ci-pipeline-failed",
                                            "color": "#DB2828"}]}]))
        prsync._poll_once(db, SETTINGS)
        row = {p["number"]: p for p in db.list_prs()}[5]
        assert json.loads(row["labels_json"])[0]["name"] == \
            "ci-pipeline-failed"
        assert row["draft"] == 0


# ── 影子模式 Jenkins PR 发现 ──────────────────────────────────


class TestShadowDiscovery:
    def test_discovers_jenkins_prs(self, db, monkeypatch):
        """head.ref 前缀 + base 过滤：命中落 source='jenkins'，其余不入库。"""
        db_mod.set_shadow(db, True)
        # 已知仓库线索（closed PR 也提供 org/repo/branch/path）
        _seed_pr(db, org="o1", repo="r1", number=99, path="Ascend/x",
                 branch="master", state="closed")
        lst = MagicMock(return_value=[
            {"number": 5, "title": "msprof 英文版文档",
             "html_url": "http://atomgit/o1/r1/pulls/5",
             "head": {"ref": "translator/auto-master",
                      "label": "bot:translator/auto-master"},
             "base": {"ref": "master"}},
            {"number": 6, "head": {"ref": "feature-x"},
             "base": {"ref": "master"}},
            {"number": 7, "head": {"ref": "translator/auto-dev"},
             "base": {"ref": "dev"}},
            {"number": 8, "head": {"label": "bot:translator/auto-master"},
             "base": {"label": "o1:master"}},
        ])
        monkeypatch.setattr(prsync.client, "list_pull_requests", lst)
        prsync._poll_once(db, SETTINGS)
        prs = {p["number"]: p for p in db.list_prs()}
        assert prs[5]["source"] == "jenkins"
        assert prs[5]["state"] == "open"
        assert prs[5]["branch"] == "master"
        assert prs[5]["url"] == "http://atomgit/o1/r1/pulls/5"
        assert prs[8]["source"] == "jenkins"  # ref 缺失时回退 label 冒号后段
        assert 6 not in prs  # 非 auto 前缀
        assert 7 not in prs  # base 与该仓 branch 不匹配

    def test_discovers_repos_from_recent_runs(self, db, monkeypatch):
        """仅出现在近期 run_repos 中的仓库也参与发现。"""
        db_mod.set_shadow(db, True)
        run_id = db.create_run("push", shadow=True)
        db.record_scan_results(run_id, {
            "changed": [{"project": {"path": "Ascend/y"}, "org": "o2",
                         "repo": "r2", "branch": "dev", "files": []}],
            "unchanged": [], "skipped": [], "errors": []})
        lst = MagicMock(return_value=[
            {"number": 3, "head": {"ref": "translator/auto-dev"},
             "base": {"ref": "dev"}}])
        monkeypatch.setattr(prsync.client, "list_pull_requests", lst)
        prsync._poll_once(db, SETTINGS)
        prs = {p["number"]: p for p in db.list_prs()}
        assert prs[3]["source"] == "jenkins"
        assert prs[3]["branch"] == "dev"

    def test_no_discovery_when_not_shadow(self, db, monkeypatch):
        """非影子模式不做发现（list_pull_requests 零调用）。"""
        db_mod.set_shadow(db, False)
        _seed_pr(db, state="closed")
        lst = prsync.client.list_pull_requests  # autouse 桩
        prsync._poll_once(db, SETTINGS)
        lst.assert_not_called()

    def test_discovery_shares_budget_with_polling(self, db, monkeypatch):
        """轮询打满 20 仓预算时，本轮不再做发现。"""
        db_mod.set_shadow(db, True)
        for i in range(20):
            _seed_pr(db, org="o", repo=f"r{i:02d}", number=1)
        _seed_pr(db, org="o9", repo="extra", number=9, state="closed")
        lst = prsync.client.list_pull_requests
        prsync._poll_once(db, SETTINGS)
        lst.assert_not_called()


# ── 错误隔离 ──────────────────────────────────────────────────


class TestErrorIsolation:
    def test_repo_error_does_not_break_others(self, db, monkeypatch):
        """单仓 AtomGitError 只告警，不影响其他仓与整轮收尾。"""
        db_mod.set_shadow(db, False)
        _seed_pr(db, org="o1", repo="bad", number=1)
        _seed_pr(db, org="o2", repo="good", number=2)

        def fake_get(org, repo, number, token):
            if repo == "bad":
                raise client.AtomGitError(500, "boom")
            return {"state": "open", "mergeable": True}

        get = _mock_get(monkeypatch, side_effect=fake_get)
        prsync._poll_once(db, SETTINGS)
        assert get.call_count == 2
        rows = {r["repo"]: r for r in db.list_prs()}
        assert rows["good"]["mergeable"] == 1
        assert db.kv_get("last_poll_at") is not None

    def test_poll_once_exception_does_not_kill_poller(
            self, db, monkeypatch):
        """整轮异常不炸线程：告警后下轮继续。"""
        monkeypatch.setattr(prsync, "_poll_once",
                            MagicMock(side_effect=RuntimeError("boom")))
        poller = prsync.Poller(db, Settings(poll_interval=3600))
        poller.start()
        poller.stop()
        poller.join(timeout=5)
        assert not poller.is_alive()


# ── 每轮收尾 ──────────────────────────────────────────────────


class TestRoundFinalization:
    def test_last_poll_at_and_cleanup(self, db, monkeypatch):
        """每轮结束写入 last_poll_at，并调用 cleanup_old_runs(90)。"""
        db_mod.set_shadow(db, False)
        cleanup = MagicMock(wraps=db.cleanup_old_runs)
        monkeypatch.setattr(db, "cleanup_old_runs", cleanup)
        prsync._poll_once(db, SETTINGS)
        assert db.kv_get("last_poll_at") is not None
        cleanup.assert_called_once_with(90)


# ── Poller 线程 ───────────────────────────────────────────────


class TestPoller:
    def test_first_round_immediate_and_cooperative_stop(
            self, db, monkeypatch):
        """启动后立即执行首轮；stop() 协作式退出，不等满 poll_interval。"""
        calls = []
        monkeypatch.setattr(prsync, "_poll_once",
                            lambda d, s: calls.append(1))
        poller = prsync.Poller(db, Settings(poll_interval=3600))
        poller.start()
        for _ in range(200):
            if calls:
                break
            time.sleep(0.01)
        poller.stop()
        poller.join(timeout=5)
        assert calls
        assert not poller.is_alive()
