#!/usr/bin/env python3
"""Read per-entity detail: commands, macros and favorites.

The top-level catalogs (``activities`` / ``devices``) only list
entities; the interesting data hangs off each one:

  * devices    -> commands  (the IR/BT/WiFi codes you can send)
  * activities -> macros     (multi-step sequences)
  * activities -> favorites  (the quick-access ordering)

This is a *control-mode* example: it needs to own the hub (no official
app connected through the proxy), so it waits for
``wait_until_controllable()`` before reading. Each read returns its data
directly — the first read of a cold entity fetches from the hub and
awaits the reply.
"""

import asyncio

from sofabaton import AsyncXProxy, async_discover_hubs


async def main() -> None:
    hubs = await async_discover_hubs(timeout=5.0)
    if not hubs:
        raise SystemExit("no hub found")
    hub = hubs[0]
    print(f"reading {hub.name} ({hub.hub_version}) at {hub.host}\n")

    proxy = AsyncXProxy(hub_ip=hub.host)   # the hub's IP is all you need

    async with proxy:
        # Own the hub before reading (no app attached, hub connected).
        if not await proxy.wait_until_controllable(timeout=30):
            raise SystemExit("hub not controllable (not connected, or an app is attached)")

        # Every browse yields the (entity_id, command_id) you send with
        # proxy.send(entity_id, command_id).

        # --- commands on each device -------------------------------------
        print("== DEVICES ==")
        for dev in await proxy.devices():             # list[Device], sorted by id
            commands = await proxy.commands(dev.device_id)   # list[Command]
            print(f"[{dev.device_id}] {dev.name or '?'}: {len(commands)} commands")
            for cmd in commands:
                print(f"    send({dev.device_id}, {cmd.command_id})  {cmd.label}")

        # --- macros and favorites on each activity -----------------------
        print("\n== ACTIVITIES ==")
        for act in await proxy.activities():          # list[Activity], sorted by id
            print(f"[{act.activity_id}] {act.name or '?'}")

            for macro in await proxy.macros(act.activity_id):   # list[Macro]
                print(f"    macro: send({act.activity_id}, {macro.command_id})  {macro.label}")

            # favorites -> list[Favorite]; each is a device command you
            # fire with send(device_id, command_id).
            for fav in await proxy.favorites(act.activity_id):
                print(
                    f"    favorite: send({fav.device_id}, {fav.command_id})"
                    f"  {fav.label}"
                )


if __name__ == "__main__":
    asyncio.run(main())
