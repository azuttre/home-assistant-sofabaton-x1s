"""``/api/v1/events``: one WebSocket stream for every hub (plan section 7, S3).

Message shapes (JSON objects, ``type`` discriminates):

* ``hello``        sent once on connect: server identity and the hub list
* ``hub_event``    a relayed library ``HubEvent`` with its ``hub_id``
* ``server_event`` the server's own lifecycle: hub added / removed /
                   enabled / disabled / rekeyed (``kind``) for ``hub_id``
* ``dropped``      this client fell behind and ``count`` older messages
                   were discarded (sent before the next message that
                   does get through)

Each client owns a bounded queue; a slow consumer loses the oldest
messages rather than stalling the relay or the hub proxies. The
library's per-hub ``seq`` is passed through untouched; the server adds
no counter of its own. ``?hub_id=`` (repeatable) narrows the stream to
those hubs. Inbound text is read and ignored; closing the socket ends
the subscription. The protocol-level ping is uvicorn's.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Optional

from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect

from sofabaton import HubEvent

from . import API_PREFIX, API_VERSION, __version__
from .manager import HubManager

log = logging.getLogger(__name__)

DEFAULT_QUEUE_SIZE = 256


# -- message types (also published as OpenAPI components) -----------------------


@dataclass(frozen=True)
class WsHubSummary:
    hub_id: str
    enabled: bool


@dataclass(frozen=True)
class WsHello:
    server_version: str
    api_version: str
    hubs: list[WsHubSummary]
    type: Literal["hello"] = "hello"


@dataclass(frozen=True)
class WsHubEvent:
    hub_id: str
    event: HubEvent
    type: Literal["hub_event"] = "hub_event"


@dataclass(frozen=True)
class WsServerEvent:
    hub_id: str
    kind: str
    type: Literal["server_event"] = "server_event"


@dataclass(frozen=True)
class WsDropped:
    count: int
    type: Literal["dropped"] = "dropped"


WS_MESSAGE_TYPES = (WsHello, WsHubEvent, WsServerEvent, WsDropped)


def _to_json(message: Any) -> dict[str, Any]:
    if isinstance(message, WsHubEvent):
        return {"type": message.type, "hub_id": message.hub_id, "event": message.event.to_dict()}
    return asdict(message)


# -- relay ------------------------------------------------------------------------


class Subscription:
    def __init__(self, maxsize: int, hub_ids: Optional[set[str]]) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.hub_ids = hub_ids
        self.dropped = 0

    def wants(self, hub_id: str) -> bool:
        return self.hub_ids is None or hub_id in self.hub_ids

    def offer(self, message: Any) -> None:
        if self.queue.full():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self.dropped += 1
        self.queue.put_nowait(message)


class EventRelay:
    """Fans the manager's hub and server events out to WebSocket clients."""

    def __init__(self, manager: HubManager, *, maxsize: int = DEFAULT_QUEUE_SIZE) -> None:
        self._manager = manager
        self.maxsize = maxsize
        self._subs: set[Subscription] = set()
        manager.on_hub_event(self._on_hub_event)
        manager.on_server_event(self._on_server_event)

    @property
    def subscribers(self) -> int:
        return len(self._subs)

    def subscribe(self, hub_ids: Optional[set[str]] = None) -> Subscription:
        sub = Subscription(self.maxsize, hub_ids)
        self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        self._subs.discard(sub)

    def _on_hub_event(self, hub_id: str, event: HubEvent) -> None:
        self._broadcast(hub_id, WsHubEvent(hub_id=hub_id, event=event))

    def _on_server_event(self, hub_id: str, kind: str) -> None:
        self._broadcast(hub_id, WsServerEvent(hub_id=hub_id, kind=kind))

    def _broadcast(self, hub_id: str, message: Any) -> None:
        for sub in list(self._subs):
            if sub.wants(hub_id):
                sub.offer(message)

    def hello(self) -> WsHello:
        return WsHello(
            server_version=__version__,
            api_version=API_VERSION,
            hubs=[WsHubSummary(hub_id=r.hub_id, enabled=r.enabled)
                  for r in (self._manager.record(h) for h in self._manager.ids())],
        )


# -- route ----------------------------------------------------------------------------

router = APIRouter(tags=["events"])


@router.websocket(f"{API_PREFIX}/events")
async def events_socket(
    websocket: WebSocket,
    hub_id: Optional[list[str]] = Query(None, description="only these hubs (repeatable)"),
) -> None:
    relay: EventRelay = websocket.app.state.event_relay
    await websocket.accept()
    sub = relay.subscribe(set(hub_id) if hub_id else None)
    client = websocket.client.host if websocket.client else "?"
    log.info("events: client %s subscribed (filter=%s)", client, sorted(sub.hub_ids) if sub.hub_ids else "all")

    async def pump() -> None:
        while True:
            message = await sub.queue.get()
            if sub.dropped:
                count, sub.dropped = sub.dropped, 0
                await websocket.send_json(_to_json(WsDropped(count=count)))
            await websocket.send_json(_to_json(message))

    await websocket.send_json(_to_json(relay.hello()))
    sender = asyncio.create_task(pump())
    try:
        while True:
            await websocket.receive_text()          # inbound is ignored in v1
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        log.debug("events: client %s connection error", client, exc_info=True)
    finally:
        sender.cancel()
        try:
            await sender
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        relay.unsubscribe(sub)
        log.info("events: client %s gone", client)
