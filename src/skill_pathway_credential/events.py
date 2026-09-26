"""事件存储：追加式事件日志，可选 JSONL 持久化，支持按日期回放。

一切状态变化先写事件再折叠为投影；服务中断后从日志重放即可恢复，
恢复后未完成的到期复核仍然可见，可继续办理。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class Event:
    """一次状态变化的不可变记录。"""

    seq: int
    kind: str
    occurred_on: date
    payload: dict[str, Any]

    def to_line(self) -> str:
        return json.dumps(
            {
                "seq": self.seq,
                "kind": self.kind,
                "occurred_on": self.occurred_on.isoformat(),
                "payload": self.payload,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def from_line(line: str) -> "Event":
        raw = json.loads(line)
        return Event(
            seq=raw["seq"],
            kind=raw["kind"],
            occurred_on=date.fromisoformat(raw["occurred_on"]),
            payload=raw["payload"],
        )


class EventStore:
    """线程安全的追加式事件日志。"""

    def __init__(self, path: Path | None = None):
        self._path = Path(path) if path else None
        self._lock = threading.Lock()
        self._events: list[Event] = []
        if self._path and self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._events.append(Event.from_line(line))

    def next_seq(self) -> int:
        return len(self._events) + 1

    def append(self, kind: str, payload: dict[str, Any], on: date) -> Event:
        return self.append_many([(kind, payload)], on)[0]

    def append_many(
        self, items: Iterable[tuple[str, dict[str, Any]]], on: date
    ) -> list[Event]:
        """原子追加一批事件：批量转段等操作要么全部落账，要么全部不落。"""
        with self._lock:
            events = [
                Event(seq=self.next_seq() + i, kind=kind, occurred_on=on, payload=payload)
                for i, (kind, payload) in enumerate(items)
            ]
            if self._path:
                with self._path.open("a", encoding="utf-8") as handle:
                    for event in events:
                        handle.write(event.to_line() + "\n")
            self._events.extend(events)
            return events

    def events(self, upto: date | None = None) -> list[Event]:
        """全部事件，或截至指定日期（含）的事件——as-of 还原的数据基础。"""
        if upto is None:
            return list(self._events)
        return [event for event in self._events if event.occurred_on <= upto]
