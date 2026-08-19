"""client 模块的单元测试。

覆盖 GitCode API 客户端的请求方法、路径、参数位置和错误处理。
所有测试通过 mock requests.request 拦截，不发起真实网络请求。
"""

from unittest.mock import patch

import pytest

from translator_ci import client
from translator_ci.client import GitCodeError
from tests.conftest import make_error_response, make_ok_response


class TestRequestBasics:
    """请求基础行为：鉴权头、错误提取、429 重试。"""

    def test_bearer_token_header(self):
        with patch("translator_ci.client.requests.request",
                   return_value=make_ok_response({})) as m:
            client.get_repo("Ascend", "msprof", "tok")
            headers = m.call_args.kwargs["headers"]
            assert headers["Authorization"] == "Bearer tok"

    def test_error_raises_gitcode_error(self):
        resp = make_error_response(403, {"message": "Forbidden"})
        with patch("translator_ci.client.requests.request", return_value=resp):
            with pytest.raises(GitCodeError) as exc_info:
                client.get_repo("Ascend", "msprof", "tok")
            assert exc_info.value.status_code == 403
            assert "Forbidden" in str(exc_info.value)

    def test_429_retries_then_succeeds(self):
        responses = [make_error_response(429), make_ok_response({"ok": 1})]
        with patch("translator_ci.client.requests.request",
                   side_effect=responses), \
                patch("translator_ci.client.time.sleep") as sleep_mock:
            result = client.get_repo("Ascend", "msprof", "tok")
            assert result == {"ok": 1}
            sleep_mock.assert_called_once()

    def test_429_exhausts_retries(self):
        with patch("translator_ci.client.requests.request",
                   return_value=make_error_response(429)), \
                patch("translator_ci.client.time.sleep"):
            with pytest.raises(GitCodeError) as exc_info:
                client.get_repo("Ascend", "msprof", "tok")
            assert exc_info.value.status_code == 429


class TestEndpoints:
    """各端点的方法、路径与参数位置（易混点：body vs query）。"""

    def _call(self, func, *args, **kwargs):
        with patch("translator_ci.client.requests.request",
                   return_value=make_ok_response({})) as m:
            func(*args, **kwargs)
            return m

    def test_sync_repo_branch_in_json_body(self):
        m = self._call(client.sync_repo, "user", "repo", "tok", branch="26.1.0")
        assert m.call_args.args[0] == "PUT"
        assert m.call_args.kwargs["json"] == {"branch": "26.1.0"}
        assert "params" not in m.call_args.kwargs

    def test_sync_repo_full_without_branch(self):
        m = self._call(client.sync_repo, "user", "repo", "tok")
        assert m.call_args.kwargs["json"] is None

    def test_get_sync_status_branch_in_query(self):
        m = self._call(client.get_sync_status, "user", "repo", "tok",
                       branch="26.1.0")
        assert m.call_args.args[0] == "GET"
        assert m.call_args.kwargs["params"] == {"branch": "26.1.0"}

    def test_create_branch_payload(self):
        m = self._call(client.create_branch, "user", "repo",
                       "translator/auto-26.1.0", "abc123", "tok")
        assert m.call_args.kwargs["json"] == {
            "refs": "abc123", "branch_name": "translator/auto-26.1.0",
        }

    def test_create_pr_with_fork_path(self):
        m = self._call(client.create_pr, "Ascend", "msprof", "title",
                       "user:translator/auto-26.1.0", "26.1.0", "body",
                       "tok", fork_path="user/MindStudio-Profiler")
        payload = m.call_args.kwargs["json"]
        assert payload["fork_path"] == "user/MindStudio-Profiler"
        assert payload["prune_source_branch"] is True
        assert payload["head"] == "user:translator/auto-26.1.0"
        assert payload["base"] == "26.1.0"

    def test_list_pull_requests_filters(self):
        m = self._call(client.list_pull_requests, "Ascend", "msprof", "tok",
                       state="open", head="user:translator/auto-26.1.0",
                       base="26.1.0")
        assert m.call_args.args[0] == "GET"
        assert m.call_args.args[1].endswith("/repos/Ascend/msprof/pulls")
        params = m.call_args.kwargs["params"]
        assert params["state"] == "open"
        assert params["head"] == "user:translator/auto-26.1.0"
        assert params["base"] == "26.1.0"

    def test_list_pull_requests_default_open(self):
        m = self._call(client.list_pull_requests, "Ascend", "msprof", "tok")
        params = m.call_args.kwargs["params"]
        assert params["state"] == "open"
        assert "head" not in params
        assert "base" not in params

    def test_update_file_carries_sha(self):
        m = self._call(client.update_file, "user", "repo", "a.md", "msg",
                       "YQ==", "sha1", "translator/auto-26.1.0", "tok")
        assert m.call_args.args[0] == "PUT"
        payload = m.call_args.kwargs["json"]
        assert payload["sha"] == "sha1"
        assert payload["branch"] == "translator/auto-26.1.0"


class TestGetContents:
    """get_contents 的 404 语义：文件不存在返回 None，其余错误抛出。"""

    def test_404_returns_none(self):
        with patch("translator_ci.client.requests.request",
                   return_value=make_error_response(404)):
            assert client.get_contents("o", "r", "a.md", "master", "tok") is None

    def test_500_raises(self):
        with patch("translator_ci.client.requests.request",
                   return_value=make_error_response(500)):
            with pytest.raises(GitCodeError):
                client.get_contents("o", "r", "a.md", "master", "tok")


class TestListDir:
    """list_dir 的目录语义：非数组响应对应「路径不是目录」错误。"""

    def test_file_path_raises(self):
        with patch("translator_ci.client.requests.request",
                   return_value=make_ok_response({"type": "file"})):
            with pytest.raises(GitCodeError) as exc_info:
                client.list_dir("o", "r", "a.md", "master", "tok")
            assert "不是目录" in str(exc_info.value)

    def test_root_path_no_trailing_slash(self):
        with patch("translator_ci.client.requests.request",
                   return_value=make_ok_response([])) as m:
            client.list_dir("o", "r", "", "master", "tok")
            assert m.call_args.args[1].endswith("/repos/o/r/contents")
