#!/usr/bin/env python3
"""Consume the hub's event stream as one typed sequence.

``proxy.events()`` folds every engine listener into ``HubEvent`` items
(``seq``, ``kind``, typed ``payload``), which is the shape a WebSocket
relay or a message bus wants: one handler, one schema. The kinds:

  activity_changed       ActivityChanged(activity_id, previous_activity_id, name)
  activity_list_updated  no payload (re-read activities())
  hub_state / app_state  ConnectionState(connected)
  status_changed         StatusChanged(mode, previous_mode)   derived, once per flip
  catalog_ready          CatalogReady(ready)                  the initial sync finished
  snapshot_changed       SnapshotChanged(...)                cached configuration changed
  ota                    no payload

Each consumer owns a bounded queue; falling behind drops the oldest
events (counted in ``proxy.events_dropped``, visible as a gap in ``seq``)
rather than stalling the engine. This runs in observe mode too: attach
the official app through the proxy and watch its activity switches land
here.
"""

import asyncio

from sofabaton import AsyncXProxy, HubConfig, async_discover_hubs


async def main() -> None:
    hubs = await async_discover_hubs(timeout=5.0)
    if not hubs:
        raise SystemExit("no hub found")
    cfg = HubConfig.from_discovered(hubs[0])
    proxy = AsyncXProxy.from_config(cfg)

    async with proxy:
        if await proxy.wait_connected(timeout=30):
            await proxy.wait_until_discoverable(timeout=5.0)   # let the app attach
        print("streaming events; Ctrl+C to stop")
        async for event in proxy.events():
            payload = event.to_dict()["payload"]
            print(f"#{event.seq:<4} {event.kind:<22} {payload if payload is not None else ''}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
