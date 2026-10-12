"""CoalescingQueue 合并队列与 Trigger 合并语义的单元测试。"""

import threading

from webapp.queueing import CoalescingQueue, Trigger


def _push(i, ref="refs/heads/main"):
    """构造第 i 个 push 事件：received_at 严格递增。"""
    return Trigger.from_push(
        ref=ref, before_sha=f"b{i:02d}", after_sha=f"a{i:02d}",
        push_event_id=i,
        received_at=f"2026-09-29T00:00:{i:02d}+00:00")


class TestTriggerMerge:
    """latest-wins 合并：before 取最早、after 取最新、来源信息累积。"""

    def test_push_push_latest_wins(self):
        first, second = _push(1), _push(2)
        first.merge(second)
        assert first.before_sha == "b01"
        assert first.after_sha == "a02"
        assert first.trigger == "push"
        assert first.full_scan is False
        assert first.push_event_ids == [1, 2]
        assert first.merged_count == 2
        assert first.received_at == "2026-09-29T00:00:01+00:00"

    def test_merge_ordered_by_received_at(self):
        """合并顺序乱序（后到的事件 received_at 更早）时仍按到达时间取 sha。"""
        first, second = _push(1), _push(2)
        second.merge(first)
        assert second.before_sha == "b01"
        assert second.after_sha == "a02"

    def test_three_way_merge(self):
        pending = _push(1)
        pending.merge(_push(2))
        pending.merge(_push(3))
        assert pending.before_sha == "b01"
        assert pending.after_sha == "a03"
        assert pending.push_event_ids == [1, 2, 3]
        assert pending.merged_count == 3

    def test_manual_full_scan_drops_sha(self):
        """手动全量扫并入 pending 的 push：before/after 丢弃。

        全量扫以来源仓当前 HEAD 为准，天然覆盖 pending 的增量范围；
        保留 sha 反而会让 Worker 误走增量、漏掉范围外变更。
        """
        pending = _push(1)
        pending.merge(Trigger.manual(full_scan=True))
        assert pending.full_scan is True
        assert pending.before_sha is None
        assert pending.after_sha is None
        assert pending.trigger == "manual"
        assert pending.push_event_ids == [1]
        assert pending.merged_count == 2

    def test_push_merged_into_pending_manual_also_drops_sha(self):
        """反方向同样成立：push 并入 pending 的手动全量扫，sha 仍丢弃。"""
        pending = Trigger.manual()
        pending.merge(_push(1))
        assert pending.full_scan is True
        assert pending.before_sha is None
        assert pending.after_sha is None
        assert pending.push_event_ids == [1]

    def test_keyword_merge_with_push_upgrades_to_full(self):
        """关键字扫与 push 范围互不保证覆盖，合并升级为全量扫。"""
        pending = Trigger.manual(keyword="msprof")
        pending.merge(_push(1))
        assert pending.full_scan is True
        assert pending.keyword == ""

    def test_same_keyword_merge_keeps_keyword(self):
        """相同关键字的手动触发合并：保持关键字扫，不扩大范围。"""
        pending = Trigger.manual(keyword="msprof")
        pending.merge(Trigger.manual(keyword="msprof"))
        assert pending.full_scan is False
        assert pending.keyword == "msprof"
        assert pending.merged_count == 2

    def test_different_keywords_upgrade_to_full(self):
        """不同关键字无法用单次关键字扫覆盖，升级为全量扫。"""
        pending = Trigger.manual(keyword="msprof")
        pending.merge(Trigger.manual(keyword="cann"))
        assert pending.full_scan is True
        assert pending.keyword == ""

    def test_different_ref_drops_to_full(self):
        """不同分支的 push 无法合成单一增量区间，回退全量（宁多勿漏）。"""
        pending = _push(1)
        pending.merge(_push(2, ref="refs/heads/dev"))
        assert pending.full_scan is True
        assert pending.before_sha is None
        assert pending.after_sha is None

    def test_commit_same_sha_merge_keeps_commit(self):
        """同一 commit 的两个触发合并：保持单 commit 变更集语义。"""
        pending = Trigger.commit_sha("c" * 40)
        pending.merge(Trigger.commit_sha(
            "c" * 40, received_at="2026-09-29T00:00:09+00:00"))
        assert pending.trigger == "commit"
        assert pending.commit == "c" * 40
        assert pending.full_scan is False
        assert pending.merged_count == 2

    def test_commit_different_sha_upgrades_to_full(self):
        """不同 commit 无法合成单一增量区间，升级全量（宁多勿漏）。"""
        pending = Trigger.commit_sha("c" * 40)
        pending.merge(Trigger.commit_sha(
            "d" * 40, received_at="2026-09-29T00:00:09+00:00"))
        assert pending.full_scan is True
        assert pending.commit == ""
        assert pending.trigger == "manual"

    def test_commit_merged_with_push_upgrades_to_full(self):
        """单 commit 与 push 合并：范围不互保证覆盖，升级全量。"""
        pending = Trigger.commit_sha("c" * 40)
        pending.merge(_push(1))
        assert pending.full_scan is True
        assert pending.commit == ""

    def test_missing_sha_drops_to_full(self):
        """任一 sha 缺失时增量区间无意义，回退全量。"""
        pending = _push(1)
        pending.merge(Trigger.from_push(
            ref="refs/heads/main", before_sha=None, after_sha="a02",
            push_event_id=2,
            received_at="2026-09-29T00:00:02+00:00"))
        assert pending.full_scan is True


class TestCoalescingQueue:
    """容量 1：单格 pending，满时合并吸收；Condition 阻塞 pop。"""

    def test_put_pop_single(self):
        q = CoalescingQueue()
        event = _push(1)
        assert q.push(event) is True
        assert q.pending_count() == 1
        assert q.pop() is event
        assert q.pending_count() == 0

    def test_second_push_merges_not_queues(self):
        """已有 pending 时新 push 合并吸收：不新增元素、不丢事件。"""
        q = CoalescingQueue()
        assert q.push(_push(1)) is True
        assert q.push(_push(2)) is False
        assert q.pending_count() == 1
        item = q.pop()
        assert item.before_sha == "b01"
        assert item.after_sha == "a02"
        assert item.merged_count == 2
        assert q.pop(timeout=0.05) is None

    def test_pop_blocks_until_push(self):
        """空队列 pop 阻塞，push 后唤醒。"""
        q = CoalescingQueue()
        results = []
        consumer = threading.Thread(target=lambda: results.append(q.pop()))
        consumer.start()
        consumer.join(timeout=0.2)
        assert consumer.is_alive()  # 仍在阻塞等待
        assert results == []
        q.push(_push(1))
        consumer.join(timeout=2)
        assert not consumer.is_alive()
        assert results[0].push_event_ids == [1]

    def test_pop_timeout_returns_none(self):
        q = CoalescingQueue()
        assert q.pop(timeout=0.05) is None

    def test_concurrent_producers_no_event_lost(self):
        """多线程并发 push：合并不丢事件，merged_count 与事件 id 全集守恒。"""
        q = CoalescingQueue()
        n_threads, per_thread = 5, 10
        total = n_threads * per_thread
        collected = []

        def produce(base):
            for i in range(per_thread):
                q.push(_push(base + i))

        def consume():
            while sum(t.merged_count for t in collected) < total:
                item = q.pop(timeout=2)
                assert item is not None
                collected.append(item)

        consumer = threading.Thread(target=consume)
        consumer.start()
        producers = [
            threading.Thread(target=produce, args=(k * per_thread,))
            for k in range(n_threads)
        ]
        for p in producers:
            p.start()
        for p in producers:
            p.join()
        consumer.join(timeout=10)
        assert not consumer.is_alive()
        assert sum(t.merged_count for t in collected) == total
        ids = [eid for t in collected for eid in t.push_event_ids]
        assert sorted(ids) == list(range(total))
