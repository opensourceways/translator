"""service 模块的单元测试。

覆盖 Token 规则（resolve_token / load_token_rules）、扫描对比
（scan_changed）和幂等提交（submit_repo / submit_all）。
测试通过 mock service.client 完全控制下游 GitCode API 依赖，
不发起真实网络请求。
"""

import base64
import json
from unittest.mock import MagicMock, patch

import pytest

from translator_ci import service
from translator_ci.client import GitCodeError

SILENT = lambda msg: None  # 测试中静默日志


def _entry(name, path, entry_type):
    """构造目录条目。"""
    return {"name": name, "path": path, "type": entry_type}


# 权威仓库的模拟目录树（固定层级：组织/版本/产品/仓库/目标路径）
FAKE_TREE = {
    "": [
        _entry("Ascend", "Ascend", "dir"),
        _entry("CANN", "CANN", "dir"),
        _entry("README.md", "README.md", "file"),
    ],
    "Ascend": [
        _entry("26.1.0", "Ascend/26.1.0", "dir"),
        _entry("master", "Ascend/master", "dir"),
    ],
    "Ascend/26.1.0": [_entry("MindStudio", "Ascend/26.1.0/MindStudio", "dir")],
    "Ascend/26.1.0/MindStudio": [
        _entry("msprof", "Ascend/26.1.0/MindStudio/msprof", "dir"),
        _entry("mspti", "Ascend/26.1.0/MindStudio/mspti", "dir"),
    ],
    "Ascend/26.1.0/MindStudio/msprof": [
        _entry("test", "Ascend/26.1.0/MindStudio/msprof/test", "file"),
    ],
    "Ascend/26.1.0/MindStudio/mspti": [
        _entry("test", "Ascend/26.1.0/MindStudio/mspti/test", "file"),
    ],
    "Ascend/master": [_entry("MindStudio", "Ascend/master/MindStudio", "dir")],
    "Ascend/master/MindStudio": [
        _entry("msprof", "Ascend/master/MindStudio/msprof", "dir"),
    ],
    "Ascend/master/MindStudio/msprof": [
        _entry("test", "Ascend/master/MindStudio/msprof/test", "file"),
    ],
    "CANN": [_entry("9.1.0", "CANN/9.1.0", "dir")],
    "CANN/9.1.0": [_entry("Toolkit", "CANN/9.1.0/Toolkit", "dir")],
    "CANN/9.1.0/Toolkit": [_entry("cann", "CANN/9.1.0/Toolkit/cann", "dir")],
    "CANN/9.1.0/Toolkit/cann": [
        _entry("README.md", "CANN/9.1.0/Toolkit/cann/README.md", "file"),
    ],
}

# 模拟树中的仓库级目录（共 4 个）
REPO_PATHS = [
    "Ascend/26.1.0/MindStudio/msprof",
    "Ascend/26.1.0/MindStudio/mspti",
    "Ascend/master/MindStudio/msprof",
    "CANN/9.1.0/Toolkit/cann",
]


def _make_client_mock():
    """构造 mock 的 client：get_repo 返回默认分支，list_dir 按模拟树返回。"""
    client_mock = MagicMock()
    client_mock.GitCodeError = GitCodeError
    client_mock.get_repo.return_value = {"default_branch": "master"}
    client_mock.list_dir.side_effect = (
        lambda owner, repo, path, ref, token: FAKE_TREE[path]
    )
    return client_mock


def _make_scan_client_mock():
    """构造扫描用的 mock 客户端：源仓下载固定内容，目标分支默认存在。"""
    client_mock = _make_client_mock()
    client_mock.get_file_text.return_value = "content"
    client_mock.list_branches.return_value = [
        {"name": "26.1.0"}, {"name": "master"}, {"name": "9.1.0"},
    ]
    client_mock.get_contents.return_value = None
    return client_mock


class TestResolveToken:
    """Token 规则路由：关键字命中第一条规则，未命中回退默认 Token。"""

    def test_match_by_keyword(self):
        token, keyword = service.resolve_token(
            "Ascend/26.1.0/MindStudio/msprof",
            [{"keyword": "MindStudio", "token": "tokA"}], "tok")
        assert (token, keyword) == ("tokA", "MindStudio")

    def test_match_case_insensitive(self):
        token, _ = service.resolve_token(
            "Ascend/26.1.0/MindStudio/msprof",
            [{"keyword": "mindstudio", "token": "tokA"}], "tok")
        assert token == "tokA"

    def test_first_match_wins(self):
        rules = [
            {"keyword": "MindStudio", "token": "tokA"},
            {"keyword": "msprof", "token": "tokB"},
        ]
        token, keyword = service.resolve_token(
            "Ascend/26.1.0/MindStudio/msprof", rules, "tok")
        assert (token, keyword) == ("tokA", "MindStudio")

    def test_no_match_uses_default(self):
        token, keyword = service.resolve_token(
            "Ascend/26.1.0/CANN/cann",
            [{"keyword": "MindStudio", "token": "tokA"}], "tok")
        assert (token, keyword) == ("tok", None)

    def test_invalid_rules_skipped(self):
        rules = [
            {"keyword": "", "token": "tokA"},
            {"keyword": "MindStudio", "token": ""},
            "not-a-dict",
        ]
        token, keyword = service.resolve_token(
            "Ascend/26.1.0/MindStudio/msprof", rules, "tok")
        assert (token, keyword) == ("tok", None)


class TestLoadTokenRules:
    """从权威仓库读取规则文件：404 → 空规则；JSON 非法 → ConfigError。"""

    def _mock_with_rules(self, rules_text):
        client_mock = _make_client_mock()
        client_mock.get_file_text.return_value = rules_text
        return client_mock

    def test_file_missing_returns_empty(self):
        client_mock = _make_client_mock()
        client_mock.get_file_text.side_effect = GitCodeError(404, "Not Found")
        with patch("translator_ci.service.client", client_mock):
            rules = service.load_token_rules("tok", log=SILENT)
            assert rules == []

    def test_valid_rules_resolved_from_env(self, monkeypatch):
        monkeypatch.setenv("GITCODE_TOKEN_MINDSTUDIO", "tokA")
        text = json.dumps({"rules": [
            {"keyword": "MindStudio", "token_env": "GITCODE_TOKEN_MINDSTUDIO"},
        ]})
        with patch("translator_ci.service.client",
                   self._mock_with_rules(text)):
            rules = service.load_token_rules("tok", log=SILENT)
            assert rules == [{"keyword": "MindStudio", "token": "tokA"}]

    def test_missing_env_rule_skipped(self, monkeypatch):
        monkeypatch.delenv("GITCODE_TOKEN_MINDSTUDIO", raising=False)
        text = json.dumps({"rules": [
            {"keyword": "MindStudio", "token_env": "GITCODE_TOKEN_MINDSTUDIO"},
        ]})
        with patch("translator_ci.service.client",
                   self._mock_with_rules(text)):
            rules = service.load_token_rules("tok", log=SILENT)
            assert rules == []

    def test_incomplete_rule_skipped(self):
        text = json.dumps({"rules": [
            {"keyword": "MindStudio"},
            "not-a-dict",
        ]})
        with patch("translator_ci.service.client",
                   self._mock_with_rules(text)):
            rules = service.load_token_rules("tok", log=SILENT)
            assert rules == []

    def test_invalid_json_raises_config_error(self):
        with patch("translator_ci.service.client",
                   self._mock_with_rules("{not json")):
            with pytest.raises(service.ConfigError):
                service.load_token_rules("tok", log=SILENT)

    def test_missing_rules_key_raises_config_error(self):
        with patch("translator_ci.service.client",
                   self._mock_with_rules(json.dumps({"foo": []}))):
            with pytest.raises(service.ConfigError):
                service.load_token_rules("tok", log=SILENT)


class TestSourceRepoOverride:
    """来源仓库可用 GITCODE_SOURCE_REPO 覆盖为自己的 Fork。"""

    def test_default_is_authoritative(self, monkeypatch):
        monkeypatch.delenv("GITCODE_SOURCE_REPO", raising=False)
        assert service._source_repo() == (
            service.SOURCE_ORG, service.SOURCE_REPO)

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("GITCODE_SOURCE_REPO", "user/Fork-Repo")
        assert service._source_repo() == ("user", "Fork-Repo")

    def test_env_full_url_tolerated(self, monkeypatch):
        """粘贴完整 URL 或带 .git 后缀时也能正确解析。"""
        for value in (
            "https://gitcode.com/user/Fork-Repo",
            "https://gitcode.com/user/Fork-Repo.git",
            "user/Fork-Repo.git",
            "user/Fork-Repo/",
        ):
            monkeypatch.setenv("GITCODE_SOURCE_REPO", value)
            assert service._source_repo() == ("user", "Fork-Repo")


class TestSyncSource:
    """来源为 Fork 时扫描前同步：权威仓库跳过，失败不阻断。"""

    def _client_mock(self):
        client_mock = MagicMock()
        client_mock.GitCodeError = GitCodeError
        return client_mock

    def test_authoritative_source_skips(self, monkeypatch):
        monkeypatch.delenv("GITCODE_SOURCE_REPO", raising=False)
        client_mock = self._client_mock()
        with patch("translator_ci.service.client", client_mock):
            service.sync_source("tok", log=SILENT)
            client_mock.sync_repo.assert_not_called()

    def test_fork_synced_and_polled(self, monkeypatch):
        monkeypatch.setenv("GITCODE_SOURCE_REPO", "user/fork")
        client_mock = self._client_mock()
        client_mock.get_sync_status.return_value = {"repo_sync_result": True}
        with patch("translator_ci.service.client", client_mock):
            service.sync_source("tok", log=SILENT)
            client_mock.sync_repo.assert_called_once_with("user", "fork", "tok")
            client_mock.get_sync_status.assert_called_once()

    def test_sync_failure_non_fatal(self, monkeypatch):
        monkeypatch.setenv("GITCODE_SOURCE_REPO", "user/fork")
        client_mock = self._client_mock()
        client_mock.sync_repo.side_effect = GitCodeError(400, "not a fork")
        with patch("translator_ci.service.client", client_mock):
            service.sync_source("tok", log=SILENT)  # 不应抛异常


class TestScanChanged:
    """扫描全部仓库级目录并与目标仓库对比，筛出有变更的仓库。"""

    def test_missing_token(self):
        result = service.scan_changed("", "", log=SILENT)
        assert result["ok"] is False
        assert result["stage"] == "validate"

    def test_load_index_failure(self):
        client_mock = _make_client_mock()
        client_mock.get_repo.side_effect = GitCodeError(403, "Forbidden")
        with patch("translator_ci.service.client", client_mock):
            result = service.scan_changed("", "tok", log=SILENT)
            assert result["ok"] is False
            assert result["stage"] == "load_index"

    def test_all_new_when_target_file_missing(self):
        """目标仓库无对应文件（404 → None）时，全部记为新增。"""
        client_mock = _make_scan_client_mock()
        with patch("translator_ci.service.client", client_mock):
            result = service.scan_changed("", "tok", log=SILENT)
            assert result["ok"] is True
            assert [c["project"]["path"] for c in result["changed"]] == REPO_PATHS
            for c in result["changed"]:
                assert c["new_count"] == 1
                assert c["update_count"] == 0
            assert result["unchanged"] == []
            assert result["skipped"] == []
            assert result["errors"] == []

    def test_unchanged_when_sha_matches(self):
        """目标文件 sha 与译稿 blob sha 一致时，仓库不进入变更清单。"""
        client_mock = _make_scan_client_mock()
        client_mock.get_contents.return_value = {
            "sha": service._git_blob_sha(b"content"),
        }
        with patch("translator_ci.service.client", client_mock):
            result = service.scan_changed("", "tok", log=SILENT)
            assert result["changed"] == []
            assert result["unchanged"] == REPO_PATHS

    def test_changed_when_sha_differs(self):
        """sha 不一致记为更新，files 只含差异文件。"""
        client_mock = _make_scan_client_mock()
        client_mock.get_contents.return_value = {"sha": "0" * 40}
        with patch("translator_ci.service.client", client_mock):
            result = service.scan_changed("", "tok", log=SILENT)
            assert len(result["changed"]) == 4
            for c in result["changed"]:
                assert c["new_count"] == 0
                assert c["update_count"] == 1

    def test_sha_missing_fallback_content_compare(self):
        """目标文件未返回 sha 时退化为下载全文比对。"""
        client_mock = _make_scan_client_mock()
        client_mock.get_contents.return_value = {}
        # 源仓下载与目标比对共用 get_file_text，按 owner 区分返回内容
        client_mock.get_file_text.side_effect = (
            lambda owner, repo, path, ref, token:
            "content" if owner == service.SOURCE_ORG else "different"
        )
        with patch("translator_ci.service.client", client_mock):
            result = service.scan_changed("", "tok", log=SILENT)
            assert len(result["changed"]) == 4
            assert result["unchanged"] == []

    def test_skipped_when_branch_missing(self):
        """目标仓库不存在版本分支时记入 skipped，不视为扫描失败。"""
        client_mock = _make_scan_client_mock()
        client_mock.list_branches.return_value = [{"name": "other"}]
        with patch("translator_ci.service.client", client_mock):
            result = service.scan_changed("", "tok", log=SILENT)
            assert result["changed"] == []
            assert len(result["skipped"]) == 4
            assert "不存在分支" in result["skipped"][0]["reason"]

    def test_skipped_when_repo_missing(self):
        """目标仓库 404（不存在或无权限）时记入 skipped。"""
        client_mock = _make_scan_client_mock()
        client_mock.list_branches.side_effect = GitCodeError(404, "Not Found")
        with patch("translator_ci.service.client", client_mock):
            result = service.scan_changed("", "tok", log=SILENT)
            assert result["changed"] == []
            assert len(result["skipped"]) == 4

    def test_single_repo_error_does_not_abort(self):
        """单仓对比出错记入 errors，其余仓库照常扫描。"""
        client_mock = _make_scan_client_mock()

        def get_contents(owner, repo, path, ref, token):
            if repo == "mspti":
                raise GitCodeError(500, "server error")
            return None

        client_mock.get_contents.side_effect = get_contents
        with patch("translator_ci.service.client", client_mock):
            result = service.scan_changed("", "tok", log=SILENT)
            assert result["ok"] is True
            assert len(result["changed"]) == 3
            assert len(result["errors"]) == 1
            assert "mspti" in result["errors"][0]["path"]

    def test_keyword_filter(self):
        """关键字过滤时只扫描命中的仓库级目录。"""
        client_mock = _make_scan_client_mock()
        with patch("translator_ci.service.client", client_mock):
            result = service.scan_changed("mspti", "tok", log=SILENT)
            assert [c["project"]["path"] for c in result["changed"]] == [
                "Ascend/26.1.0/MindStudio/mspti",
            ]

    def test_token_rules_whitelist(self):
        """配置规则后仅处理命中的仓库：规则外跳过且不做任何对比请求。"""
        client_mock = _make_scan_client_mock()
        rules = [{"keyword": "mspti", "token": "tokA"}]
        with patch("translator_ci.service.client", client_mock):
            result = service.scan_changed("", "tok", token_rules=rules,
                                          log=SILENT)
            assert [c["repo"] for c in result["changed"]] == ["mspti"]
            assert len(result["skipped"]) == 3
            assert "未命中 Token 规则" in result["skipped"][0]["reason"]
            # 仅 mspti 与目标仓库做过对比，且使用规则 Token
            compare_calls = client_mock.get_contents.call_args_list
            assert {c.args[1] for c in compare_calls} == {"mspti"}
            assert all(c.args[4] == "tokA" for c in compare_calls)
            # 权威仓库下载始终用默认 Token
            source_calls = [
                c for c in client_mock.get_file_text.call_args_list
                if c.args[0] == service.SOURCE_ORG
            ]
            assert source_calls
            assert all(c.args[4] == "tok" for c in source_calls)

    def test_invalid_rules_treated_as_no_rules(self):
        """规则全部无效（关键字/Token 留空）时等同于未配置，全部照常扫描。"""
        client_mock = _make_scan_client_mock()
        rules = [{"keyword": "", "token": "tokA"},
                 {"keyword": "mspti", "token": ""}]
        with patch("translator_ci.service.client", client_mock):
            result = service.scan_changed("", "tok", token_rules=rules,
                                          log=SILENT)
            assert len(result["changed"]) == 4
            assert result["skipped"] == []


# ── 幂等提交测试 ──────────────────────────────────────────────────────


def _make_files():
    """构造一个文件的变更清单（内容为 b"content"）。"""
    return [{
        "target_path": "docs/en/test",
        "content_b64": base64.b64encode(b"content").decode("ascii"),
    }]


def _make_submit_client_mock():
    """构造走通整个 submit_repo 流程的 mock 客户端。

    已有 Fork；目标分支 26.1.0 存在（sha abc123）；Fork 上无固定分支；
    目标路径无同名文件；无已开 PR。
    """
    client_mock = MagicMock()
    client_mock.GitCodeError = GitCodeError
    client_mock.get_user.return_value = {"login": "bot"}
    client_mock.list_repo_forks.return_value = [
        {"full_name": "bot/MindStudio-Profiler", "owner": {"login": "bot"}},
    ]

    def list_branches(owner, repo, token):
        if owner == "Ascend":
            return [{"name": "26.1.0", "commit": {"sha": "abc123"}}]
        return []  # Fork 上的分支

    client_mock.list_branches.side_effect = list_branches
    client_mock.sync_repo.return_value = {}
    client_mock.get_sync_status.return_value = {"repo_sync_result": True}
    client_mock.get_contents.return_value = None
    client_mock.create_file.return_value = {}
    client_mock.list_pull_requests.return_value = []
    client_mock.create_pr.return_value = {"number": 1, "html_url": "http://x/1"}
    return client_mock


class TestSubmitRepo:
    """幂等提交：固定分支复用、相同内容跳过、open PR 去重。"""

    def test_success_creates_fixed_branch_and_pr(self):
        client_mock = _make_submit_client_mock()
        with patch("translator_ci.service.client", client_mock):
            result = service.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is True
            assert result["pr_existed"] is False
            assert result["pr_url"] == "http://x/1"
            # 固定分支名，refs 优先用上游分支的 commit sha
            client_mock.create_branch.assert_called_once_with(
                "bot", "MindStudio-Profiler",
                "translator/auto-26.1.0", "abc123", "tok")
            # 跨仓 PR 带 fork_path，head 为 用户:固定分支
            pr_kwargs = client_mock.create_pr.call_args
            assert pr_kwargs.args[3] == "bot:translator/auto-26.1.0"
            assert pr_kwargs.args[4] == "26.1.0"
            assert pr_kwargs.kwargs["fork_path"] == "bot/MindStudio-Profiler"
            assert "Jenkins" in pr_kwargs.args[5]

    def test_existing_fixed_branch_reused(self):
        """Fork 上已存在固定分支时不再创建。"""
        client_mock = _make_submit_client_mock()

        def list_branches(owner, repo, token):
            if owner == "Ascend":
                return [{"name": "26.1.0", "commit": {"sha": "abc123"}}]
            return [{"name": "translator/auto-26.1.0"}]

        client_mock.list_branches.side_effect = list_branches
        with patch("translator_ci.service.client", client_mock):
            result = service.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is True
            client_mock.create_branch.assert_not_called()

    def test_identical_content_skipped_no_empty_commit(self):
        """分支现有文件内容相同则跳过写入（blob sha 比对）。"""
        client_mock = _make_submit_client_mock()
        client_mock.get_contents.return_value = {
            "sha": service._git_blob_sha(b"content"),
        }
        with patch("translator_ci.service.client", client_mock):
            result = service.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is True
            client_mock.update_file.assert_not_called()
            client_mock.create_file.assert_not_called()
            assert result["unchanged"] == ["docs/en/test"]

    def test_open_pr_dedup(self):
        """已存在同一 head/base 的 open PR 时不重复创建，返回已有 PR。"""
        client_mock = _make_submit_client_mock()
        client_mock.list_pull_requests.return_value = [{
            "number": 42,
            "html_url": "http://x/42",
            "head": {"label": "bot:translator/auto-26.1.0"},
            "base": {"ref": "26.1.0"},
        }]
        with patch("translator_ci.service.client", client_mock):
            result = service.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is True
            assert result["pr_existed"] is True
            assert result["pr_url"] == "http://x/42"
            assert result["pr_number"] == 42
            client_mock.create_pr.assert_not_called()
            # 去重查询带 head/base 过滤参数
            _, kwargs = client_mock.list_pull_requests.call_args
            assert kwargs["head"] == "bot:translator/auto-26.1.0"
            assert kwargs["base"] == "26.1.0"

    def test_pr_other_branch_not_deduped(self):
        """head 分支不同的 open PR 不影响本仓库创建新 PR。"""
        client_mock = _make_submit_client_mock()
        client_mock.list_pull_requests.return_value = [{
            "number": 42,
            "html_url": "http://x/42",
            "head": {"label": "bot:translator/auto-master"},
            "base": {"ref": "master"},
        }]
        with patch("translator_ci.service.client", client_mock):
            result = service.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["pr_existed"] is False
            client_mock.create_pr.assert_called_once()

    def test_identical_content_with_open_pr_is_noop(self):
        """内容未变且已有 open PR：不写文件、不建 PR，纯幂等空转。"""
        client_mock = _make_submit_client_mock()
        client_mock.get_contents.return_value = {
            "sha": service._git_blob_sha(b"content"),
        }
        client_mock.list_pull_requests.return_value = [{
            "number": 42,
            "html_url": "http://x/42",
            "head": {"label": "bot:translator/auto-26.1.0"},
            "base": {"ref": "26.1.0"},
        }]
        with patch("translator_ci.service.client", client_mock):
            result = service.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is True
            assert result["pr_existed"] is True
            client_mock.create_file.assert_not_called()
            client_mock.update_file.assert_not_called()
            client_mock.create_pr.assert_not_called()

    def test_branch_not_exists(self):
        """目标仓库不存在版本分支时失败（不自动创建分支）。"""
        client_mock = _make_submit_client_mock()
        client_mock.list_branches.side_effect = (
            lambda owner, repo, token: [{"name": "master"}]
        )
        with patch("translator_ci.service.client", client_mock):
            result = service.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is False
            assert result["stage"] == "check_branch"

    def test_create_branch_sha_404_falls_back_to_name(self):
        """上游 sha 在 Fork 上 404 时退化为分支名建分支。"""
        client_mock = _make_submit_client_mock()
        client_mock.create_branch.side_effect = [
            GitCodeError(404, "not found"),
            {},
        ]
        with patch("translator_ci.service.client", client_mock):
            result = service.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is True
            calls = client_mock.create_branch.call_args_list
            assert calls[0].args[3] == "abc123"
            assert calls[1].args[3] == "26.1.0"

    def test_all_files_fail_no_pr(self):
        """所有文件写入失败时不创建 PR。"""
        client_mock = _make_submit_client_mock()
        client_mock.get_contents.side_effect = GitCodeError(500, "boom")
        with patch("translator_ci.service.client", client_mock):
            result = service.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is False
            assert result["stage"] == "write"
            client_mock.create_pr.assert_not_called()

    def test_get_user_failure(self):
        client_mock = _make_submit_client_mock()
        client_mock.get_user.side_effect = GitCodeError(401, "Unauthorized")
        with patch("translator_ci.service.client", client_mock):
            result = service.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is False
            assert result["stage"] == "get_user"

    def test_sync_400_falls_back_to_full_sync(self):
        """按分支同步 400（Fork 缺少该分支）时退化为全量同步。"""
        client_mock = _make_submit_client_mock()
        client_mock.sync_repo.side_effect = [
            GitCodeError(400, "26.1.0 branch or tag not found"),
            {"repo_sync_result": False},
        ]
        with patch("translator_ci.service.client", client_mock):
            result = service.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is True
            assert client_mock.sync_repo.call_count == 2
            second = client_mock.sync_repo.call_args_list[1]
            assert second.kwargs == {}


class TestSubmitAll:
    """批量提交：Token 路由 + 白名单 + 单仓失败不中断。"""

    def _item(self, path="Ascend/26.1.0/MindStudio/msprof"):
        return {
            "project": {"path": path},
            "org": "Ascend",
            "repo": "msprof",
            "branch": "26.1.0",
            "files": _make_files(),
        }

    def test_missing_token(self):
        result = service.submit_all([self._item()], "", log=SILENT)
        assert result["ok"] is False
        assert result["stage"] == "validate"

    def test_empty_items(self):
        result = service.submit_all([], "tok", log=SILENT)
        assert result["ok"] is True
        assert result["submitted"] == 0
        assert result["failed"] == 0

    def test_partial_failure_continues(self):
        items = [self._item(), self._item("Ascend/26.1.0/MindStudio/mspti")]
        results = [
            {"ok": False, "stage": "create_pr", "error": "boom"},
            {"ok": True, "pr_url": "http://x/2", "pr_number": 2},
        ]
        with patch.object(service, "submit_repo", side_effect=results) as m:
            result = service.submit_all(items, "tok", log=SILENT)
            assert m.call_count == 2
            assert result["submitted"] == 1
            assert result["failed"] == 1
            first, second = result["results"]
            assert first["ok"] is False
            assert first["stage"] == "create_pr"
            assert second["ok"] is True
            assert second["pr_number"] == 2

    def test_token_rules_route_submit_token(self):
        """配置规则后仅提交命中的仓库：规则外记为失败且不调用 submit_repo。"""
        items = [self._item(), self._item("Ascend/26.1.0/CANN/cann")]
        rules = [{"keyword": "MindStudio", "token": "tokA"}]
        ok_result = {"ok": True, "pr_url": "http://x/1", "pr_number": 1}
        with patch.object(service, "submit_repo", return_value=ok_result) as m:
            result = service.submit_all(items, "tok", token_rules=rules,
                                        log=SILENT)
            assert result["submitted"] == 1
            assert result["failed"] == 1
            # 只有命中规则的仓库真正提交，且使用规则 Token
            assert m.call_count == 1
            assert m.call_args_list[0].args[4] == "tokA"
            skipped = result["results"][1]
            assert skipped["ok"] is False
            assert skipped["stage"] == "token_rule"

    def test_pr_existed_flag_propagated(self):
        """已有 PR 的幂等命中标记透传到批量结果。"""
        items = [self._item()]
        existed = {"ok": True, "pr_existed": True,
                   "pr_url": "http://x/42", "pr_number": 42}
        with patch.object(service, "submit_repo", return_value=existed):
            result = service.submit_all(items, "tok", log=SILENT)
            assert result["results"][0]["pr_existed"] is True
