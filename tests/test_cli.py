"""cli 模块的单元测试。

覆盖命令行入口的流程编排与退出码约定：
0 成功（含无变更）、1 运行失败、2 部分仓库提交失败。
测试通过 mock service 层控制行为，不发起真实网络请求。
"""

from unittest.mock import patch

import pytest

from translator_ci import cli, service
from translator_ci.client import GitCodeError


@pytest.fixture(autouse=True)
def _default_token(monkeypatch):
    """默认注入 GITCODE_TOKEN；不需要的用例可自行删除。"""
    monkeypatch.setenv("GITCODE_TOKEN", "tok")


def _scan_result(changed=None):
    return {
        "ok": True,
        "changed": changed or [],
        "unchanged": ["Ascend/26.1.0/MindStudio/other"],
        "skipped": [],
        "errors": [],
    }


def _changed_item():
    return {
        "project": {"path": "Ascend/26.1.0/MindStudio/msprof"},
        "org": "Ascend",
        "repo": "msprof",
        "branch": "26.1.0",
        "files": [{"target_path": "a.md", "content_b64": "YQ=="}],
        "new_count": 1,
        "update_count": 0,
    }


class TestTokenValidation:
    """默认 Token 缺失直接失败，不发起任何请求。"""

    def test_missing_default_token(self, monkeypatch):
        monkeypatch.delenv("GITCODE_TOKEN")
        with patch.object(cli.service, "load_token_rules") as load_mock:
            assert cli.main([]) == 1
            load_mock.assert_not_called()


class TestLoadRules:
    """规则加载阶段的失败路径。"""

    def test_config_error_returns_1(self):
        with patch.object(cli.service, "load_token_rules",
                          side_effect=service.ConfigError("bad json")):
            assert cli.main([]) == 1

    def test_gitcode_error_returns_1(self):
        with patch.object(cli.service, "load_token_rules",
                          side_effect=GitCodeError(403, "Forbidden")):
            assert cli.main([]) == 1

    def test_rules_path_passed_through(self):
        with patch.object(cli.service, "load_token_rules",
                          return_value=[]) as load_mock, \
                patch.object(cli.service, "load_blacklist", return_value=[]), \
                patch.object(cli.service, "scan_changed",
                             return_value=_scan_result()):
            assert cli.main([]) == 0
            _, kwargs = load_mock.call_args
            assert kwargs["rules_path"] == service.DEFAULT_RULES_PATH

    def test_blacklist_path_passed_through(self):
        with patch.object(cli.service, "load_token_rules",
                          return_value=[]), \
                patch.object(cli.service, "load_blacklist",
                             return_value=[]) as load_mock, \
                patch.object(cli.service, "scan_changed",
                             return_value=_scan_result()):
            assert cli.main([]) == 0
            _, kwargs = load_mock.call_args
            assert kwargs["blacklist_path"] == service.DEFAULT_BLACKLIST_PATH


class TestScanPhase:
    """扫描阶段：失败退出码 1；无变更退出码 0 且不提交。"""

    def test_scan_failure_returns_1(self):
        with patch.object(cli.service, "load_token_rules", return_value=[]), \
                patch.object(cli.service, "load_blacklist", return_value=[]), \
                patch.object(cli.service, "scan_changed",
                             return_value={"ok": False, "stage": "load_index",
                                           "error": "boom"}), \
                patch.object(cli.service, "submit_all") as submit_mock:
            assert cli.main([]) == 1
            submit_mock.assert_not_called()

    def test_no_changes_returns_0(self):
        with patch.object(cli.service, "load_token_rules", return_value=[]), \
                patch.object(cli.service, "load_blacklist", return_value=[]), \
                patch.object(cli.service, "scan_changed",
                             return_value=_scan_result()), \
                patch.object(cli.service, "submit_all") as submit_mock:
            assert cli.main([]) == 0
            submit_mock.assert_not_called()


class TestSubmitPhase:
    """提交阶段：全成功 0；部分失败 2；规则与清单正确透传。"""

    def _run(self, batch_result):
        changed = [_changed_item()]
        rules = [{"keyword": "MindStudio", "token": "tokA"}]
        with patch.object(cli.service, "load_token_rules",
                          return_value=rules), \
                patch.object(cli.service, "load_blacklist", return_value=[]), \
                patch.object(cli.service, "scan_changed",
                             return_value=_scan_result(changed)) as scan_mock, \
                patch.object(cli.service, "submit_all",
                             return_value=batch_result) as submit_mock:
            code = cli.main([])
        return code, scan_mock, submit_mock, changed, rules

    def test_all_success_returns_0(self):
        code, _, submit_mock, changed, rules = self._run({
            "ok": True,
            "results": [{"path": "Ascend/26.1.0/MindStudio/msprof",
                         "ok": True, "pr_url": "http://x/1"}],
            "submitted": 1,
            "failed": 0,
        })
        assert code == 0
        # changed 清单与解析后的规则原样透传
        assert submit_mock.call_args.args[0] == changed
        assert submit_mock.call_args.kwargs["token_rules"] == rules

    def test_partial_failure_returns_2(self):
        code, *_ = self._run({
            "ok": True,
            "results": [
                {"path": "a", "ok": True, "pr_url": "http://x/1"},
                {"path": "b", "ok": False, "stage": "create_pr",
                 "error": "boom"},
            ],
            "submitted": 1,
            "failed": 1,
        })
        assert code == 2

    def test_batch_failure_returns_1(self):
        code, *_ = self._run({
            "ok": False, "stage": "validate", "error": "默认 Token 不能为空",
        })
        assert code == 1

    def test_pr_existed_marked_in_log(self, capsys):
        code, *_ = self._run({
            "ok": True,
            "results": [{"path": "Ascend/26.1.0/MindStudio/msprof",
                         "ok": True, "pr_url": "http://x/42",
                         "pr_existed": True}],
            "submitted": 1,
            "failed": 0,
        })
        assert code == 0
        assert "已存在未合并 PR" in capsys.readouterr().out

    def test_keyword_passed_to_scan(self):
        code, scan_mock, *_ = self._run({
            "ok": True, "results": [], "submitted": 0, "failed": 0,
        })
        # 上面 _run 固定走 cli.main([])，单独验证 --keyword 透传
        assert code == 0
        with patch.object(cli.service, "load_token_rules", return_value=[]), \
                patch.object(cli.service, "load_blacklist", return_value=[]), \
                patch.object(cli.service, "scan_changed",
                             return_value=_scan_result()) as sm:
            cli.main(["--keyword", "msprof"])
            assert sm.call_args.args[0] == "msprof"

    def test_sync_source_flag(self):
        """--sync-source 时在加载规则前先同步来源 Fork。"""
        with patch.object(cli.service, "sync_source") as sync_mock, \
                patch.object(cli.service, "load_token_rules",
                             return_value=[]), \
                patch.object(cli.service, "load_blacklist", return_value=[]), \
                patch.object(cli.service, "scan_changed",
                             return_value=_scan_result()):
            assert cli.main(["--sync-source"]) == 0
            sync_mock.assert_called_once()

    def test_sync_source_default_off(self):
        """默认不同步来源仓库。"""
        with patch.object(cli.service, "sync_source") as sync_mock, \
                patch.object(cli.service, "load_token_rules",
                             return_value=[]), \
                patch.object(cli.service, "load_blacklist", return_value=[]), \
                patch.object(cli.service, "scan_changed",
                             return_value=_scan_result()):
            assert cli.main([]) == 0
            sync_mock.assert_not_called()


class TestLocalModeEnv:
    """本地化开关：由环境变量注入，透传到规则加载与扫描。"""

    def test_local_env_passed_through(self, monkeypatch):
        monkeypatch.setenv("GITCODE_LOCAL_SOURCE_DIR", "/src")
        monkeypatch.setenv("GITCODE_CLONE_DIR", "/clones")
        with patch.object(cli.service, "load_token_rules",
                          return_value=[]) as load_mock, \
                patch.object(cli.service, "load_blacklist", return_value=[]), \
                patch.object(cli.service, "scan_changed",
                             return_value=_scan_result()) as scan_mock:
            assert cli.main([]) == 0
            _, kwargs = load_mock.call_args
            assert kwargs["source_repo_dir"] == "/src"
            _, kwargs = scan_mock.call_args
            assert kwargs["source_repo_dir"] == "/src"
            assert kwargs["clone_root"] == "/clones"

    def test_no_local_env_defaults_none(self, monkeypatch):
        monkeypatch.delenv("GITCODE_LOCAL_SOURCE_DIR", raising=False)
        monkeypatch.delenv("GITCODE_CLONE_DIR", raising=False)
        with patch.object(cli.service, "load_token_rules",
                          return_value=[]) as load_mock, \
                patch.object(cli.service, "load_blacklist", return_value=[]), \
                patch.object(cli.service, "scan_changed",
                             return_value=_scan_result()) as scan_mock:
            assert cli.main([]) == 0
            _, kwargs = load_mock.call_args
            assert kwargs["source_repo_dir"] is None
            _, kwargs = scan_mock.call_args
            assert kwargs["source_repo_dir"] is None
            assert kwargs["clone_root"] is None
