"""service 模块的单元测试。

覆盖 Token 规则（resolve_token / load_token_rules）、扫描对比
（scan_changed）和幂等提交（submit_repo / submit_all）。
测试通过 mock 各子模块的 client 完全控制下游 GitCode API 依赖，
不发起真实网络请求。
"""

import base64
import json
import os
import shutil
import subprocess
import tempfile
from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, patch

import pytest
import requests

from translator_ci import client, common, index, localgit, rules, scan, submit
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
    "CANN": [_entry("9.1.0", "CANN/9.1.0", "dir"),
             _entry("master", "CANN/master", "dir")],
    "CANN/9.1.0": [_entry("cann", "CANN/9.1.0/cann", "dir")],
    "CANN/9.1.0/cann": [
        _entry("README.md", "CANN/9.1.0/cann/README.md", "file"),
    ],
    "CANN/master": [
        _entry("release-management", "CANN/master/release-management", "dir"),
    ],
    "CANN/master/release-management": [
        _entry("en", "CANN/master/release-management/en", "dir"),
    ],
    "CANN/master/release-management/en": [
        _entry("9.1.1", "CANN/master/release-management/en/9.1.1", "dir"),
    ],
    "CANN/master/release-management/en/9.1.1": [
        _entry("release-notes_en.md",
               "CANN/master/release-management/en/9.1.1/release-notes_en.md",
               "file"),
    ],
}

# 模拟树中的仓库级目录（共 5 个）
REPO_PATHS = [
    "Ascend/26.1.0/MindStudio/msprof",
    "Ascend/26.1.0/MindStudio/mspti",
    "Ascend/master/MindStudio/msprof",
    "CANN/9.1.0/cann",
    "CANN/master/release-management",
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


class TestWhitelist:
    """开放白名单：从权威仓库读取目录路径列表，损坏配置抛 ConfigError。"""

    def _mock_with_whitelist(self, text):
        client_mock = _make_client_mock()
        client_mock.get_file_text.return_value = text
        return client_mock

    def test_file_missing_returns_empty(self):
        """白名单缺失视为不限制开放范围（与黑名单缺失一致，不报错）。"""
        client_mock = _make_client_mock()
        client_mock.get_file_text.side_effect = GitCodeError(404, "Not Found")
        with patch("translator_ci.rules.client", client_mock):
            loaded = rules.load_whitelist("tok", log=SILENT)
            assert loaded == []

    def test_valid_whitelist_loaded(self):
        text = json.dumps({"repos": ["CANN/9.1.0/sip"]})
        with patch("translator_ci.rules.client",
                   self._mock_with_whitelist(text)):
            loaded = rules.load_whitelist("tok", log=SILENT)
            assert loaded == ["CANN/9.1.0/sip"]

    def test_empty_or_blank_entries_skipped(self):
        text = json.dumps({"repos": ["CANN/9.1.0/sip", "", "  "]})
        with patch("translator_ci.rules.client",
                   self._mock_with_whitelist(text)):
            loaded = rules.load_whitelist("tok", log=SILENT)
            assert loaded == ["CANN/9.1.0/sip"]

    def test_invalid_json_raises_config_error(self):
        """配置损坏不能静默放行（否则可能意外放开全部仓库）。"""
        with patch("translator_ci.rules.client",
                   self._mock_with_whitelist("{not json")):
            with pytest.raises(common.ConfigError):
                rules.load_whitelist("tok", log=SILENT)

    def test_missing_repos_key_raises_config_error(self):
        with patch("translator_ci.rules.client",
                   self._mock_with_whitelist(json.dumps({"foo": []}))):
            with pytest.raises(common.ConfigError):
                rules.load_whitelist("tok", log=SILENT)


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
            assert len(result["changed"]) == 5
            for c in result["changed"]:
                assert c["new_count"] == 0
                assert c["update_count"] == 1

    def test_cann_master_release_management_mapping(self):
        """CANN master 目录映射到 release-management 仓库的 master 分支。

        release-management 仓库只有 master 分支（版本号是文件夹而非
        分支）：译稿放在 CANN/master/release-management 下，版本段
        master 即目标分支；en/ 语言目录拍平——目标路径剥离 en/，
        文件名在扩展名前补 _en（已带 _en 则不重复），与目标仓
        <版本>/<文件>_en.md 的中英并存约定对齐。
        """
        client_mock = _make_scan_client_mock()
        with _patch_scan_client(client_mock):
            result = scan.scan_changed("release-management", "tok",
                                       log=SILENT)
            assert [c["project"]["path"] for c in result["changed"]] == [
                "CANN/master/release-management",
            ]
            entry = result["changed"][0]
            assert entry["org"] == "CANN"
            assert entry["repo"] == "release-management"
            assert entry["branch"] == "master"
            assert entry["files"][0]["target_path"] == (
                "9.1.1/release-notes_en.md")
            # 对比目标仓库时使用 master 分支
            compare_calls = client_mock.get_contents.call_args_list
            assert {c.args[3] for c in compare_calls} == {"master"}

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
            assert len(result["changed"]) == 5
            assert result["unchanged"] == []

    def test_skipped_when_branch_missing(self):
        """目标仓库不存在版本分支时记入 skipped，不视为扫描失败。"""
        client_mock = _make_scan_client_mock()
        client_mock.list_branches.return_value = [{"name": "other"}]
        with _patch_scan_client(client_mock):
            result = scan.scan_changed("", "tok", log=SILENT)
            assert result["changed"] == []
            assert len(result["skipped"]) == 5
            assert "不存在分支" in result["skipped"][0]["reason"]

    def test_skipped_when_repo_missing(self):
        """目标仓库 404（不存在或无权限）时记入 skipped。"""
        client_mock = _make_scan_client_mock()
        client_mock.list_branches.side_effect = GitCodeError(404, "Not Found")
        with _patch_scan_client(client_mock):
            result = scan.scan_changed("", "tok", log=SILENT)
            assert result["changed"] == []
            assert len(result["skipped"]) == 5

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
            assert len(result["changed"]) == 4
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
            assert len(result["skipped"]) == 4
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
            assert len(result["changed"]) == 5
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
                "CANN/master/release-management",
            ]
            assert len(result["skipped"]) == 2
            assert all("拉黑名单" in s["reason"] for s in result["skipped"])

    def test_whitelist_restricts_open_scope(self):
        """白名单非空时只处理命中的仓库，名单外直接跳过且无任何对比请求。

        对应「整个功能先只对 sip 仓库开放」：配置白名单后其余仓库即使
        内容有差异也不会被提交。
        """
        client_mock = _make_scan_client_mock()
        with _patch_scan_client(client_mock):
            result = scan.scan_changed(
                "", "tok", whitelist=["CANN/9.1.0/cann"], log=SILENT)
            assert [c["project"]["path"] for c in result["changed"]] == [
                "CANN/9.1.0/cann"]
            assert len(result["skipped"]) == len(REPO_PATHS) - 1
            assert all("不在开放白名单中" in s["reason"]
                       for s in result["skipped"])
            # 名单外的仓库未发起任何目标仓库对比请求
            assert {c.args[1] for c in client_mock.get_contents.call_args_list} \
                == {"cann"}

    def test_empty_whitelist_means_no_restriction(self):
        """白名单为空（未配置）时不限制开放范围，全部仓库照常处理。"""
        client_mock = _make_scan_client_mock()
        with _patch_scan_client(client_mock):
            result = scan.scan_changed("", "tok", whitelist=[], log=SILENT)
            assert len(result["changed"]) == 5
            assert result["skipped"] == []

    def test_whitelist_prefix_matches(self):
        """白名单按前缀命中：开放高层目录则其下所有仓库级目录都放行。"""
        client_mock = _make_scan_client_mock()
        with _patch_scan_client(client_mock):
            result = scan.scan_changed(
                "", "tok",
                whitelist=["Ascend/26.1.0", "CANN/9.1.0/cann"], log=SILENT)
            assert sorted(c["project"]["path"] for c in result["changed"]) == [
                "Ascend/26.1.0/MindStudio/msprof",
                "Ascend/26.1.0/MindStudio/mspti",
                "CANN/9.1.0/cann",
            ]
            assert len(result["skipped"]) == 2

    def test_whitelist_and_blacklist_combined(self):
        """同时配置时：名单外跳过，名单内被拉黑的仍跳过（拉黑优先）。"""
        client_mock = _make_scan_client_mock()
        with _patch_scan_client(client_mock):
            result = scan.scan_changed(
                "", "tok", whitelist=["CANN/9.1.0/cann"],
                blacklist=["CANN/9.1.0/cann"], log=SILENT)
            assert result["changed"] == []
            assert len(result["skipped"]) == len(REPO_PATHS)
            assert any("拉黑名单" in s["reason"] for s in result["skipped"])

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
            assert len(result["changed"]) == 5
            for c in result["changed"]:
                assert c["new_count"] == 0
                assert c["update_count"] == 1
            # 每个仓库各下载 1 个变更文件，且内容带 b64 供提交阶段使用
            assert client_mock.get_file_bytes.call_count == 5
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
            assert len(result["changed"]) == 5
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

    已有 Fork；目标分支 26.1.0 存在（sha abc123）；无已开 PR。
    """
    client_mock = MagicMock()
    client_mock.GitCodeError = GitCodeError
    client_mock.get_user.return_value = {"login": "bot",
                                         "email": "bot@huawei.com"}
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
    client_mock.list_pull_requests.return_value = []
    client_mock.create_pr.return_value = {"number": 1, "html_url": "http://x/1"}
    return client_mock


def _make_localgit_mock(pushed=True, written=None):
    """构造 localgit mock：rebuild_branch 默认推送单 commit 并写入一个文件。"""
    localgit_mock = MagicMock()
    localgit_mock.rebuild_branch.return_value = {
        "pushed": pushed,
        "written": (written if written is not None
                    else [{"path": "docs/en/test", "action": "create"}]),
        "reason": "重建完成" if pushed else "远端已是最新单 commit",
    }
    return localgit_mock


class TestSubmitRepo:
    """幂等提交：单 commit 重建、内容未变跳过、open PR 去重。"""

    def test_binary_gif_flows_through_submit(self):
        """图片/GIF 全流程适配：扫描按字节下载、提交原样传给重建（不解码）。"""
        gif = b"GIF89a\x01\x00\x02\x00\xf7\x00\x80\x00\x00\xff\xff\xff\x00"
        # 扫描：源/目标 sha 不同 → 按字节下载 GIF，content_b64 为原始字节
        scan_client = _make_sha_scan_client_mock(entry_sha="aaa", target_sha="bbb")
        scan_client.get_file_bytes.return_value = gif
        with _patch_scan_client(scan_client):
            scanned = scan.scan_changed("", "tok", log=SILENT)
        assert len(scanned["changed"]) == 5
        gif_entry = scanned["changed"][0]["files"][0]
        assert base64.b64decode(gif_entry["content_b64"]) == gif

        # 提交：files 原样传给 rebuild_branch（base64 字节流不解码不重编码）
        submit_client = _make_submit_client_mock()
        localgit_mock = _make_localgit_mock()
        with patch("translator_ci.submit.client", submit_client), \
                patch("translator_ci.submit.localgit", localgit_mock):
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", [gif_entry], "tok", log=SILENT)
        assert result["ok"] is True
        rb_args = localgit_mock.rebuild_branch.call_args.args
        assert rb_args[5] is gif_entry or \
            base64.b64decode(rb_args[5][0]["content_b64"]) == gif

    def test_success_rebuilds_branch_and_creates_pr(self):
        client_mock = _make_submit_client_mock()
        localgit_mock = _make_localgit_mock()
        with patch("translator_ci.submit.client", client_mock), \
                patch("translator_ci.submit.localgit", localgit_mock):
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is True
            assert result["pr_existed"] is False
            assert result["pr_url"] == "http://x/1"
            # 重建固定分支：以目标分支为基点，files 原样传入
            rb_args = localgit_mock.rebuild_branch.call_args.args
            assert rb_args[0] == "bot"  # Fork owner
            assert rb_args[1] == "MindStudio-Profiler"  # Fork 仓库名
            assert rb_args[3] == "26.1.0"  # 基点 = 目标分支
            assert rb_args[4] == "translator/auto-26.1.0"  # 固定分支
            assert rb_args[5] == _make_files()
            # commit 作者邮箱 = 账号绑定邮箱（CLA 按 commit 作者邮箱校验）
            assert localgit_mock.rebuild_branch.call_args.kwargs.get(
                "author_email") == "bot@huawei.com"
            # 跨仓 PR 带 fork_path，head 为 用户:固定分支
            pr_kwargs = client_mock.create_pr.call_args
            assert pr_kwargs.args[3] == "bot:translator/auto-26.1.0"
            assert pr_kwargs.args[4] == "26.1.0"
            assert pr_kwargs.kwargs["fork_path"] == "bot/MindStudio-Profiler"
            assert "Jenkins" in pr_kwargs.args[5]

    def test_legacy_history_rewrite_reported_as_push(self):
        """存量碎 commit 分支的历史压缩：rebuild 返回 pushed=True、written 空，
        且已有 open PR 时只并入既有 PR（PR diff 自动变为单 commit）。"""
        client_mock = _make_submit_client_mock()
        # 重建只压缩了历史（written 为空）但已强推
        localgit_mock = _make_localgit_mock(pushed=True, written=[])
        client_mock.list_pull_requests.return_value = [{
            "number": 42, "html_url": "http://x/42",
            "head": {"label": "bot:translator/auto-26.1.0"},
            "base": {"ref": "26.1.0"},
        }]
        with patch("translator_ci.submit.client", client_mock), \
                patch("translator_ci.submit.localgit", localgit_mock):
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is True
            assert result["pr_existed"] is True
            client_mock.create_pr.assert_not_called()

    def test_legacy_history_rewrite_without_pr_is_noop(self):
        """历史压缩后无已开 PR：没有可展示的 diff，不创建空 PR。"""
        client_mock = _make_submit_client_mock()
        localgit_mock = _make_localgit_mock(pushed=True, written=[])
        with patch("translator_ci.submit.client", client_mock), \
                patch("translator_ci.submit.localgit", localgit_mock):
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is True
            assert result.get("noop") is True
            client_mock.create_pr.assert_not_called()

    def test_pr_title_body_groups_create_update(self):
        """PR 标题/描述按新增/更新分组。"""
        title, body = submit._make_pr_title_body([
            {"path": "a.md", "action": "create"},
            {"path": "b.md", "action": "update"},
        ])
        assert title == "新增 1 份，更新 1 份 英文版文档"
        assert "## 新增" in body and "## 更新" in body
        assert "`a.md`" in body and "`b.md`" in body

        title, _ = submit._make_pr_title_body(
            [{"path": "b.md", "action": "update"}])
        assert title == "更新 b.md 英文版文档"

    def test_identical_content_skipped_no_push(self):
        """分支内容已是最新（rebuild 未推送）且无 PR：纯幂等空转。"""
        client_mock = _make_submit_client_mock()
        localgit_mock = _make_localgit_mock(pushed=False, written=[])
        with patch("translator_ci.submit.client", client_mock), \
                patch("translator_ci.submit.localgit", localgit_mock):
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is True
            assert result.get("noop") is True
            client_mock.create_pr.assert_not_called()

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
        with patch("translator_ci.submit.client", client_mock), \
                patch("translator_ci.submit.localgit", _make_localgit_mock()):
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
        with patch("translator_ci.submit.client", client_mock), \
                patch("translator_ci.submit.localgit", _make_localgit_mock()):
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["pr_existed"] is False
            client_mock.create_pr.assert_called_once()

    def test_identical_content_with_open_pr_is_noop(self):
        """内容未变（rebuild 未推送）且已有 open PR：不建 PR，纯幂等空转。"""
        client_mock = _make_submit_client_mock()
        client_mock.list_pull_requests.return_value = [{
            "number": 42,
            "html_url": "http://x/42",
            "head": {"label": "bot:translator/auto-26.1.0"},
            "base": {"ref": "26.1.0"},
        }]
        with patch("translator_ci.submit.client", client_mock), \
                patch("translator_ci.submit.localgit",
                      _make_localgit_mock(pushed=False, written=[])):
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is True
            assert result["pr_existed"] is True
            client_mock.create_pr.assert_not_called()

    def test_create_pr_409_treated_as_existing(self):
        """去重漏判时服务端返回 409：从错误消息提取 PR 号按已存在处理。"""
        client_mock = _make_submit_client_mock()
        client_mock.create_pr.side_effect = GitCodeError(
            409, '{"error_code":409,"error_message":"Another open merge '
                 'request already exists for this source branch: !443"}')
        with patch("translator_ci.submit.client", client_mock), \
                patch("translator_ci.submit.localgit", _make_localgit_mock()):
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
        with patch("translator_ci.submit.client", client_mock), \
                patch("translator_ci.submit.localgit", _make_localgit_mock()):
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is False
            assert result["stage"] == "check_branch"

    def test_rebuild_failure_no_pr(self):
        """分支重建失败（如 Fork 缺目标分支/推送失败）时不创建 PR。"""
        client_mock = _make_submit_client_mock()
        localgit_mock = _make_localgit_mock()
        localgit_mock.rebuild_branch.side_effect = GitCodeError(
            200, "git checkout -B 失败: 分支不存在")
        with patch("translator_ci.submit.client", client_mock), \
                patch("translator_ci.submit.localgit", localgit_mock):
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is False
            assert result["stage"] == "write"
            client_mock.create_pr.assert_not_called()

    def test_get_user_failure(self):
        client_mock = _make_submit_client_mock()
        client_mock.get_user.side_effect = GitCodeError(401, "Unauthorized")
        with patch("translator_ci.submit.client", client_mock), \
                patch("translator_ci.submit.localgit", _make_localgit_mock()):
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
        with patch("translator_ci.submit.client", client_mock), \
                patch("translator_ci.submit.localgit", _make_localgit_mock()):
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is True
            assert client_mock.sync_repo.call_count == 2
            second = client_mock.sync_repo.call_args_list[1]
            assert second.kwargs == {}

    def test_full_sync_waits_for_branch_to_appear(self):
        """全量同步是异步的：轮询 Fork 分支列表，等目标分支出现再重建。"""
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
                patch("translator_ci.submit.localgit", _make_localgit_mock()), \
                patch("translator_ci.submit.time.sleep") as sleep_mock:
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is True
            # 等到了分支才继续（睡了 2 次），且没再查同步状态接口
            assert sleep_mock.call_count == 2
            client_mock.get_sync_status.assert_not_called()


class TestRebuildBranch:
    """rebuild_branch 真实 git 离线测试：本地 bare 仓库模拟 Fork，不触网。

    覆盖：首建单 commit、幂等跳过、更新强推、存量碎 commit 强制压缩、
    二进制字节精确提交。
    """

    HEAD = "translator/auto-master"

    def _git(self, *args, cwd=None):
        subprocess.run(["git", *args], cwd=cwd, check=True,
                        capture_output=True)

    @pytest.fixture()
    def fork_bare(self, tmp_path):
        """创建 bare「Fork」：master 分支带一个基础文件。"""
        seed = tmp_path / "seed"
        seed.mkdir()
        self._git("init", "-b", "master", cwd=str(seed))
        (seed / "README.md").write_bytes(b"hello\n")
        self._git("add", "-A", cwd=str(seed))
        self._git("-C", str(seed), "-c", "user.name=t", "-c",
                  "user.email=t@t", "commit", "-m", "base init")
        bare = tmp_path / "fork.git"
        self._git("clone", "--bare", str(seed), str(bare))
        return bare

    def _pr_commit_count(self, bare):
        """PR 范围（master..固定分支）的 commit 数，即 PR 实际包含的 commit 数。"""
        out = subprocess.run(
            ["git", "-C", str(bare), "rev-list", "--count",
             f"master..{self.HEAD}"], capture_output=True, text=True)
        return int(out.stdout.strip())

    def _rebuild(self, bare, spec, author_email="u@huawei.com"):
        """对本地 bare 仓库执行 rebuild_branch，返回 (结果, 克隆目录)。"""
        dest = os.path.join(tempfile.gettempdir(),
                            f"rebuild-test-{id(spec)}-{os.getpid()}")
        with patch.object(localgit, "_auth_url", lambda *a: str(bare)):
            result = localgit.rebuild_branch(
                "u", "r", "tok", "master", self.HEAD,
                [{"target_path": p,
                  "content_b64": base64.b64encode(c).decode()}
                 for p, c in spec.items()],
                dest, log=SILENT, author_email=author_email)
        return result, dest

    def _cleanup(self, dest):
        shutil.rmtree(dest, ignore_errors=True)

    def test_first_rebuild_single_commit(self, fork_bare):
        """首次重建：全部写入合并为单个 commit。"""
        spec = {"a.md": b"A", "d/b.md": b"B"}
        result, dest = self._rebuild(fork_bare, spec)
        try:
            assert result["pushed"] is True
            acts = {w["path"]: w["action"] for w in result["written"]}
            assert acts == {"a.md": "create", "d/b.md": "create"}
            assert self._pr_commit_count(fork_bare) == 1
        finally:
            self._cleanup(dest)

    def test_unchanged_rerun_skips_push(self, fork_bare):
        """重复运行内容不变：不推送（远端已是本工具单 commit）。"""
        spec = {"a.md": b"A"}
        _, dest = self._rebuild(fork_bare, spec)
        self._cleanup(dest)
        result, dest = self._rebuild(fork_bare, spec)
        try:
            assert result["pushed"] is False
            assert self._pr_commit_count(fork_bare) == 1
        finally:
            self._cleanup(dest)

    def test_update_still_single_commit(self, fork_bare):
        """译稿更新：强推后 PR 范围仍单 commit，动作相对目标分支基点。"""
        result, dest = self._rebuild(fork_bare, {"a.md": b"A"})
        self._cleanup(dest)
        result, dest = self._rebuild(fork_bare, {"a.md": b"A2"})
        try:
            assert result["pushed"] is True
            # 基点（master）上没有 a.md → PR 语义为新增
            acts = {w["path"]: w["action"] for w in result["written"]}
            assert acts == {"a.md": "create"}
            assert self._pr_commit_count(fork_bare) == 1
        finally:
            self._cleanup(dest)

    def test_legacy_fragmented_history_compacted(self, fork_bare):
        """存量碎 commit 分支：树相同时仍强推，把 PR 历史压缩为单 commit。"""
        result, dest = self._rebuild(fork_bare, {"a.md": b"A"})
        self._cleanup(dest)
        # 手工在固定分支上追加 5 个碎 commit（模拟旧实现逐文件写入）
        work = tempfile.mkdtemp(prefix="legacy-")
        try:
            self._git("clone", str(fork_bare), work)
            self._git("checkout", self.HEAD, cwd=work)
            for i in range(5):
                with open(os.path.join(work, "a.md"), "ab") as fh:
                    fh.write(b"x")
                self._git("add", "-A", cwd=work)
                self._git("-C", work, "-c", "user.name=t", "-c",
                          "user.email=t@t", "commit",
                          "-m", f"新增 f{i}.md 英文版文档")
            self._git("push", cwd=work)
            assert self._pr_commit_count(fork_bare) == 6
        finally:
            shutil.rmtree(work, ignore_errors=True)
        # 重建回正确内容：树与碎 commit 版不同 → 强推压缩
        result, dest = self._rebuild(fork_bare, {"a.md": b"A"})
        try:
            assert result["pushed"] is True
            assert self._pr_commit_count(fork_bare) == 1
        finally:
            self._cleanup(dest)
        # 再次运行：树相同且已是本工具单 commit → 幂等跳过
        result, dest = self._rebuild(fork_bare, {"a.md": b"A"})
        try:
            assert result["pushed"] is False
        finally:
            self._cleanup(dest)

    def test_legacy_same_tree_still_compacted(self, fork_bare):
        """树与期望一致但历史是旧碎 commit：仍强推压缩（空 commit 重建历史）。"""
        result, dest = self._rebuild(fork_bare, {"a.md": b"A"})
        self._cleanup(dest)
        # 手工追加碎 commit 后把内容改回期望值：树相同、历史多 commit
        work = tempfile.mkdtemp(prefix="legacy2-")
        try:
            self._git("clone", str(fork_bare), work)
            self._git("checkout", self.HEAD, cwd=work)
            for i in range(3):
                with open(os.path.join(work, f"f{i}.md"), "wb") as fh:
                    fh.write(b"x")
                self._git("add", "-A", cwd=work)
                self._git("-C", work, "-c", "user.name=t", "-c",
                          "user.email=t@t", "commit",
                          "-m", f"新增 f{i}.md 英文版文档")
            # 撤销碎文件：树回到与期望一致，但 HEAD 是旧格式 message
            for i in range(3):
                os.remove(os.path.join(work, f"f{i}.md"))
            self._git("add", "-A", cwd=work)
            self._git("-C", work, "-c", "user.name=t", "-c",
                      "user.email=t@t", "commit", "-m", "新增 back.md 英文版文档")
            self._git("push", cwd=work)
            assert self._pr_commit_count(fork_bare) == 5
        finally:
            shutil.rmtree(work, ignore_errors=True)
        # 树与远端相同但历史非本工具单 commit → 仍强推压缩
        #（written 相对目标分支基点，故 a.md 仍计为新增）
        result, dest = self._rebuild(fork_bare, {"a.md": b"A"})
        try:
            assert result["pushed"] is True
            assert self._pr_commit_count(fork_bare) == 1
        finally:
            self._cleanup(dest)

    def test_commit_without_global_identity(self, fork_bare, monkeypatch):
        """无全局 git 身份（如 Jenkins agent）时仍能 commit（仓库级配置兜底），
        作者邮箱优先用账号绑定邮箱（CLA 校验依据），缺省退化为 noreply。"""
        fake_home = tempfile.mkdtemp(prefix="no-identity-")
        monkeypatch.setenv("HOME", fake_home)
        monkeypatch.setenv("GIT_CONFIG_GLOBAL",
                           os.path.join(fake_home, "gitconfig-empty"))
        try:
            # 显式传入账号绑定邮箱
            with patch.object(localgit, "_auth_url", lambda *a: str(fork_bare)):
                result = localgit.rebuild_branch(
                    "u", "r", "tok", "master", self.HEAD,
                    [{"target_path": "a.md",
                      "content_b64": base64.b64encode(b"A").decode()}],
                    os.path.join(tempfile.gettempdir(),
                                 f"rebuild-id-{os.getpid()}"),
                    log=SILENT, author_email="u@huawei.com")
            assert result["pushed"] is True
            assert self._pr_commit_count(fork_bare) == 1
            out = subprocess.run(
                ["git", "-C", str(fork_bare), "log", "-1",
                 "--format=%an %ae", self.HEAD],
                capture_output=True, text=True)
            assert out.stdout.strip() == "u u@huawei.com"
            self._cleanup(os.path.join(tempfile.gettempdir(),
                                       f"rebuild-id-{os.getpid()}"))
            # 未传邮箱：退化为 noreply 地址（换内容强制重建出新 commit）
            result, dest = self._rebuild(fork_bare, {"a.md": b"A2"},
                                         author_email=None)
            self._cleanup(dest)
            out = subprocess.run(
                ["git", "-C", str(fork_bare), "log", "-1",
                 "--format=%ae", self.HEAD],
                capture_output=True, text=True)
            assert out.stdout.strip() == "u@noreply.gitcode.com"
        finally:
            shutil.rmtree(fake_home, ignore_errors=True)

    def test_wrong_author_email_still_rewritten(self, fork_bare):
        """树相同但远端 commit 作者邮箱不对（如旧版 noreply 导致 CLA
        不通过）：即使内容无变化也强推重写，刷新作者邮箱。"""
        spec = {"a.md": b"A"}
        # 先用 noreply 邮箱建远端（模拟修复前的错误状态）
        result, dest = self._rebuild(fork_bare, spec, author_email=None)
        self._cleanup(dest)
        out = subprocess.run(
            ["git", "-C", str(fork_bare), "log", "-1", "--format=%ae",
             self.HEAD], capture_output=True, text=True)
        assert out.stdout.strip() == "u@noreply.gitcode.com"
        # 同内容、传正确邮箱：树相同但作者不对 → 强推重写
        result, dest = self._rebuild(fork_bare, spec,
                                     author_email="u@huawei.com")
        try:
            assert result["pushed"] is True
            assert self._pr_commit_count(fork_bare) == 1
            out = subprocess.run(
                ["git", "-C", str(fork_bare), "log", "-1", "--format=%ae",
                 self.HEAD], capture_output=True, text=True)
            assert out.stdout.strip() == "u@huawei.com"
        finally:
            self._cleanup(dest)
        # 作者已正确后再跑：幂等跳过
        result, dest = self._rebuild(fork_bare, spec,
                                     author_email="u@huawei.com")
        try:
            assert result["pushed"] is False
        finally:
            self._cleanup(dest)

    def test_binary_bytes_exact(self, fork_bare):
        """二进制/CRLF 文件按原始字节提交，不被行尾规范化改写。"""
        gif = b"GIF89a\x01\x00\xf7\r\n"
        result, dest = self._rebuild(fork_bare, {"a.md": b"A", "g.gif": gif})
        try:
            assert result["pushed"] is True
            out = subprocess.run(
                ["git", "-C", str(fork_bare), "show", f"{self.HEAD}:g.gif"],
                capture_output=True)
            assert out.stdout == gif
        finally:
            self._cleanup(dest)


class TestListTree:
    """list_tree 用 contents API 逐层遍历（git trees recursive 有静默截断问题）。"""

    def test_walks_contents_recursively(self):
        """从根目录出发递归收集全部文件路径，目录不入结果。"""
        from translator_ci import client as client_mod

        dirs = {
            "": [
                {"type": "dir", "path": "en"},
                {"type": "file", "path": "README.md"},
            ],
            "en": [
                {"type": "dir", "path": "en/9.1.0"},
                {"type": "file", "path": "en/release-notes.md"},
            ],
            "en/9.1.0": [
                {"type": "file", "path": "en/9.1.0/release-notes.md"},
            ],
        }
        with patch.object(client_mod, "list_dir",
                          side_effect=lambda o, r, p, ref, t: dirs[p]):
            paths = client_mod.list_tree(
                "cann", "release-management", "translator/auto-master", "t")
        assert paths == {
            "README.md", "en/release-notes.md", "en/9.1.0/release-notes.md",
        }

    def test_walk_failure_propagates(self):
        """任一目录查询失败即抛错，由调用方跳过清理（宁可不删，不可误删）。"""
        from translator_ci import client as client_mod

        def list_dir(owner, repo, path, ref, token):
            if path == "en":
                raise GitCodeError(500, "boom")
            return [{"type": "dir", "path": "en"}]

        with patch.object(client_mod, "list_dir", side_effect=list_dir):
            with pytest.raises(GitCodeError):
                client_mod.list_tree("cann", "release-management", "master", "t")


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


def _incremental_mocks(changed_paths, error=None):
    """构造增量模式的 localgit 依赖 mock（fetch_commit + diff_paths）。

    changed_paths: diff_paths 返回的变更路径集合
    error: 非 None 时 diff_paths 抛 GitCodeError（模拟 sha 不可达）
    返回 (patches 列表, fetch_mock, diff_mock)。
    """
    fetch_mock = MagicMock(return_value="before-sha")
    diff_mock = MagicMock(
        side_effect=error) if error else MagicMock(return_value=changed_paths)
    patches = [
        patch("translator_ci.localgit.fetch_commit", fetch_mock),
        patch("translator_ci.localgit.diff_paths", diff_mock),
    ]
    return patches, fetch_mock, diff_mock


class TestTargetRelPath:
    """仓库级目录下相对路径 → 目标仓库内路径的改写规则。"""

    def test_unregistered_repo_passthrough(self):
        """未登记的仓库直连：相对路径原样作为目标路径（sip/hccl）。"""
        assert index._target_rel_path(
            "CANN", "sip", "docs/en/context/infra-faqs.md"
        ) == "docs/en/context/infra-faqs.md"
        assert index._target_rel_path(
            "CANN", "hccl", "docs/en/api_ref/README.md"
        ) == "docs/en/api_ref/README.md"
        assert index._target_rel_path(
            "Ascend", "msprof", "docs/en/a.md") == "docs/en/a.md"

    def test_cann_docs_prepend_docs_prefix(self):
        """CANN/docs 仓内容根为 docs/：目标路径补 docs/ 前缀。"""
        assert index._target_rel_path(
            "CANN", "docs",
            "en/fusion-patterns/Graph_Fusion_Patterns/AABiasaddConvFusion.md"
        ) == "docs/en/fusion-patterns/Graph_Fusion_Patterns/" \
           "AABiasaddConvFusion.md"

    def test_release_management_flattens_en_dir(self):
        """release-management：剥离 en/，扩展名前补 _en（未带时）。"""
        assert index._target_rel_path(
            "CANN", "release-management",
            "en/9.1.0/release-notes.md"
        ) == "9.1.0/release-notes_en.md"

    def test_release_management_no_double_suffix(self):
        """release-management：文件名已带 _en 时不重复追加。"""
        assert index._target_rel_path(
            "CANN", "release-management",
            "en/9.1.1/release-notes_en.md"
        ) == "9.1.1/release-notes_en.md"

    def test_release_management_keeps_multi_dot_name(self):
        """release-management：多级扩展名只在最后一个扩展名前补 _en。"""
        assert index._target_rel_path(
            "CANN", "release-management", "en/9.1.0/a.b.md"
        ) == "9.1.0/a.b_en.md"

    def test_release_management_passthrough_outside_en(self):
        """release-management：不在 en/ 下的文件原样透传（不覆盖中文）。"""
        assert index._target_rel_path(
            "CANN", "release-management", "README.md") == "README.md"


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
        "CANN/master/release-management/en/9.1.1/release-notes_en.md":
            "srcsha1",
        "README.md": "srcsha1",
    }

    def _run(self, client_mock, target_trees, **kwargs):
        patches, clone_mock, cat_blob_mock = _local_mode_mocks(
            self.LOCAL_SOURCE, target_trees)
        extra_patches = kwargs.pop("_extra_patches", [])
        with _patch_scan_client(client_mock), ExitStack() as stack:
            for p in patches + extra_patches:
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
        # 5 个仓库各本地克隆一次；每个变更文件读一次内容
        assert clone_mock.call_count == 5
        assert cat_blob_mock.call_count == 5

    def test_local_unchanged_when_sha_matches(self):
        """本地模式：目标 sha 与来源一致时未变更，不读任何内容。"""
        client_mock = _make_scan_client_mock()
        targets = {
            "Ascend__msprof": {"test": "srcsha1"},
            "Ascend__mspti": {"test": "srcsha1"},
            "CANN__cann": {"README.md": "srcsha1"},
            "CANN__release-management": {
                "9.1.1/release-notes_en.md": "srcsha1"},
        }
        result, clone_mock, cat_blob_mock = self._run(client_mock, targets)
        assert result["changed"] == []
        assert result["unchanged"] == REPO_PATHS
        assert cat_blob_mock.call_count == 0

    def test_single_file_size_limit(self):
        """单文件超 MAX_FILE_SIZE 时该仓库记为失败，不影响其他仓库。"""
        client_mock = _make_scan_client_mock()
        big = b"x" * (common.MAX_FILE_SIZE + 1)
        patches, clone_mock, cat_blob_mock = _local_mode_mocks(
            self.LOCAL_SOURCE, {})
        cat_blob_mock.return_value = big
        with _patch_scan_client(client_mock), ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            result = scan.scan_changed(
                "", "tok", log=SILENT,
                source_repo_dir="/src", clone_root="/clones")
        assert result["ok"] is True
        # 第一个仓库(msprof)单文件超限 → 全部 5 个仓库都失败
        assert result["changed"] == []
        assert len(result["errors"]) == 5
        assert "单文件上限" in result["errors"][0]["error"]

    def test_total_size_limit_accumulates(self):
        """累计大小在同一仓库内跨文件累加，超限报累计上限错误。"""
        client_mock = _make_scan_client_mock()
        # 单仓库 3 个文件各 15MB：单个 < 50MB 不拦，累计 45MB > 40MB 拦截
        source_tree = {
            "CANN/9.1.0/cann/a.md": "sha-a",
            "CANN/9.1.0/cann/b.md": "sha-b",
            "CANN/9.1.0/cann/c.md": "sha-c",
        }
        big = b"x" * (15 * 1024 * 1024)
        patches, clone_mock, cat_blob_mock = _local_mode_mocks(
            source_tree, {})
        cat_blob_mock.return_value = big
        with patch("translator_ci.scan.MAX_TOTAL_SIZE", 40 * 1024 * 1024), \
                _patch_scan_client(client_mock), ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            result = scan.scan_changed(
                "", "tok", log=SILENT,
                source_repo_dir="/src", clone_root="/clones")
        assert result["ok"] is True
        assert result["changed"] == []
        assert len(result["errors"]) == 1
        assert "累计文件大小" in result["errors"][0]["error"]
        assert "上限" in result["errors"][0]["error"]

    def test_local_changed_when_sha_differs(self):
        """本地模式：sha 不一致记为更新，内容走 git cat-file。"""
        client_mock = _make_scan_client_mock()
        targets = {
            "Ascend__msprof": {"test": "tgt-other"},
            "Ascend__mspti": {"test": "tgt-other"},
            "CANN__cann": {"README.md": "tgt-other"},
            "CANN__release-management": {
                "9.1.1/release-notes_en.md": "tgt-other"},
        }
        result, clone_mock, cat_blob_mock = self._run(client_mock, targets)
        assert len(result["changed"]) == 5
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
            "CANN__release-management": {             # 未变更
                "9.1.1/release-notes_en.md": "srcsha1"},
        }
        result, clone_mock, cat_blob_mock = self._run(client_mock, targets)
        assert [c["repo"] for c in result["changed"]] == ["mspti"]
        assert set(result["unchanged"]) == {
            "Ascend/26.1.0/MindStudio/msprof",
            "Ascend/master/MindStudio/msprof",
            "CANN/9.1.0/cann",
            "CANN/master/release-management",
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
        assert len(result["changed"]) == 5
        for c in result["changed"]:
            assert c["new_count"] == 1
        assert client_mock.list_branches.call_count == 5

    def test_local_binary_content_via_cat_blob(self):
        """本地模式读内容走 git cat-file，图片/GIF 原始字节直达提交。"""
        gif = b"GIF89a\x01\x00\x02\x00\xf7\x00\x80\x00\x00\xff\xff\xff\x00"
        client_mock = _make_scan_client_mock()
        targets = {
            "Ascend__msprof": {"test": "tgt-other"},
            "Ascend__mspti": {"test": "tgt-other"},
            "CANN__cann": {"README.md": "tgt-other"},
            "CANN__release-management": {
                "9.1.1/release-notes_en.md": "tgt-other"},
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
        assert len(result["changed"]) == 5
        for c in result["changed"]:
            assert base64.b64decode(c["files"][0]["content_b64"]) == gif


class TestScanChangedIncremental:
    """增量模式：push 的 before/after diff 只处理命中的仓库目录。

    依托本地化模式（LOCAL_SOURCE 同 TestScanChangedLocalMode），
    额外 mock fetch_commit / diff_paths 验证增量过滤行为。
    """

    SOURCE_TREE = TestScanChangedLocalMode.LOCAL_SOURCE

    def _run(self, client_mock, target_trees, changed_paths,
             diff_error=None, **kwargs):
        patches, clone_mock, cat_blob_mock = _local_mode_mocks(
            self.SOURCE_TREE, target_trees)
        inc_patches, fetch_mock, diff_mock = _incremental_mocks(
            changed_paths, error=diff_error)
        with _patch_scan_client(client_mock), ExitStack() as stack:
            for p in patches + inc_patches:
                stack.enter_context(p)
            result = scan.scan_changed(
                "", "tok", log=SILENT, source_repo_dir="/src",
                clone_root="/clones", **kwargs)
        return result, clone_mock, cat_blob_mock, fetch_mock, diff_mock

    def test_incremental_filters_unrelated_repos(self):
        """只有变更路径命中的仓库被处理，其余仓库完全不碰（不克隆）。"""
        client_mock = _make_scan_client_mock()
        result, clone_mock, cat_blob_mock, fetch_mock, diff_mock = self._run(
            client_mock, {},
            changed_paths={"CANN/9.1.0/cann/README.md"},
            diff_before="b-sha", diff_after="a-sha")
        assert result["ok"] is True
        assert result["incremental"] is True
        assert [c["project"]["path"] for c in result["changed"]] == [
            "CANN/9.1.0/cann"]
        # 只有命中的仓库被克隆对比
        assert clone_mock.call_count == 1
        # 其余仓库不属于 changed/unchanged——增量模式下未处理
        assert result["unchanged"] == []

    def test_incremental_root_file_change_skips_all(self):
        """根目录非配置文件变更（如 LICENSE）不触发任何仓库处理。"""
        client_mock = _make_scan_client_mock()
        result, clone_mock, *_ = self._run(
            client_mock, {},
            changed_paths={"LICENSE"},
            diff_before="b-sha", diff_after="a-sha")
        assert result["ok"] is True
        assert result["incremental"] is True
        assert result["changed"] == []
        assert result["unchanged"] == []
        assert clone_mock.call_count == 0

    def test_incremental_config_change_falls_back_to_full(self):
        """变更命中根目录配置文件（blacklist.json 等）回退全量扫描。"""
        client_mock = _make_scan_client_mock()
        result, clone_mock, *_ = self._run(
            client_mock, {},
            changed_paths={"blacklist.json", "CANN/9.1.0/cann/README.md"},
            diff_before="b-sha", diff_after="a-sha")
        assert result["ok"] is True
        assert result["incremental"] is False
        # 全量：5 个仓库全部处理
        assert len(result["changed"]) == 5
        assert clone_mock.call_count == 5

    def test_incremental_whitelist_change_falls_back_to_full(self):
        """变更命中 whitelist.json（范围配置）同样回退全量扫描。"""
        client_mock = _make_scan_client_mock()
        result, clone_mock, *_ = self._run(
            client_mock, {},
            changed_paths={"whitelist.json"},
            diff_before="b-sha", diff_after="a-sha")
        assert result["ok"] is True
        assert result["incremental"] is False
        assert len(result["changed"]) == 5

    def test_incremental_diff_failure_falls_back_to_full(self):
        """diff 失败（before sha 不可达等）回退全量，宁多勿漏。"""
        client_mock = _make_scan_client_mock()
        result, clone_mock, *_ = self._run(
            client_mock, {},
            changed_paths=None,
            diff_error=GitCodeError(200, "bad object b-sha"),
            diff_before="b-sha", diff_after="a-sha")
        assert result["ok"] is True
        assert result["incremental"] is False
        assert len(result["changed"]) == 5

    def test_incremental_requires_local_mode(self):
        """非本地化模式传增量参数：忽略并按全量跑（webhook 兜底）。"""
        client_mock = _make_scan_client_mock()
        with _patch_scan_client(client_mock):
            result = scan.scan_changed(
                "", "tok", log=SILENT,
                diff_before="b-sha", diff_after="a-sha")
        assert result["ok"] is True
        assert result["incremental"] is False

    def test_incremental_directory_move_detected(self):
        """仓库目录整体搬家：新旧路径都命中时两个仓库都处理。"""
        client_mock = _make_scan_client_mock()
        result, clone_mock, *_ = self._run(
            client_mock, {},
            changed_paths={
                "CANN/master/release-management/en/9.1.1/release-notes_en.md",
                "Ascend/master/MindStudio/msprof/test",
            },
            diff_before="b-sha", diff_after="a-sha")
        assert result["ok"] is True
        assert result["incremental"] is True
        assert sorted(c["project"]["path"] for c in result["changed"]) == [
            "Ascend/master/MindStudio/msprof",
            "CANN/master/release-management",
        ]
        assert clone_mock.call_count == 2

    def test_no_incremental_params_is_full(self):
        """不传增量参数：普通全量（含 unchanged 归档）。"""
        client_mock = _make_scan_client_mock()
        targets = {
            "Ascend__msprof": {"test": "srcsha1"},
            "Ascend__mspti": {"test": "srcsha1"},
            "CANN__cann": {"README.md": "srcsha1"},
            "CANN__release-management": {
                "9.1.1/release-notes_en.md": "srcsha1"},
        }
        result, *_ = self._run(
            client_mock, targets, changed_paths=set())
        assert result["ok"] is True
        assert result["incremental"] is False
        assert result["unchanged"] == REPO_PATHS


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


class TestLoadWhitelistLocal:
    """本地化模式：开放白名单从本地克隆读取（git show），失败路径同 API。"""

    def test_reads_from_local_clone(self):
        with patch("translator_ci.localgit.read_file",
                   return_value=b'{"repos": ["CANN/9.1.0/sip"]}') as read_mock:
            loaded = rules.load_whitelist("tok", log=SILENT,
                                          source_repo_dir="/src")
            assert loaded == ["CANN/9.1.0/sip"]
            read_mock.assert_called_once_with("/src", "whitelist.json")

    def test_missing_file_returns_empty(self):
        with patch("translator_ci.localgit.read_file",
                   side_effect=GitCodeError(404, "missing")):
            loaded = rules.load_whitelist("tok", log=SILENT,
                                          source_repo_dir="/src")
            assert loaded == []

    def test_invalid_json_raises_config_error(self):
        with patch("translator_ci.localgit.read_file",
                   return_value=b"{not json"):
            with pytest.raises(common.ConfigError):
                rules.load_whitelist("tok", log=SILENT,
                                     source_repo_dir="/src")


def _ok_response(payload=None):
    """构造一个 200 响应的 MagicMock（_do_request 返回 r.json()）。"""
    resp = MagicMock()
    resp.status_code = 200
    resp.ok = True
    resp.json.return_value = payload or {}
    return resp


class TestNetworkErrorRetry:
    """网络层异常（读超时/断连）在 _do_request 内重试，耗尽后包装为
    GitCodeError（status_code=0），让单仓失败不中断整个批次。"""

    def test_retry_then_success(self):
        ok = _ok_response({"id": 1})
        with patch("translator_ci.client._throttle"), \
                patch("translator_ci.client.time.sleep") as sleep_mock, \
                patch("translator_ci.client.requests.request",
                      side_effect=[requests.exceptions.ReadTimeout("t"), ok]) \
                as req_mock:
            result = client._do_request("POST", "http://api/pulls", "tok")
        assert result == {"id": 1}
        assert req_mock.call_count == 2
        sleep_mock.assert_called_once()

    def test_retry_exhausted_raises_gitcode_error(self):
        with patch("translator_ci.client._throttle"), \
                patch("translator_ci.client.time.sleep"), \
                patch("translator_ci.client.requests.request",
                      side_effect=requests.exceptions.ConnectionError("down")):
            with pytest.raises(GitCodeError) as ei:
                client._do_request("POST", "http://api/pulls", "tok")
        assert "网络错误" in str(ei.value)

    def test_create_pr_uses_slow_timeout(self):
        """创建 PR 属慢操作（服务端逐文件计算大 PR 变更），用 SLOW_TIMEOUT。"""
        ok = _ok_response({"number": 1})
        with patch("translator_ci.client._throttle"), \
                patch("translator_ci.client.requests.request",
                      return_value=ok) as req_mock:
            client.create_pr("cann", "docs", "t", "u:br", "master", "b", "tok",
                             fork_path="u/docs")
        assert req_mock.call_args.kwargs["timeout"] == client.SLOW_TIMEOUT


class TestPaginateBrokenPaging:
    """GitCode 部分接口（PR files）忽略分页参数，每页都返回同一份全量，
    _paginate 必须终止，否则以限流速率无限打请求（#80 卡死一小时）。"""

    @staticmethod
    def _file_entry(i):
        return {"filename": f"docs/en/f{i}.md", "status": "added"}

    def test_full_page_each_time_returns_batch_once(self):
        """返回条数 > per_page：服务端一次性返回全量，直接返回该批。"""
        full = [self._file_entry(i) for i in range(120)]
        with patch("translator_ci.client._throttle"), \
                patch("translator_ci.client.requests.request",
                      side_effect=lambda *a, **k: _ok_response(full)) as req:
            result = client._paginate("http://api/files", "tok", {})
        assert len(result) == 120
        assert req.call_count == 1

    def test_same_first_item_each_page_stops_loop(self):
        """返回条数 == per_page 但 page 参数被忽略（每页内容相同）：
        第二页首条与第一页相同时终止，不重复计入。"""
        same_page = [self._file_entry(i) for i in range(100)]
        with patch("translator_ci.client._throttle"), \
                patch("translator_ci.client.requests.request",
                      side_effect=lambda *a, **k: _ok_response(same_page)) as req:
            result = client._paginate("http://api/files", "tok", {})
        assert len(result) == 100
        assert req.call_count == 2

    def test_normal_multi_page_pagination_still_works(self):
        """常规分页不受影响：逐页取到不足一整页为止。
        （平台单页上限 100，合法分页响应不会超过 per_page。）"""
        pages = [[self._file_entry(i) for i in range(100)],
                 [self._file_entry(i) for i in range(100, 140)]]
        with patch("translator_ci.client._throttle"), \
                patch("translator_ci.client.requests.request",
                      side_effect=[_ok_response(p) for p in pages]) as req:
            result = client._paginate("http://api/branches", "tok", {})
        assert len(result) == 140
        assert req.call_count == 2
