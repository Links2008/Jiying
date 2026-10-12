"""事件总线测试：发布订阅、退订、满队列丢帧。"""
import queue

import pytest

from app.core.events import AppEvent, EventBus


def test_pubsub_roundtrip():
    b = EventBus()
    q = b.subscribe()
    b.publish(AppEvent(type="progress", task_id="t1", fraction=0.5, speed_bps=1024))
    ev = q.get(timeout=1)
    assert ev.type == "progress" and ev.task_id == "t1" and ev.fraction == 0.5


def test_unsubscribe_stops_delivery():
    b = EventBus()
    q = b.subscribe()
    b.unsubscribe(q)
    b.publish(AppEvent(type="stage", stage="download", status="done"))
    with pytest.raises(queue.Empty):
        q.get(timeout=0.1)


def test_full_queue_drops_instead_of_blocking():
    b = EventBus(max_queue=1)
    q = b.subscribe()
    b.publish(AppEvent(type="progress", fraction=0.1))
    b.publish(AppEvent(type="progress", fraction=0.2))  # 队列满 → 丢弃，不抛异常
    assert q.get(timeout=1).fraction == 0.1


def test_sse_format():
    s = AppEvent(type="stage", task_id="t1", stage="render", status="done").to_sse()
    assert s.startswith("data: {") and s.endswith("}\n\n")
