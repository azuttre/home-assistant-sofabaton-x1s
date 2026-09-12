#!/usr/bin/env python3
"""Build an HTTP callback listener ON TOP of sofabaton.

Executing HTTP callbacks is deliberately out of scope for the library:
it only carries the protocol side. This sketch shows the pattern the
Home Assistant integration implements with its Roku-style listener.

1. Use ``proxy.deploy_wifi_device`` with a ``WifiDeviceSpec`` containing
   the labels in COMMANDS below, host set to this machine's LAN address,
   and port set to LISTEN_PORT (see the README's managed wifi example).
   Bind the returned device's commands to the remote. Deployment writes
   paths of this form; this listener itself does not provision anything::

       POST /launch/<hub action id>/<device id>/<zero-based slot>/<short|long>
       POST /launch/e26a44861b45/12/0/short

2. Run a tiny HTTP server that parses that path: when the user presses
   the button on the remote, the hub fires the request and your handler
   turns it into an action.

The proxy is only needed here to observe the hub (mode, activity
changes); the callbacks travel hub -> this server directly. The stdlib
HTTP server is blocking, so it runs in the loop's executor.
The action id is normally the hub's MAC, not a decimal number. Hook slots
in WifiDeviceSpec are 1..10; callback path indexes are 0..9. This sketch
prints requests; a real action handler should match the source hub and
the saved deployment before dispatching actions.
"""

import asyncio
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sofabaton import AsyncXProxy, async_discover_hubs

LISTEN_PORT = 8060

# What each command index means on the device provisioned in step 1.
COMMANDS = ["Lights On", "Lights Off"]

_LAUNCH_RE = re.compile(r"^/launch/([^/]+)/(\d+)/([0-9])/(short|long)$")


class HubCallbackHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - stdlib API
        match = _LAUNCH_RE.fullmatch(self.path.split("?", 1)[0])
        if match:
            action_id, device_id, command_index, press_type = match.groups()
            slot = int(command_index)
            name = COMMANDS[slot] if slot < len(COMMANDS) else f"slot {slot}"
            print(f"remote pressed: {name} ({press_type}) [hub {action_id}, device {device_id}]")
            # ... do something real here: toggle lights, call an API, ...
        else:
            print(f"unrecognized callback path: {self.path}")
        self.send_response(200 if match else 404)
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
            server.server_close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
