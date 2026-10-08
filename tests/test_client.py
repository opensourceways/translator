"""client 模块的单元测试。

覆盖 GitCode API 客户端的请求方法、路径、参数位置和错误处理。
所有测试通过 mock requests.request 拦截，不发起真实网络请求。
"""

from unittest.mock import patch

import pytest

from docsync import client
from docsync.client import GitCodeError
from tests.conftest import make_error_response, make_ok_response


class TestRequestBasics:
    """请求基础行为：鉴权头、错误提取、429 重试。"""

    def test_bearer_token_header(self):
        with patch("docsync.client.requests.request",
                   return_value=make_ok_response({})) as m:
            client.get_repo("Ascend", "msprof", "tok")
            headers = m.call_args.kwargs["headers"]
            assert headers["Authorization"] == "Bearer tok"

    def test_error_raises_gitcode_error(self):
        resp = make_error_response(403, {"message": "Forbidden"})
        with patch("docsync.client.requests.request", return_value=resp):
            with pytest.raises(GitCodeError) as exc_info:
                client.get_repo("Ascend", "msprof", "tok")
            assert exc_info.value.status_code == 403
            assert "Forbidden" in str(exc_info.value)

    def test_429_retries_then_succeeds(self):
        responses = [make_error_response(429), make_ok_response({"ok": 1})]
        with patch("docsync.client.requests.request",
                   side_effect=responses), \
                patch("docsync.client.time.sleep") as sleep_mock:
            result = client.get_repo("Ascend", "msprof", "tok")
            assert result == {"ok": 1}
            sleep_mock.assert_called_once()

    def test_429_exhausts_retries(self):
        with patch("docsync.client.requests.request",
                   return_value=make_error_response(429)), \
                patch("docsync.client.time.sleep"):
            with pytest.raises(GitCodeError) as exc_info:
                client.get_repo("Ascend", "msprof", "tok")
            assert exc_info.value.status_code == 429


class TestEndpoints:
    """各端点的方法、路径与参数位置（易混点：body vs query）。"""

    def _call(self, func, *args, **kwargs):
        with patch("docsync.client.requests.request",
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

    def test_create_pr_with_fork_path(self):
        m = self._call(client.create_pr, "Ascend", "msprof", "title",
                       "user:translator/auto-26.1.0", "26.1.0", "body",
                       "tok", fork_path="user/MindStudio-Profiler")
        payload = m.call_args.kwargs["json"]
        assert payload["fork_path"] == "user/MindStudio-Profiler"
        assert payload["prune_source_branch"] is True
        assert payload["head"] == "user:translator/auto-26.1.0"
        assert payload["base"] == "26.1.0"

    def test_list_pull_requests_no_server_filters(self):
        """GitCode 服务端不支持 head/base 过滤（带参返回空），不得传参。"""
        with patch("docsync.client.requests.request",
                   return_value=make_ok_response([])) as m:
            client.list_pull_requests("Ascend", "msprof", "tok", state="open")
        assert m.call_args.args[0] == "GET"
        assert m.call_args.args[1].endswith("/repos/Ascend/msprof/pulls")
        params = m.call_args.kwargs["params"]
        assert params == {"state": "open", "page": 1, "per_page": 100}
        assert "head" not in params
        assert "base" not in params

    def test_list_pull_requests_default_open(self):
        with patch("docsync.client.requests.request",
                   return_value=make_ok_response([])) as m:
            client.list_pull_requests("Ascend", "msprof", "tok")
        params = m.call_args.kwargs["params"]
        assert params["state"] == "open"
        assert "head" not in params
        assert "base" not in params

    def test_get_pull_request_method_url_auth(self):
        m = self._call(client.get_pull_request, "Ascend", "msprof", 42, "tok")
        assert m.call_args.args[0] == "GET"
        assert m.call_args.args[1].endswith("/repos/Ascend/msprof/pulls/42")
        assert m.call_args.kwargs["headers"]["Authorization"] == "Bearer tok"

    def test_get_pull_request_returns_json(self):
        """返回的 PR 详情 JSON 原样透传。"""
        detail = {"number": 42, "state": "closed", "merged_at": "2026-01-01"}
        with patch("docsync.client.requests.request",
                   return_value=make_ok_response(detail)):
            assert client.get_pull_request("o", "r", 42, "tok") == detail


class TestGetContents:
    """get_contents 的 404 语义：文件不存在返回 None，其余错误抛出。"""

    def test_404_returns_none(self):
        with patch("docsync.client.requests.request",
                   return_value=make_error_response(404)):
            assert client.get_contents("o", "r", "a.md", "master", "tok") is None

    def test_500_raises(self):
        """持续 500 时重试耗尽后抛出 GitCodeError。"""
        with patch("docsync.client.requests.request",
                   return_value=make_error_response(500)), \
                patch("docsync.client.time.sleep"):
            with pytest.raises(GitCodeError):
                client.get_contents("o", "r", "a.md", "master", "tok")

    def test_500_retries_then_succeeds(self):
        """GET 5xx 短暂等待后重试，成功返回结果。"""
        responses = [make_error_response(500), make_ok_response({"sha": "x"})]
        with patch("docsync.client.requests.request",
                   side_effect=responses), \
                patch("docsync.client.time.sleep") as sleep_mock:
            result = client.get_contents("o", "r", "a.md", "master", "tok")
            assert result == {"sha": "x"}
            sleep_mock.assert_called_once()

    def test_5xx_non_get_not_retried(self):
        """非 GET 的 5xx 不重试（写操作不能盲目重放）。"""
        with patch("docsync.client.requests.request",
                   return_value=make_error_response(503)), \
                patch("docsync.client.time.sleep") as sleep_mock:
            with pytest.raises(GitCodeError):
                client.sync_repo("o", "r", "tok")
            sleep_mock.assert_not_called()


class TestListDir:
    """list_dir 的目录语义：非数组响应对应「路径不是目录」错误。"""

    def test_file_path_raises(self):
        with patch("docsync.client.requests.request",
                   return_value=make_ok_response({"type": "file"})):
            with pytest.raises(GitCodeError) as exc_info:
                client.list_dir("o", "r", "a.md", "master", "tok")
            assert "不是目录" in str(exc_info.value)

    def test_root_path_no_trailing_slash(self):
        with patch("docsync.client.requests.request",
                   return_value=make_ok_response([])) as m:
            client.list_dir("o", "r", "", "master", "tok")
            assert m.call_args.args[1].endswith("/repos/o/r/contents")


class TestRateLimit:
    """进程级限流：限额内请求不等待，超限后才进入等待。"""

    def test_under_limit_no_wait(self):
        with patch("docsync.client.requests.request",
                   return_value=make_ok_response({})), \
                patch("docsync.client.time.sleep") as sleep_mock:
            for _ in range(3):
                client.get_repo("o", "r", "tok")
            assert sleep_mock.call_count == 0


class TestPagination:
    """列表接口自动分页：超过单页继续拉下一页，直到取空。"""

    def _pages(self, *pages):
        return [make_ok_response(list(p)) for p in pages]

    def test_branches_multiple_pages(self):
        responses = self._pages(
            [{"name": f"b{i}"} for i in range(100)],
            [{"name": "b100"}], [],  # 第二页不足一页即停止
        )
        with patch("docsync.client.requests.request",
                   side_effect=responses) as m:
            branches = client.list_branches("o", "r", "tok")
            assert len(branches) == 101
            # 第一页 page=1，第二页 page=2
            assert m.call_args_list[0].kwargs["params"]["page"] == 1
            assert m.call_args_list[1].kwargs["params"]["page"] == 2
            assert len(m.call_args_list) == 2  # 空页不再请求

    def test_branches_empty_stops(self):
        with patch("docsync.client.requests.request",
                   return_value=make_ok_response([])) as m:
            assert client.list_branches("o", "r", "tok") == []
            assert len(m.call_args_list) == 1

    def test_pull_requests_multiple_pages(self):
        responses = self._pages(
            [{"number": i} for i in range(100)],
            [{"number": 100}],
        )
        with patch("docsync.client.requests.request",
                   side_effect=responses) as m:
            prs = client.list_pull_requests("o", "r", "tok")
            assert len(prs) == 101
            params1 = m.call_args_list[0].kwargs["params"]
            assert params1["state"] == "open" and params1["page"] == 1
            assert m.call_args_list[1].kwargs["params"]["page"] == 2

    def test_non_array_page_raises(self):
        with patch("docsync.client.requests.request",
                   return_value=make_ok_response({"error": "boom"})):
            with pytest.raises(GitCodeError):
                client.list_branches("o", "r", "tok")

