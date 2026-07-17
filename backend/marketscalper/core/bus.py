"""In-process asyncio EventBus (roadmap P0.9; Architecture §1).

Type-keyed publish/subscribe — the event's dataclass type is the routing
key; no string topics, no envelopes. publish() awaits handlers
sequentially, in subscription order: deterministic delivery, required for
bit-identical replay (Architecture §10).

The full API is subscribe() and publish(). Nothing more.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

Handler = Callable[[Any], Awaitable[None]]


class EventBus:
    """Minimal deterministic pub/sub for the single-process pipeline."""

    def __init__(self) -> None:
        self._handlers: dict[type, list[Handler]] = {}

    def subscribe(self, event_type: type, handler: Handler) -> None:
        """Register an async handler for events of exactly `event_type`."""
        self._handlers.setdefault(event_type, []).append(handler)

    async def publish(self, event: Any) -> None:
        """Deliver `event` to its type's handlers, awaited one by one in
        subscription order. No handlers registered -> no-op."""
        for handler in self._handlers.get(type(event), ()):
            await handler(event)
