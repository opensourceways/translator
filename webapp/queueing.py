"""Push 事件合并队列：容量 1，排队事件按 latest-wins 合并为一次执行。

语义等价 Jenkins disableConcurrentBuilds（同时最多一个执行 + 至多一个
排队），但更优：排队期间到达的多个 push 不排队等位也不丢弃，而是合并进
pending 元素——Worker 一次执行即覆盖全部排队事件的变更范围。

线程安全：webhook 处理线程生产（push）、Worker 线程消费（pop）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone


def _utc_now() -> str:
    """UTC ISO8601 时间戳（与落库时间戳口径一致）。"""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Trigger:
    """队列元素：一次待执行的扫描/提交触发。

    字段命名与 push_events 表一致（received_at / ref / before_sha /
    after_sha）。before_sha/after_sha 为 None 表示没有可用增量范围，
    执行时应全量扫描。
    """

    trigger: str  # "push"（webhook）或 "manual"（手动触发）
    ref: str = ""
    before_sha: str | None = None
    after_sha: str | None = None
    received_at: str = field(default_factory=_utc_now)
    full_scan: bool = False
    keyword: str = ""
    push_event_ids: list[int] = field(default_factory=list)
    merged_count: int = 1  # 合并进来的原始事件总数（含自身）

    @classmethod
    def from_push(cls, ref, before_sha, after_sha, push_event_id,
                  received_at=None):
        """构造 webhook push 触发（带增量范围）。"""
        return cls(trigger="push", ref=ref, before_sha=before_sha,
                   after_sha=after_sha,
                   received_at=received_at or _utc_now(),
                   push_event_ids=[push_event_id])

    @classmethod
    def manual(cls, full_scan=True, keyword="", received_at=None):
        """构造手动触发（无 sha）：关键字过滤扫或全量扫（缺省）。"""
        if keyword:
            full_scan = False
        return cls(trigger="manual", received_at=received_at or _utc_now(),
                   full_scan=full_scan, keyword=keyword)

    def merge(self, other: "Trigger") -> None:
        """把 other 合并进本元素（latest-wins）。

        - push + push：before_sha 取较早事件的（增量起点前移）、after_sha
          取较新事件的，合成覆盖两次 push 的完整增量区间；ref 不一致或任一
          sha 缺失时增量区间无意义，回退全量（宁多勿漏，与扫描层 diff 失败
          回退一致）。
        - 任一方为全量扫：结果为全量扫，before/after 一律丢弃——全量扫以
          来源仓当前 HEAD 为准，天然覆盖 pending 的增量范围；保留 sha 反而
          会让 Worker 误走增量、漏掉范围外变更。
        - 关键字扫与 push 或不同关键字合并：升级为全量扫——关键字范围与
          对方范围互不保证覆盖，全量是两者意图的并集。
        """
        first, last = (self, other) if self.received_at <= other.received_at \
            else (other, self)
        if self.full_scan or other.full_scan:
            self._to_full()
        elif self.keyword or other.keyword:
            if self.keyword and self.keyword == other.keyword:
                pass  # 相同关键字：保持关键字扫
            else:
                self._to_full()
        elif self.ref != other.ref or not all(
                (self.before_sha, self.after_sha,
                 other.before_sha, other.after_sha)):
            self._to_full()
        else:
            self.before_sha = first.before_sha
            self.after_sha = last.after_sha
        if other.trigger == "manual":
            self.trigger = "manual"
        self.ref = last.ref or self.ref
        self.received_at = first.received_at
        self.push_event_ids = self.push_event_ids + other.push_event_ids
        self.merged_count += other.merged_count

    def _to_full(self) -> None:
        """转为全量扫：清空增量范围与关键字限制。"""
        self.full_scan = True
        self.keyword = ""
        self.before_sha = None
        self.after_sha = None


class CoalescingQueue:
    """容量 1 的合并队列：只有一格 pending。

    队列为空时 push 占位并唤醒消费者；已有 pending 时新事件合并进 pending，
    永不拒绝、永不阻塞生产者。
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._pending: Trigger | None = None

    def push(self, event: Trigger) -> bool:
        """入队。返回 True 表示新占位，False 表示已合并进 pending。"""
        with self._cond:
            if self._pending is None:
                self._pending = event
                self._cond.notify()
                return True
            self._pending.merge(event)
            return False

    def pop(self, timeout: float | None = None) -> Trigger | None:
        """取走 pending 元素；为空时阻塞等待，timeout 秒超时返回 None。

        Worker 主循环用 timeout 以便周期性检查退出标志。
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while self._pending is None:
                if deadline is None:
                    self._cond.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)
            item, self._pending = self._pending, None
            return item

    def pending_count(self) -> int:
        """pending 元素个数（0 或 1），供 /healthz 使用。"""
        with self._cond:
            return 1 if self._pending is not None else 0
