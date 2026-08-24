"""service 模块的单元测试。

覆盖 Token 规则（resolve_token / load_token_rules）、扫描对比
（scan_changed）和幂等提交（submit_repo / submit_all）。
测试通过 mock 各子模块的 client 完全控制下游 GitCode API 依赖，
不发起真实网络请求。
"""

import base64
import json
import os
from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, patch

import pytest

from translator_ci import common, index, rules, scan, submit
from translator_ci.client import GitCodeError

SILENT = lambda msg: None  # 测试中静默日志


@contextmanager
def _patch_scan_client(client_mock):
    """扫描链路同时使用 scan 与 index 两个模块的 client，需一并 mock。

    目录索引（_list_subdirs/_collect_dirs/_walk_files）在 index 模块、
    文件对比在 scan 模块，两个模块的 client 都要替换，避免真实网络请求。
    """
    with patch("translator_ci.scan.client", client_mock), \
            patch("translator_ci.index.client", client_mock):
        yield


def _entry(name, path, entry_type):
    """构造目录条目。"""
    return {"name": name, "path": path, "type": entry_type}


# 权威仓库的模拟目录树（按组织层级：Ascend=组织/版本/产品/仓库，CANN=组织/版本/仓库）
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
    "CANN/9.1.0": [_entry("cann", "CANN/9.1.0/cann", "dir")],
    "CANN/9.1.0/cann": [
        _entry("README.md", "CANN/9.1.0/cann/README.md", "file"),
    ],
}

# 模拟树中的仓库级目录（共 4 个）
REPO_PATHS = [
    "Ascend/26.1.0/MindStudio/msprof",
    "Ascend/26.1.0/MindStudio/mspti",
    "Ascend/master/MindStudio/msprof",
    "CANN/9.1.0/cann",
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
    client_mock.get_file_bytes.return_value = b"content"
    client_mock.list_branches.return_value = [
        {"name": "26.1.0"}, {"name": "master"}, {"name": "9.1.0"},
    ]
    client_mock.get_contents.return_value = None
    return client_mock


def _tree_with_file_shas(sha):
    """返回文件条目带 blob sha 的目录树副本（模拟目录条目携带 sha）。

    sha 取与内容无关的固定值，测试仅关注「来源条目 sha 是否被用于
    免下载比对」，不关心其与 blob sha 的一致性。
    """
    import copy
    tree = copy.deepcopy(FAKE_TREE)
    for entries in tree.values():
        for e in entries:
            if e["type"] == "file":
                e["sha"] = sha
    return tree


def _make_sha_scan_client_mock(entry_sha, target_sha):
    """构造「目录条目带 sha」的扫描 mock：来源 sha 与目标 sha 可分别指定。"""
    client_mock = _make_scan_client_mock()
    client_mock.list_dir.side_effect = (
        lambda owner, repo, path, ref, token: _tree_with_file_shas(entry_sha)[path]
    )
    client_mock.get_contents.return_value = {"sha": target_sha}
    return client_mock


class TestResolveToken:
    """Token 规则路由：关键字命中第一条规则，未命中回退默认 Token。"""

    def test_match_by_keyword(self):
        token, keyword = rules.resolve_token(
            "Ascend/26.1.0/MindStudio/msprof",
            [{"keyword": "MindStudio", "token": "tokA"}], "tok")
        assert (token, keyword) == ("tokA", "MindStudio")

    def test_match_case_insensitive(self):
        token, _ = rules.resolve_token(
            "Ascend/26.1.0/MindStudio/msprof",
            [{"keyword": "mindstudio", "token": "tokA"}], "tok")
        assert token == "tokA"

    def test_first_match_wins(self):
        rule_list = [
            {"keyword": "MindStudio", "token": "tokA"},
            {"keyword": "msprof", "token": "tokB"},
        ]
        token, keyword = rules.resolve_token(
            "Ascend/26.1.0/MindStudio/msprof", rule_list, "tok")
        assert (token, keyword) == ("tokA", "MindStudio")

    def test_no_match_uses_default(self):
        token, keyword = rules.resolve_token(
            "Ascend/26.1.0/CANN/cann",
            [{"keyword": "MindStudio", "token": "tokA"}], "tok")
        assert (token, keyword) == ("tok", None)

    def test_invalid_rules_skipped(self):
        rule_list = [
            {"keyword": "", "token": "tokA"},
            {"keyword": "MindStudio", "token": ""},
            "not-a-dict",
        ]
        token, keyword = rules.resolve_token(
            "Ascend/26.1.0/MindStudio/msprof", rule_list, "tok")
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
        with patch("translator_ci.rules.client", client_mock):
            loaded = rules.load_token_rules("tok", log=SILENT)
            assert loaded == []

    def test_valid_rules_resolved_from_env(self, monkeypatch):
        monkeypatch.setenv("GITCODE_TOKEN_MINDSTUDIO", "tokA")
        text = json.dumps({"rules": [
            {"keyword": "MindStudio", "token_env": "GITCODE_TOKEN_MINDSTUDIO"},
        ]})
        with patch("translator_ci.rules.client",
                   self._mock_with_rules(text)):
            loaded = rules.load_token_rules("tok", log=SILENT)
            assert loaded == [{"keyword": "MindStudio", "token": "tokA"}]

    def test_missing_env_rule_skipped(self, monkeypatch):
        monkeypatch.delenv("GITCODE_TOKEN_MINDSTUDIO", raising=False)
        text = json.dumps({"rules": [
            {"keyword": "MindStudio", "token_env": "GITCODE_TOKEN_MINDSTUDIO"},
        ]})
        with patch("translator_ci.rules.client",
                   self._mock_with_rules(text)):
            loaded = rules.load_token_rules("tok", log=SILENT)
            assert loaded == []

    def test_incomplete_rule_skipped(self):
        text = json.dumps({"rules": [
            {"keyword": "MindStudio"},
            "not-a-dict",
        ]})
        with patch("translator_ci.rules.client",
                   self._mock_with_rules(text)):
            loaded = rules.load_token_rules("tok", log=SILENT)
            assert loaded == []

    def test_invalid_json_raises_config_error(self):
        with patch("translator_ci.rules.client",
                   self._mock_with_rules("{not json")):
            with pytest.raises(common.ConfigError):
                rules.load_token_rules("tok", log=SILENT)

    def test_missing_rules_key_raises_config_error(self):
        with patch("translator_ci.rules.client",
                   self._mock_with_rules(json.dumps({"foo": []}))):
            with pytest.raises(common.ConfigError):
                rules.load_token_rules("tok", log=SILENT)


class TestBlacklist:
    """拉黑名单：从权威仓库读取目录路径列表，损坏配置抛 ConfigError。"""

    def _mock_with_blacklist(self, text):
        client_mock = _make_client_mock()
        client_mock.get_file_text.return_value = text
        return client_mock

    def test_file_missing_returns_empty(self):
        client_mock = _make_client_mock()
        client_mock.get_file_text.side_effect = GitCodeError(404, "Not Found")
        with patch("translator_ci.rules.client", client_mock):
            loaded = rules.load_blacklist("tok", log=SILENT)
            assert loaded == []

    def test_valid_blacklist_loaded(self):
        text = json.dumps({"repos": [
            "Ascend/docs",
            "Ascend/release-management",
        ]})
        with patch("translator_ci.rules.client",
                   self._mock_with_blacklist(text)):
            loaded = rules.load_blacklist("tok", log=SILENT)
            assert loaded == ["Ascend/docs", "Ascend/release-management"]

    def test_empty_or_blank_entries_skipped(self):
        text = json.dumps({"repos": ["Ascend/docs", "", "  "]})
        with patch("translator_ci.rules.client",
                   self._mock_with_blacklist(text)):
            loaded = rules.load_blacklist("tok", log=SILENT)
            assert loaded == ["Ascend/docs"]

    def test_invalid_json_raises_config_error(self):
        with patch("translator_ci.rules.client",
                   self._mock_with_blacklist("{not json")):
            with pytest.raises(common.ConfigError):
                rules.load_blacklist("tok", log=SILENT)

    def test_missing_repos_key_raises_config_error(self):
        with patch("translator_ci.rules.client",
                   self._mock_with_blacklist(json.dumps({"foo": []}))):
            with pytest.raises(common.ConfigError):
                rules.load_blacklist("tok", log=SILENT)


class TestSourceRepoOverride:
    """来源仓库可用 GITCODE_SOURCE_REPO 覆盖为自己的 Fork。"""

    def test_default_is_authoritative(self, monkeypatch):
        monkeypatch.delenv("GITCODE_SOURCE_REPO", raising=False)
        assert common._source_repo() == (
            common.SOURCE_ORG, common.SOURCE_REPO)

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("GITCODE_SOURCE_REPO", "user/Fork-Repo")
        assert common._source_repo() == ("user", "Fork-Repo")

    def test_env_full_url_tolerated(self, monkeypatch):
        """粘贴完整 URL 或带 .git 后缀时也能正确解析。"""
        for value in (
            "https://gitcode.com/user/Fork-Repo",
            "https://gitcode.com/user/Fork-Repo.git",
            "user/Fork-Repo.git",
            "user/Fork-Repo/",
        ):
            monkeypatch.setenv("GITCODE_SOURCE_REPO", value)
            assert common._source_repo() == ("user", "Fork-Repo")


class TestSyncSource:
    """来源为 Fork 时扫描前同步：权威仓库跳过，失败不阻断。"""

    def _client_mock(self):
        client_mock = MagicMock()
        client_mock.GitCodeError = GitCodeError
        return client_mock

    def test_authoritative_source_skips(self, monkeypatch):
        monkeypatch.delenv("GITCODE_SOURCE_REPO", raising=False)
        client_mock = self._client_mock()
        with patch("translator_ci.index.client", client_mock):
            index.sync_source("tok", log=SILENT)
            client_mock.sync_repo.assert_not_called()

    def test_fork_synced_and_polled(self, monkeypatch):
        monkeypatch.setenv("GITCODE_SOURCE_REPO", "user/fork")
        client_mock = self._client_mock()
        client_mock.get_sync_status.return_value = {"repo_sync_result": True}
        with patch("translator_ci.index.client", client_mock):
            index.sync_source("tok", log=SILENT)
            client_mock.sync_repo.assert_called_once_with("user", "fork", "tok")
            client_mock.get_sync_status.assert_called_once()

    def test_sync_failure_non_fatal(self, monkeypatch):
        monkeypatch.setenv("GITCODE_SOURCE_REPO", "user/fork")
        client_mock = self._client_mock()
        client_mock.sync_repo.side_effect = GitCodeError(400, "not a fork")
        with patch("translator_ci.index.client", client_mock):
            index.sync_source("tok", log=SILENT)  # 不应抛异常


class TestScanChanged:
    """扫描全部仓库级目录并与目标仓库对比，筛出有变更的仓库。"""

    def test_missing_token(self):
        result = scan.scan_changed("", "", log=SILENT)
        assert result["ok"] is False
        assert result["stage"] == "validate"

    def test_load_index_failure(self):
        client_mock = _make_client_mock()
        client_mock.get_repo.side_effect = GitCodeError(403, "Forbidden")
        with _patch_scan_client(client_mock):
            result = scan.scan_changed("", "tok", log=SILENT)
            assert result["ok"] is False
            assert result["stage"] == "load_index"

    def test_all_new_when_target_file_missing(self):
        """目标仓库无对应文件（404 → None）时，全部记为新增。"""
        client_mock = _make_scan_client_mock()
        with _patch_scan_client(client_mock):
            result = scan.scan_changed("", "tok", log=SILENT)
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
            "sha": common._git_blob_sha(b"content"),
        }
        with _patch_scan_client(client_mock):
            result = scan.scan_changed("", "tok", log=SILENT)
            assert result["changed"] == []
            assert result["unchanged"] == REPO_PATHS

    def test_changed_when_sha_differs(self):
        """sha 不一致记为更新，files 只含差异文件。"""
        client_mock = _make_scan_client_mock()
        client_mock.get_contents.return_value = {"sha": "0" * 40}
        with _patch_scan_client(client_mock):
            result = scan.scan_changed("", "tok", log=SILENT)
            assert len(result["changed"]) == 4
            for c in result["changed"]:
                assert c["new_count"] == 0
                assert c["update_count"] == 1

    def test_sha_missing_fallback_content_compare(self):
        """目标文件未返回 sha 时退化为下载全文比对。"""
        client_mock = _make_scan_client_mock()
        client_mock.get_contents.return_value = {}
        # 源仓下载与目标比对共用 get_file_bytes，按 owner 区分返回内容
        client_mock.get_file_bytes.side_effect = (
            lambda owner, repo, path, ref, token:
            b"content" if owner == common.SOURCE_ORG else b"different"
        )
        with _patch_scan_client(client_mock):
            result = scan.scan_changed("", "tok", log=SILENT)
            assert len(result["changed"]) == 4
            assert result["unchanged"] == []

    def test_skipped_when_branch_missing(self):
        """目标仓库不存在版本分支时记入 skipped，不视为扫描失败。"""
        client_mock = _make_scan_client_mock()
        client_mock.list_branches.return_value = [{"name": "other"}]
        with _patch_scan_client(client_mock):
            result = scan.scan_changed("", "tok", log=SILENT)
            assert result["changed"] == []
            assert len(result["skipped"]) == 4
            assert "不存在分支" in result["skipped"][0]["reason"]

    def test_skipped_when_repo_missing(self):
        """目标仓库 404（不存在或无权限）时记入 skipped。"""
        client_mock = _make_scan_client_mock()
        client_mock.list_branches.side_effect = GitCodeError(404, "Not Found")
        with _patch_scan_client(client_mock):
            result = scan.scan_changed("", "tok", log=SILENT)
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
        with _patch_scan_client(client_mock):
            result = scan.scan_changed("", "tok", log=SILENT)
            assert result["ok"] is True
            assert len(result["changed"]) == 3
            assert len(result["errors"]) == 1
            assert "mspti" in result["errors"][0]["path"]

    def test_keyword_filter(self):
        """关键字过滤时只扫描命中的仓库级目录。"""
        client_mock = _make_scan_client_mock()
        with _patch_scan_client(client_mock):
            result = scan.scan_changed("mspti", "tok", log=SILENT)
            assert [c["project"]["path"] for c in result["changed"]] == [
                "Ascend/26.1.0/MindStudio/mspti",
            ]

    def test_token_rules_whitelist(self):
        """配置规则后仅处理命中的仓库：规则外跳过且不做任何对比请求。"""
        client_mock = _make_scan_client_mock()
        rules = [{"keyword": "mspti", "token": "tokA"}]
        with _patch_scan_client(client_mock):
            result = scan.scan_changed("", "tok", token_rules=rules,
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
                c for c in client_mock.get_file_bytes.call_args_list
                if c.args[0] == common.SOURCE_ORG
            ]
            assert source_calls
            assert all(c.args[4] == "tok" for c in source_calls)

    def test_invalid_rules_treated_as_no_rules(self):
        """规则全部无效（关键字/Token 留空）时等同于未配置，全部照常扫描。"""
        client_mock = _make_scan_client_mock()
        rules = [{"keyword": "", "token": "tokA"},
                 {"keyword": "mspti", "token": ""}]
        with _patch_scan_client(client_mock):
            result = scan.scan_changed("", "tok", token_rules=rules,
                                          log=SILENT)
            assert len(result["changed"]) == 4
            assert result["skipped"] == []

    def test_blacklist_skips_repo(self):
        """拉黑名单命中（相等或前缀）的仓库直接跳过，不做任何对比请求。"""
        client_mock = _make_scan_client_mock()
        with _patch_scan_client(client_mock):
            result = scan.scan_changed(
                "", "tok", blacklist=["Ascend/26.1.0/MindStudio/mspti"],
                log=SILENT)
            assert [c["project"]["path"] for c in result["changed"]] == [
                p for p in REPO_PATHS
                if p != "Ascend/26.1.0/MindStudio/mspti"
            ]
            assert result["skipped"] == [{
                "path": "Ascend/26.1.0/MindStudio/mspti",
                "reason": "在拉黑名单中，业务侧暂不处理",
            }]
            assert "mspti" not in {
                c.args[1] for c in client_mock.get_contents.call_args_list
            }

    def test_blacklist_prefix_matches(self):
        """黑名单按前缀命中：拉黑高层目录则其下所有仓库级目录都跳过。"""
        client_mock = _make_scan_client_mock()
        with _patch_scan_client(client_mock):
            result = scan.scan_changed(
                "", "tok", blacklist=["Ascend/26.1.0"], log=SILENT)
            assert [c["project"]["path"] for c in result["changed"]] == [
                "Ascend/master/MindStudio/msprof",
                "CANN/9.1.0/cann",
            ]
            assert len(result["skipped"]) == 2
            assert all("拉黑名单" in s["reason"] for s in result["skipped"])

    def test_unchanged_with_source_sha_skips_download(self):
        """来源目录条目 sha 与目标 sha 一致时，不做任何内容下载即判未变更。"""
        client_mock = _make_sha_scan_client_mock(entry_sha="aaa", target_sha="aaa")
        with _patch_scan_client(client_mock):
            result = scan.scan_changed("", "tok", log=SILENT)
            assert result["changed"] == []
            assert result["unchanged"] == REPO_PATHS
            # 核心优化：sha 一致直接判定未变更，来源文件一次都没下载
            assert client_mock.get_file_bytes.call_count == 0

    def test_changed_with_source_sha_differs_downloads(self):
        """来源目录条目 sha 与目标 sha 不一致时，按需下载内容并记为更新。"""
        client_mock = _make_sha_scan_client_mock(entry_sha="aaa", target_sha="bbb")
        with _patch_scan_client(client_mock):
            result = scan.scan_changed("", "tok", log=SILENT)
            assert len(result["changed"]) == 4
            for c in result["changed"]:
                assert c["new_count"] == 0
                assert c["update_count"] == 1
            # 每个仓库各下载 1 个变更文件，且内容带 b64 供提交阶段使用
            assert client_mock.get_file_bytes.call_count == 4
            assert all(f["content_b64"] for c in result["changed"]
                       for f in c["files"])

    def test_binary_file_downloaded_as_bytes(self):
        """译稿含二进制文件（如 GIF 图片）时按字节下载，不做 UTF-8 解码。

        旧实现经 get_file_text 按文本解码，遇图片直接 UnicodeDecodeError
        导致整轮扫描崩溃（对应 CI 中 docs/en/figures/*.gif 的失败）。
        """
        gif = b"GIF89a\x01\x00\x02\x00\xf7\x00\x80\x00\x00\xff\xff\xff\x00"
        client_mock = _make_sha_scan_client_mock(entry_sha="aaa", target_sha="bbb")
        client_mock.get_file_bytes.return_value = gif
        with _patch_scan_client(client_mock):
            result = scan.scan_changed("", "tok", log=SILENT)
            assert len(result["changed"]) == 4
            for c in result["changed"]:
                assert c["update_count"] == 1
                assert base64.b64decode(c["files"][0]["content_b64"]) == gif

    def test_invalid_path_rejected_before_download(self):
        """含 .. 的非法路径在列文件阶段即被拒绝，不触发任何内容下载。"""
        import copy
        tree = copy.deepcopy(FAKE_TREE)
        tree["Ascend/26.1.0/MindStudio/msprof"].append(
            _entry("../evil", "Ascend/26.1.0/MindStudio/msprof/../evil", "file"))
        client_mock = _make_scan_client_mock()
        client_mock.list_dir.side_effect = (
            lambda owner, repo, path, ref, token: tree[path]
        )
        project = index._to_project("Ascend/26.1.0/MindStudio/msprof")
        with _patch_scan_client(client_mock):
            result = scan._fetch_repo_files(project, "master", "tok", SILENT)
            assert result["ok"] is False
            assert result["stage"] == "list_files"
            assert "非法的文件路径" in result["error"]
            # 校验前置：非法路径在下载前拦截，未下载任何文件
            assert client_mock.get_file_bytes.call_count == 0


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

    def test_binary_gif_flows_through_submit(self):
        """图片/GIF 全流程适配：扫描按字节下载、提交按 base64 写入不解码。"""
        gif = b"GIF89a\x01\x00\x02\x00\xf7\x00\x80\x00\x00\xff\xff\xff\x00"
        # 扫描：源/目标 sha 不同 → 按字节下载 GIF，content_b64 为原始字节
        scan_client = _make_sha_scan_client_mock(entry_sha="aaa", target_sha="bbb")
        scan_client.get_file_bytes.return_value = gif
        with _patch_scan_client(scan_client):
            scanned = scan.scan_changed("", "tok", log=SILENT)
        assert len(scanned["changed"]) == 4
        gif_entry = scanned["changed"][0]["files"][0]
        assert base64.b64decode(gif_entry["content_b64"]) == gif

        # 提交：目标已有同名文件 → 走 update_file，写回同样的 GIF 字节
        submit_client = _make_submit_client_mock()
        submit_client.get_contents.return_value = {"sha": "targetsha"}
        with patch("translator_ci.submit.client", submit_client):
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", [gif_entry], "tok", log=SILENT)
        assert result["ok"] is True
        assert submit_client.create_file.call_count == 0
        update_kwargs = submit_client.update_file.call_args
        assert base64.b64decode(update_kwargs.args[4]) == gif
        assert update_kwargs.args[5] == "targetsha"

    def test_success_creates_fixed_branch_and_pr(self):
        client_mock = _make_submit_client_mock()
        with patch("translator_ci.submit.client", client_mock):
            result = submit.submit_repo(
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
        with patch("translator_ci.submit.client", client_mock):
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is True
            client_mock.create_branch.assert_not_called()

    def test_identical_content_skipped_no_empty_commit(self):
        """分支现有文件内容相同则跳过写入（blob sha 比对）。"""
        client_mock = _make_submit_client_mock()
        client_mock.get_contents.return_value = {
            "sha": common._git_blob_sha(b"content"),
        }
        with patch("translator_ci.submit.client", client_mock):
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is True
            client_mock.update_file.assert_not_called()
            client_mock.create_file.assert_not_called()
            assert result["unchanged"] == ["docs/en/test"]

    def test_open_pr_dedup(self):
        """已存在同一 head/base 的 open PR 时不重复创建，返回已有 PR。"""
        client_mock = _make_submit_client_mock()
        # 真实 API 的 head.label 只有分支名，不含用户前缀
        client_mock.list_pull_requests.return_value = [{
            "number": 42,
            "html_url": "http://x/42",
            "head": {"label": "translator/auto-26.1.0",
                     "ref": "translator/auto-26.1.0"},
            "base": {"ref": "26.1.0"},
        }]
        with patch("translator_ci.submit.client", client_mock):
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is True
            assert result["pr_existed"] is True
            assert result["pr_url"] == "http://x/42"
            assert result["pr_number"] == 42
            client_mock.create_pr.assert_not_called()
            # 服务端不支持 head/base 过滤，拉全量 open PR 后客户端过滤
            _, kwargs = client_mock.list_pull_requests.call_args
            assert "head" not in kwargs
            assert "base" not in kwargs

    def test_pr_other_branch_not_deduped(self):
        """head 分支不同的 open PR 不影响本仓库创建新 PR。"""
        client_mock = _make_submit_client_mock()
        client_mock.list_pull_requests.return_value = [{
            "number": 42,
            "html_url": "http://x/42",
            "head": {"label": "bot:translator/auto-master"},
            "base": {"ref": "master"},
        }]
        with patch("translator_ci.submit.client", client_mock):
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["pr_existed"] is False
            client_mock.create_pr.assert_called_once()

    def test_identical_content_with_open_pr_is_noop(self):
        """内容未变且已有 open PR：不写文件、不建 PR，纯幂等空转。"""
        client_mock = _make_submit_client_mock()
        client_mock.get_contents.return_value = {
            "sha": common._git_blob_sha(b"content"),
        }
        client_mock.list_pull_requests.return_value = [{
            "number": 42,
            "html_url": "http://x/42",
            "head": {"label": "bot:translator/auto-26.1.0"},
            "base": {"ref": "26.1.0"},
        }]
        with patch("translator_ci.submit.client", client_mock):
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is True
            assert result["pr_existed"] is True
            client_mock.create_file.assert_not_called()
            client_mock.update_file.assert_not_called()
            client_mock.create_pr.assert_not_called()

    def test_create_pr_409_treated_as_existing(self):
        """去重漏判时服务端返回 409：从错误消息提取 PR 号按已存在处理。"""
        client_mock = _make_submit_client_mock()
        client_mock.create_pr.side_effect = GitCodeError(
            409, '{"error_code":409,"error_message":"Another open merge '
                 'request already exists for this source branch: !443"}')
        with patch("translator_ci.submit.client", client_mock):
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is True
            assert result["pr_existed"] is True
            assert result["pr_number"] == 443
            assert result["pr_url"].endswith("/merge_requests/443")

    def test_branch_not_exists(self):
        """目标仓库不存在版本分支时失败（不自动创建分支）。"""
        client_mock = _make_submit_client_mock()
        client_mock.list_branches.side_effect = (
            lambda owner, repo, token: [{"name": "master"}]
        )
        with patch("translator_ci.submit.client", client_mock):
            result = submit.submit_repo(
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
        with patch("translator_ci.submit.client", client_mock):
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is True
            calls = client_mock.create_branch.call_args_list
            assert calls[0].args[3] == "abc123"
            assert calls[1].args[3] == "26.1.0"

    def test_all_files_fail_no_pr(self):
        """所有文件写入失败时不创建 PR。"""
        client_mock = _make_submit_client_mock()
        client_mock.get_contents.side_effect = GitCodeError(500, "boom")
        with patch("translator_ci.submit.client", client_mock):
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is False
            assert result["stage"] == "write"
            client_mock.create_pr.assert_not_called()

    def test_get_user_failure(self):
        client_mock = _make_submit_client_mock()
        client_mock.get_user.side_effect = GitCodeError(401, "Unauthorized")
        with patch("translator_ci.submit.client", client_mock):
            result = submit.submit_repo(
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
        # 全量同步后目标分支立即出现在 Fork 上（免于轮询等待）
        client_mock.list_branches.side_effect = lambda owner, repo, token: [
            {"name": "26.1.0", "commit": {"sha": "abc123"}}]
        with patch("translator_ci.submit.client", client_mock):
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is True
            assert client_mock.sync_repo.call_count == 2
            second = client_mock.sync_repo.call_args_list[1]
            assert second.kwargs == {}

    def test_full_sync_waits_for_branch_to_appear(self):
        """全量同步是异步的：轮询 Fork 分支列表，等目标分支出现再建分支。"""
        client_mock = _make_submit_client_mock()
        client_mock.sync_repo.side_effect = [
            GitCodeError(400, "26.1.0 branch or tag not found"),
            {},
        ]
        calls = {"n": 0}

        def list_branches(owner, repo, token):
            if owner == "Ascend":
                return [{"name": "26.1.0", "commit": {"sha": "abc123"}}]
            # Fork：前两次轮询分支尚未同步过来，第三次出现
            calls["n"] += 1
            return [{"name": "26.1.0"}] if calls["n"] >= 3 else []

        client_mock.list_branches.side_effect = list_branches
        with patch("translator_ci.submit.client", client_mock), \
                patch("translator_ci.submit.time.sleep") as sleep_mock:
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is True
            # 等到了分支才继续（睡了 2 次），且没再查同步状态接口
            assert sleep_mock.call_count == 2
            client_mock.get_sync_status.assert_not_called()


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
        result = submit.submit_all([self._item()], "", log=SILENT)
        assert result["ok"] is False
        assert result["stage"] == "validate"

    def test_empty_items(self):
        result = submit.submit_all([], "tok", log=SILENT)
        assert result["ok"] is True
        assert result["submitted"] == 0
        assert result["failed"] == 0

    def test_partial_failure_continues(self):
        items = [self._item(), self._item("Ascend/26.1.0/MindStudio/mspti")]
        results = [
            {"ok": False, "stage": "create_pr", "error": "boom"},
            {"ok": True, "pr_url": "http://x/2", "pr_number": 2},
        ]
        with patch.object(submit, "submit_repo", side_effect=results) as m:
            result = submit.submit_all(items, "tok", log=SILENT)
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
        with patch.object(submit, "submit_repo", return_value=ok_result) as m:
            result = submit.submit_all(items, "tok", token_rules=rules,
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
        with patch.object(submit, "submit_repo", return_value=existed):
            result = submit.submit_all(items, "tok", log=SILENT)
            assert result["results"][0]["pr_existed"] is True


# ── 本地化模式测试 ────────────────────────────────────────────────────


def _local_mode_mocks(source_tree, target_trees, source_dir="/src"):
    """构造本地化模式的 localgit 依赖 mock。

    source_tree:  权威仓库的本地文件映射 {path: sha}
    target_trees: 按 "{org}__{repo}" 键映射目标仓库的文件映射 {path: sha}
    （本地模式目标仓库克隆到 {clone_root}/{org}__{repo}）
    返回 (patches 列表, clone_mock, cat_blob_mock)。
    """
    clone_mock = MagicMock(return_value="/clones/dummy")
    cat_blob_mock = MagicMock(return_value=b"content")

    def tree_map(repo_dir):
        if repo_dir == source_dir:
            return source_tree
        return target_trees.get(os.path.basename(repo_dir), {})

    patches = [
        patch("translator_ci.localgit.clone", clone_mock),
        patch("translator_ci.localgit.tree_map", side_effect=tree_map),
        patch("translator_ci.localgit.cat_blob", cat_blob_mock),
    ]
    return patches, clone_mock, cat_blob_mock


class TestIndexLocalTree:
    """从本地 git 文件映射提取仓库级目录与文件。"""

    def test_repo_dirs_from_tree(self):
        tree = {
            "Ascend/26.1.0/MindStudio/msprof/test": "s",
            "Ascend/26.1.0/MindStudio/mspti/test": "s",
            "README.md": "s",
        }
        assert index._repo_dirs_from_tree(tree) == [
            "Ascend/26.1.0/MindStudio/msprof",
            "Ascend/26.1.0/MindStudio/mspti",
        ]

    def test_repo_files_from_tree(self):
        tree = {
            "Ascend/26.1.0/MindStudio/msprof/docs/en/a.md": "sha1",
            "Ascend/26.1.0/MindStudio/msprof/docs/en/b.md": "sha2",
            "Ascend/26.1.0/MindStudio/mspti/test": "sha3",
        }
        files = index._repo_files_from_tree(
            tree, "Ascend/26.1.0/MindStudio/msprof")
        assert files == [
            {"path": "docs/en/a.md", "sha": "sha1"},
            {"path": "docs/en/b.md", "sha": "sha2"},
        ]

    def test_repo_files_invalid_path_rejected(self):
        tree = {
            "Ascend/26.1.0/MindStudio/msprof/../../evil": "s",
        }
        with pytest.raises(GitCodeError) as exc_info:
            index._repo_files_from_tree(
                tree, "Ascend/26.1.0/MindStudio/msprof")
        assert "非法的文件路径" in str(exc_info.value)


class TestScanChangedLocalMode:
    """本地化模式：索引/列文件/对比全走本地 git，扫描阶段不发 API。

    注入 source_repo_dir + clone_root 后，目录索引与内容读取由
    localgit 完成；目标仓库本地克隆失败时回退 API 对比。
    """

    LOCAL_SOURCE = {
        "Ascend/26.1.0/MindStudio/msprof/test": "srcsha1",
        "Ascend/26.1.0/MindStudio/mspti/test": "srcsha1",
        "Ascend/master/MindStudio/msprof/test": "srcsha1",
        "CANN/9.1.0/cann/README.md": "srcsha1",
        "README.md": "srcsha1",
    }

    def _run(self, client_mock, target_trees, **kwargs):
        patches, clone_mock, cat_blob_mock = _local_mode_mocks(
            self.LOCAL_SOURCE, target_trees)
        with _patch_scan_client(client_mock), ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            result = scan.scan_changed(
                "", "tok", log=SILENT,
                source_repo_dir="/src", clone_root="/clones", **kwargs)
        return result, clone_mock, cat_blob_mock

    def test_local_all_new_when_target_missing(self):
        """目标仓库本地映射为空时全部记为新增，扫描零 API 调用。"""
        client_mock = _make_scan_client_mock()
        result, clone_mock, cat_blob_mock = self._run(client_mock, {})
        assert result["ok"] is True
        assert [c["project"]["path"] for c in result["changed"]] == REPO_PATHS
        for c in result["changed"]:
            assert c["new_count"] == 1
            assert c["update_count"] == 0
        # 索引/对比全部本地化：不调用来源仓库或目标的 API
        client_mock.get_repo.assert_not_called()
        client_mock.list_dir.assert_not_called()
        client_mock.get_file_bytes.assert_not_called()
        # 4 个仓库各本地克隆一次；每个变更文件读一次内容
        assert clone_mock.call_count == 4
        assert cat_blob_mock.call_count == 4

    def test_local_unchanged_when_sha_matches(self):
        """本地模式：目标 sha 与来源一致时未变更，不读任何内容。"""
        client_mock = _make_scan_client_mock()
        targets = {
            "Ascend__msprof": {"test": "srcsha1"},
            "Ascend__mspti": {"test": "srcsha1"},
            "CANN__cann": {"README.md": "srcsha1"},
        }
        result, clone_mock, cat_blob_mock = self._run(client_mock, targets)
        assert result["changed"] == []
        assert result["unchanged"] == REPO_PATHS
        assert cat_blob_mock.call_count == 0

    def test_local_changed_when_sha_differs(self):
        """本地模式：sha 不一致记为更新，内容走 git cat-file。"""
        client_mock = _make_scan_client_mock()
        targets = {
            "Ascend__msprof": {"test": "tgt-other"},
            "Ascend__mspti": {"test": "tgt-other"},
            "CANN__cann": {"README.md": "tgt-other"},
        }
        result, clone_mock, cat_blob_mock = self._run(client_mock, targets)
        assert len(result["changed"]) == 4
        for c in result["changed"]:
            assert c["new_count"] == 0
            assert c["update_count"] == 1
            assert c["files"][0]["content_b64"] == base64.b64encode(b"content").decode("ascii")

    def test_local_partial_change(self):
        """本地模式：部分仓库变化时只处理差异项。"""
        client_mock = _make_scan_client_mock()
        targets = {
            "Ascend__msprof": {"test": "srcsha1"},   # 未变更
            "Ascend__mspti": {"test": "tgt-other"},  # 变更
            "CANN__cann": {"README.md": "srcsha1"},  # 未变更
        }
        result, clone_mock, cat_blob_mock = self._run(client_mock, targets)
        assert [c["repo"] for c in result["changed"]] == ["mspti"]
        assert set(result["unchanged"]) == {
            "Ascend/26.1.0/MindStudio/msprof",
            "Ascend/master/MindStudio/msprof",
            "CANN/9.1.0/cann",
        }
        assert cat_blob_mock.call_count == 1

    def test_local_clone_failure_falls_back_to_api(self):
        """目标仓库本地克隆失败时回退 API 对比，不中断整轮扫描。"""
        client_mock = _make_scan_client_mock()
        patches, clone_mock, cat_blob_mock = _local_mode_mocks(
            self.LOCAL_SOURCE, {})
        clone_mock.side_effect = GitCodeError(404, "Repository not found")
        with _patch_scan_client(client_mock), ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            result = scan.scan_changed(
                "", "tok", log=SILENT,
                source_repo_dir="/src", clone_root="/clones")
        # 回退 API：list_branches 正常返回、目标无文件 → 全部记为新增
        assert result["ok"] is True
        assert len(result["changed"]) == 4
        for c in result["changed"]:
            assert c["new_count"] == 1
        assert client_mock.list_branches.call_count == 4

    def test_local_binary_content_via_cat_blob(self):
        """本地模式读内容走 git cat-file，图片/GIF 原始字节直达提交。"""
        gif = b"GIF89a\x01\x00\x02\x00\xf7\x00\x80\x00\x00\xff\xff\xff\x00"
        client_mock = _make_scan_client_mock()
        targets = {
            "Ascend__msprof": {"test": "tgt-other"},
            "Ascend__mspti": {"test": "tgt-other"},
            "CANN__cann": {"README.md": "tgt-other"},
        }
        patches, clone_mock, cat_blob_mock = _local_mode_mocks(
            self.LOCAL_SOURCE, targets)
        cat_blob_mock.return_value = gif
        with _patch_scan_client(client_mock), ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            result = scan.scan_changed(
                "", "tok", log=SILENT,
                source_repo_dir="/src", clone_root="/clones")
        assert len(result["changed"]) == 4
        for c in result["changed"]:
            assert base64.b64decode(c["files"][0]["content_b64"]) == gif


class TestLoadTokenRulesLocal:
    """本地化模式：规则文件从本地克隆读取（git show），失败路径同 API。"""

    def test_reads_from_local_clone(self):
        with patch("translator_ci.localgit.read_file",
                   return_value=b'{"rules": []}') as read_mock:
            loaded = rules.load_token_rules("tok", log=SILENT,
                                            source_repo_dir="/src")
            assert loaded == []
            read_mock.assert_called_once_with("/src", "token_rules.json")

    def test_missing_file_returns_empty(self):
        with patch("translator_ci.localgit.read_file",
                   side_effect=GitCodeError(404, "missing")):
            loaded = rules.load_token_rules("tok", log=SILENT,
                                            source_repo_dir="/src")
            assert loaded == []

    def test_valid_rules_resolved_from_env(self, monkeypatch):
        monkeypatch.setenv("GITCODE_TOKEN_MINDSTUDIO", "tokA")
        text = json.dumps({"rules": [
            {"keyword": "MindStudio", "token_env": "GITCODE_TOKEN_MINDSTUDIO"},
        ]})
        with patch("translator_ci.localgit.read_file",
                   return_value=text.encode("utf-8")):
            loaded = rules.load_token_rules("tok", log=SILENT,
                                            source_repo_dir="/src")
            assert loaded == [{"keyword": "MindStudio", "token": "tokA"}]

    def test_invalid_json_raises_config_error(self):
        with patch("translator_ci.localgit.read_file",
                   return_value=b"{not json"):
            with pytest.raises(common.ConfigError):
                rules.load_token_rules("tok", log=SILENT,
                                       source_repo_dir="/src")


class TestLoadBlacklistLocal:
    """本地化模式：拉黑名单从本地克隆读取（git show），失败路径同 API。"""

    def test_reads_from_local_clone(self):
        with patch("translator_ci.localgit.read_file",
                   return_value=b'{"repos": ["Ascend/docs"]}') as read_mock:
            loaded = rules.load_blacklist("tok", log=SILENT,
                                          source_repo_dir="/src")
            assert loaded == ["Ascend/docs"]
            read_mock.assert_called_once_with("/src", "blacklist.json")

    def test_missing_file_returns_empty(self):
        with patch("translator_ci.localgit.read_file",
                   side_effect=GitCodeError(404, "missing")):
            loaded = rules.load_blacklist("tok", log=SILENT,
                                          source_repo_dir="/src")
            assert loaded == []

    def test_invalid_json_raises_config_error(self):
        with patch("translator_ci.localgit.read_file",
                   return_value=b"{not json"):
            with pytest.raises(common.ConfigError):
                rules.load_blacklist("tok", log=SILENT,
                                     source_repo_dir="/src")
