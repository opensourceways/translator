"""pytest 共享工具：提供统一的 mock 对象工厂。

所有测试均不发起真实网络请求——通过 mock requests 调用
控制 API 返回值，保证测试快速且可靠。
"""

from unittest.mock import MagicMock

import pytest

from docsync import client


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    """每个测试前清空进程级限流计数。

    限流器按 60 秒窗口全局计数，测试不做真实等待；不重置的话
    前序测试累积的计数可能触发 _throttle 进入 sleep（被 mock 掉的
    time.sleep 会打乱重试断言）。
    """
    client._reset_rate_limit()
    yield


def make_ok_response(json_data=None):
    """构造一个模拟的 requests 成功响应（status 200, ok=True）。"""
    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    resp.json.return_value = json_data if json_data is not None else {}
    return resp


def make_error_response(status_code=403, json_body=None):
    """构造一个模拟的 requests 失败响应。

    默认 403 + Forbidden 消息，可指定其他状态码和返回体。
    """
    msg = json_body or {"message": "Forbidden"}
    resp = MagicMock()
    resp.ok = False
    resp.status_code = status_code
    resp.json.return_value = msg
    resp.text = str(msg)
    return resp
