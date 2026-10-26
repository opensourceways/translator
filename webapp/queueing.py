"""提交任务队列：FIFO、有界、不合并。

与旧 push 合并队列不同，API 每个请求都必须有独立结果，不能被合并。
队列只保存 (submission_id, token) 二元组；token 仅在内存中传递，不落库。
"""

import queue


class SubmissionQueue:
    """有界 FIFO 队列：满时 push 返回 False，由 API 转 429。"""

    def __init__(self, maxsize=100):
        self._queue = queue.Queue(maxsize=max(int(maxsize), 1))

    def push(self, submission_id, token):
        """入队；队列满返回 False（不阻塞 HTTP 请求线程）。"""
        try:
            self._queue.put_nowait((submission_id, token))
        except queue.Full:
            return False
        return True

    def pop(self, timeout=1.0):
        """取出一个任务；超时返回 None。"""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def pending_count(self):
        """当前排队任务数。"""
        return self._queue.qsize()
