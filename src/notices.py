"""Friendly, user-facing notices shown as dashboard banners (and tray toasts)."""
from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from typing import Callable


@dataclass
class Notice:
    id: str
    level: str  # "info" | "warn" | "error"
    message: str
    dismissible: bool = True


class Notices:
    def __init__(self) -> None:
        self._items: dict[str, Notice] = {}
        self._lock = threading.Lock()
        self._listeners: list[Callable[[], None]] = []

    def add_listener(self, cb: Callable[[], None]) -> None:
        self._listeners.append(cb)

    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()

    def set(self, id: str, level: str, message: str, dismissible: bool = True) -> None:
        n = Notice(id, level, message, dismissible)
        with self._lock:
            if self._items.get(id) == n:
                return
            self._items[id] = n
        self._notify()

    def clear(self, id: str) -> None:
        with self._lock:
            if self._items.pop(id, None) is None:
                return
        self._notify()

    def all(self) -> list[dict]:
        with self._lock:
            return [asdict(n) for n in self._items.values()]
