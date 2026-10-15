"""Canonical application state and the event bus that broadcasts it."""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from collections import deque
from typing import Any

log = logging.getLogger(__name__)

SUBSCRIBER_QUEUE_SIZE = 100
RECENT_EVENTS = 30


class EventBus:
    """Fan-out of server events to WebSocket subscribers.

    Each subscriber has a bounded queue. A subscriber that falls behind is
    dropped (its connection is closed and the client resynchronizes from a
    fresh snapshot) rather than letting memory grow without bound.
    """

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    @property
    def count(self) -> int:
        return len(self._subscribers)

    def publish(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("dropping slow WebSocket subscriber")
                self._subscribers.discard(queue)
                try:
                    queue.put_nowait({"type": "_overflow"})
                except asyncio.QueueFull:
                    pass


class StateManager:
    """Owns the canonical state. All mutations go through ``update``."""

    def __init__(self, bus: EventBus, *, version: str, host: str, port: int, allow_lan: bool):
        self._bus = bus
        self._lock = asyncio.Lock()
        self._events: deque[dict[str, Any]] = deque(maxlen=RECENT_EVENTS)
        self._state: dict[str, Any] = {
            "revision": 0,
            "application": {"running": True, "version": version, "started_at": time.time()},
            "presence": {
                "enabled": False,
                "connection_state": "DISCONNECTED",
                "last_successful_update": None,
                "active_preset_id": None,
                "current_activity": None,
                "applied": True,
                "enabled_by": None,
            },
            "discord": {
                "rpc_connected": False,
                "connected_client_id": None,
                "last_error": None,
                "reconnect_attempt": 0,
                "next_retry_in": None,
            },
            "server": {
                "host": host,
                "port": port,
                "allow_lan": allow_lan,
                "lan_active": allow_lan,
                "restart_required": False,
                "websocket_connected_clients": 0,
            },
            "configuration": {"configured": False, "configuration_valid": False, "problems": [], "warnings": []},
            "auto_switch": {"enabled": False, "matched_preset_id": None},
        }

    def snapshot(self) -> dict[str, Any]:
        state = copy.deepcopy(self._state)
        state["recent_events"] = list(self._events)
        state["server"]["websocket_connected_clients"] = self._bus.count
        return state

    async def update(self, **sections: dict[str, Any]) -> None:
        """Merge partial section updates and broadcast if anything changed."""
        async with self._lock:
            changed = False
            for name, values in sections.items():
                target = self._state[name]
                for key, value in values.items():
                    if target.get(key) != value:
                        target[key] = copy.deepcopy(value)
                        changed = True
            if changed:
                self._state["revision"] += 1
                self._bus.publish({"type": "state.updated", "payload": self.snapshot()})

    async def record_event(self, kind: str, message: str, *, success: bool = True, source: str = "system") -> None:
        async with self._lock:
            self._events.append({"at": time.time(), "kind": kind, "message": message, "success": success, "source": source})
            self._state["revision"] += 1
            self._bus.publish({"type": "state.updated", "payload": self.snapshot()})

    def broadcast(self, event_type: str, payload: Any) -> None:
        self._bus.publish({"type": event_type, "payload": payload})

    def clients_changed(self) -> None:
        self._bus.publish({"type": "state.updated", "payload": self.snapshot()})
