"""Einfaches Pub/Sub fuer WebSocket-Clients."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Set

QUEUE_SIZE = 2000


class Broadcaster:
    """Verteilt Nachrichten an alle Abonnenten eines Themas.

    Langsame Clients blockieren niemals den Produzenten: ist die Queue voll,
    werden die aeltesten Nachrichten verworfen und der Client erhaelt einen
    Hinweis, dass er neu laden soll.
    """

    def __init__(self) -> None:
        self._topics: Dict[str, Set[asyncio.Queue]] = {}

    def subscribe(self, topic: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._topics.setdefault(topic, set()).add(queue)
        return queue

    def unsubscribe(self, topic: str, queue: asyncio.Queue) -> None:
        subscribers = self._topics.get(topic)
        if not subscribers:
            return
        subscribers.discard(queue)
        if not subscribers:
            self._topics.pop(topic, None)

    def subscriber_count(self, topic: str) -> int:
        return len(self._topics.get(topic, ()))

    def publish(self, topic: str, message: Any) -> None:
        for queue in list(self._topics.get(topic, ())):
            self._offer(queue, message)

    @staticmethod
    def _offer(queue: asyncio.Queue, message: Any) -> None:
        try:
            queue.put_nowait(message)
            return
        except asyncio.QueueFull:
            pass
        # Platz schaffen und einen Overflow-Hinweis hinterlassen
        dropped = 0
        while dropped < QUEUE_SIZE // 2:
            try:
                queue.get_nowait()
                dropped += 1
            except asyncio.QueueEmpty:
                break
        for payload in ({"type": "overflow", "dropped": dropped}, message):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                return

    def close_topic(self, topic: str) -> None:
        for queue in list(self._topics.get(topic, ())):
            self._offer(queue, {"type": "closed"})
