"""Tiny thread-safe pub/sub feeding the browser's Server-Sent Events stream."""
import json
import queue
import threading

_subscribers = set()
_lock = threading.Lock()


def subscribe():
    q = queue.Queue(maxsize=2000)
    with _lock:
        _subscribers.add(q)
    return q


def unsubscribe(q):
    with _lock:
        _subscribers.discard(q)


def publish(kind, **data):
    message = json.dumps({"type": kind, **data}, ensure_ascii=False, default=str)
    with _lock:
        targets = list(_subscribers)
    for q in targets:
        try:
            q.put_nowait(message)
        except queue.Full:
            pass  # a stalled tab must not block the pipeline
