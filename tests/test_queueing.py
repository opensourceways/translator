"""webapp.queueing：有界 FIFO 提交队列。"""

import time

from webapp.queueing import SubmissionQueue


def test_push_pop_fifo():
    q = SubmissionQueue(maxsize=10)
    assert q.push(1, "t1") is True
    assert q.push(2, "t2") is True
    assert q.pending_count() == 2
    assert q.pop(timeout=0.01) == (1, "t1")
    assert q.pop(timeout=0.01) == (2, "t2")


def test_push_returns_false_when_full():
    q = SubmissionQueue(maxsize=1)
    assert q.push(1, "t") is True
    assert q.push(2, "t") is False       # 满：不阻塞、不抛异常
    assert q.pending_count() == 1


def test_pop_timeout_returns_none():
    q = SubmissionQueue(maxsize=1)
    started = time.monotonic()
    assert q.pop(timeout=0.05) is None
    assert time.monotonic() - started >= 0.04


def test_maxsize_at_least_one():
    q = SubmissionQueue(maxsize=0)
    assert q.push(1, "t") is True
    assert q.push(2, "t") is False
