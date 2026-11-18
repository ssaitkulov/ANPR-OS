"""Bounded, in-memory stream of operational events for the local control panel."""
from collections import deque
from threading import Lock
from uuid import uuid4


class EventLog:
    def __init__(self, capacity=200):
        self._events = deque(maxlen=capacity)
        self._lock = Lock()
        self._last_id = 0
        self.instance = uuid4().hex

    def add(self, kind, text, timestamp):
        with self._lock:
            self._last_id += 1
            event = {'id': self._last_id, 'time': timestamp, 'kind': kind, 'text': text}
            self._events.append(event)
            return event.copy()

    def snapshot(self, after=0):
        with self._lock:
            return {'instance': self.instance, 'last_id': self._last_id,
                    'events': [event.copy() for event in self._events if event['id'] > after]}


EVENTS = EventLog()
