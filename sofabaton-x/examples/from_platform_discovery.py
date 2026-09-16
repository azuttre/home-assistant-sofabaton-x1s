#!/usr/bin/env python3
"""Accept a hub that a platform's OWN mDNS stack discovered.

Automation platforms usually run their own discovery. When one of them
finds a Sofabaton hub, it hands over the raw advertisement parts, which
``HubConfig.from_advertisement`` turns into the same record the library's
own discovery produces. The record round-trips through a plain dict, so
this is also what an "add hub" REST body looks like.

Two things worth knowing when the feed is foreign:

* ``properties`` may be bytes or str keyed (or contain None values);
  both are accepted, matching what mDNS libraries hand out.
* A foreign stack will also see the proxy's OWN advertisements, which
  mimic hubs by design. Those records come back with ``is_proxy=True``:
  map them to the hub you already front instead of proxying a proxy.
"""

import asyncio

from sofabaton import AsyncXProxy, HubConfig

# What a platform's discovery might report for one service instance.
FOREIGN_RECORD = {
    "service_type": "_x1hub._udp.local.",
    "instance_name": "SOFABATON._x1hub._udp.local.",
    "host": "192.168.1.50",
    "port": 8102,
    "properties": {b"HVER": b"2", b"NAME": b"Living Room", b"MAC": b"AA:BB:CC:DD:EE:FF"},
}


async def main() -> None:
    cfg = HubConfig.from_advertisement(
        FOREIGN_RECORD["service_type"],
        FOREIGN_RECORD["instance_name"],
        host=FOREIGN_RECORD["host"],
        port=FOREIGN_RECORD["port"],
        properties=FOREIGN_RECORD["properties"],
        source="client",
    )
    if cfg.is_proxy:
        raise SystemExit("that advertisement is one of our own proxies; map it to the hub it fronts")

    print("record:", cfg.to_dict())          # store this, or send it as JSON
    cfg = HubConfig.from_dict(cfg.to_dict())  # ... and rebuild it later

    proxy = AsyncXProxy.from_config(cfg)
    async with proxy:
        if not await proxy.wait_until_ready(timeout=30):
            st = await proxy.status()
            raise SystemExit(f"hub not ready (mode {st.mode})")
        info = await proxy.hub_info()
        print(f"connected to {info.name!r} ({info.model}, fw {info.firmware_version})")
        for act in await proxy.activities():
            print(f"  activity {act.activity_id}: {act.name}")


if __name__ == "__main__":
    asyncio.run(main())
