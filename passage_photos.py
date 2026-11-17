"""Bounded in-memory cache for photos received with live camera events."""
from collections import OrderedDict
from threading import Lock

class PhotoCache:
    def __init__(self, max_items=30, max_bytes=48 * 1024 * 1024):
        self.max_items = max_items
        self.max_bytes = max_bytes
        self.items = OrderedDict()
        self.total = 0
        self.lock = Lock()

    def put(self, captured_at, plate, image):
        if not captured_at or not image or len(image) > self.max_bytes:
            return
        key = (captured_at, plate)
        with self.lock:
            old = self.items.pop(key, None)
            if old: self.total -= len(old)
            self.items[key] = bytes(image)
            self.total += len(image)
            while len(self.items) > self.max_items or self.total > self.max_bytes:
                _, removed = self.items.popitem(last=False)
                self.total -= len(removed)

    def get(self, captured_at, plate):
        with self.lock:
            return self.items.get((captured_at, plate))

PHOTOS = PhotoCache()
