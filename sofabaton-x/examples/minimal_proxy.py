#!/usr/bin/env python3
"""Connect to a hub and list its catalog; optional activity control is commented out.

The proxy advertises itself via mDNS exactly like the hub it fronts, so
the official Sofabaton app keeps working — pointed at the proxy — while
this process observes and injects traffic.

This is a *control-mode* example: the interesting operations (fresh
reads, sending commands, switching activities) require the proxy to own
the hub, which means the hub is connected AND no official app is
attached. ``wait_until_controllable()`` blocks until that holds. To
instead *watch* a live session while the app is connected, see
``watch.py``.

Uses the asyncio facade (``AsyncXProxy``): blocking calls run in the
loop's executor and callbacks are delivered on the loop.
"""

import asyncio

from sofabaton import AsyncXProxy, HubConfig, async_discover_hubs


async def main() -> None:
    hubs = await async_discover_hubs(timeout=5.0)
    if not hubs:
        raise SystemExit("no hub found; edit this example to use HubConfig(host='192.168.1.50')")
    hub = hubs[0]
    print(f"proxying {hub.name} ({hub.hub_version}) at {hub.host}")

    # Preserve the discovered identity so the app can find this hub's proxy.
    proxy = AsyncXProxy.from_config(HubConfig.from_discovered(hub))

    proxy.on_hub_state_change(lambda up: print("hub:", "up" if up else "down"))
    proxy.on_client_state_change(lambda up: print("app:", "connected" if up else "gone"))

    async with proxy:
        if not await proxy.wait_until_controllable(timeout=30):
            raise SystemExit("hub not controllable (not connected, or an app is attached)")

        # Ensure mDNS is advertised using the hub's confirmed banner identity.
        if not await proxy.wait_until_discoverable(timeout=5):
            print("Proxy is not yet discoverable by the official app")

        activities = await proxy.activities()
        print("activities:", {a.activity_id: a.name for a in activities})

        devices = await proxy.devices()
        print("devices:", {d.device_id: d.name for d in devices})

        # Switching an activity powers real equipment on and off, so it is
        # not run automatically. Pick an activity id from the listing
        # printed above and uncomment. Returns False if refused. Other
        # control verbs: proxy.press(ent, button), proxy.stop_activity(act),
        # proxy.find_remote().
        #
        # if await proxy.start_activity(101):
        #     print("started activity 101")


if __name__ == "__main__":
    asyncio.run(main())
