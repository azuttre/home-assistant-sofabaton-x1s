#!/usr/bin/env python3
"""Build an HTTP callback listener ON TOP of sofabaton.

Executing HTTP callbacks is deliberately out of scope for the library:
it only carries the protocol side. This sketch shows the pattern the
Home Assistant integration implements with its Roku-style listener.

1. Provision a network device whose commands POST to this machine. The
   library does that through the restore path (see
   ``restore_ip_device.py``: a hand-built bundle whose command rows carry
   host, port, method and path; the hub assigns the device id). Point
   every command at ``<this machine>:<LISTEN_PORT>`` with a path like::

       POST /launch/<action_id>/<device_id>/<command_index>/<press_type>

2. Run a tiny HTTP server that parses that path: when the user presses
   the button on the remote, the hub fires the request and your handler
   turns it into an action.

The proxy is only needed here to observe the hub (mode, activity
changes); the callbacks travel hub -> this server directly. The stdlib
HTTP server is blocking, so it runs in the loop's executor.
"""

import asyncio
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sofabaton import AsyncXProxy, async_discover_hubs

LISTEN_PORT = 8060

# What each command index means on the device provisioned in step 1.
COMMANDS = ["Lights On", "Lights Off"]

_LAUNCH_RE = re.compile(r"^/launch/(\d+)/(\d+)/(\d+)/(short|long)$")


class HubCallbackHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - stdlib API
        match = _LAUNCH_RE.match(self.path)
        if match:
            action_id, device_id, command_index, press_type = match.groups()
            slot = int(command_index)
            name = COMMANDS[slot] if slot < len(COMMANDS) else f"slot {slot}"
            print(f"remote pressed: {name} ({press_type}) [device {device_id}]")
            # ... do something real here: toggle lights, call an API, ...
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):  # quiet the default request logging
        pass


async def main() -> None:
    loop = asyncio.get_running_loop()
    hubs = await async_discover_hubs(timeout=5.0)
    if not hubs:
        raise SystemExit("no hub found")
    hub = hubs[0]

    proxy = AsyncXProxy(hub_ip=hub.host)
    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), HubCallbackHandler)
    async with proxy:
        if await proxy.wait_connected(timeout=30):
            st = await proxy.status()
            print(f"hub connected ({st.mode} mode)")
        print(f"listening for hub callbacks on :{LISTEN_PORT}; Ctrl+C to stop")
        try:
            # serve_forever() blocks, so run it in the executor; shut it
            # down (from this thread) on cancellation / Ctrl+C.
            await loop.run_in_executor(None, server.serve_forever)
        finally:
            server.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
