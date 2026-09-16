#!/usr/bin/env python3
"""Scan the LAN for Sofabaton hubs.

Fully close the official Sofabaton app before scanning. A hub connected
directly to the app does not advertise and cannot be discovered.

Equivalent to ``sofabaton discover``. Proxy advertisements (our own
mDNS announcements, marked with the PROXY_TXT_KEY TXT record) are
filtered out by default so you only see physical hubs.
"""

import asyncio

from sofabaton import async_discover_hubs


async def main() -> None:
    hubs = await async_discover_hubs(timeout=5.0)
    if not hubs:
        print("no hubs found; fully close the official Sofabaton app, then scan again")
    for hub in hubs:
        print(
            f"{hub.name}: {hub.hub_version or 'unknown variant'} at "
            f"{hub.host}:{hub.port} (mac={hub.mac}, txt={hub.txt})"
        )

    # Continuous browsing instead of a one-shot scan:
    #
    # from sofabaton import AsyncHubBrowser
    #
    # async def on_added(hub):
    #     print("found", hub.name)
    #
    # async with AsyncHubBrowser(on_added=on_added,
    #                            on_removed=lambda h: print("lost", h.name)):
    #     await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())
