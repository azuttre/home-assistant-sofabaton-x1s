# errors.py: the facade's typed failures.
#
# The library's contract is that it raises stdlib exceptions. These are
# stdlib *subclasses*: every existing ``except RuntimeError`` /
# ``except TimeoutError`` keeps working, while a consumer that needs to
# dispatch (a REST layer mapping failures to responses) can catch the
# specific class instead of matching message text.
#
# Only the asyncio facade (aio.py) raises these; the engine below it is
# unchanged.
from __future__ import annotations

__all__ = [
    "HubNotConnectedError",
    "HubBusyError",
    "FetchTimeoutError",
]


class HubNotConnectedError(RuntimeError):
    """A read needed a hub fetch, but the hub is not connected.

    Wait for :meth:`AsyncXProxy.wait_connected` (observe mode) or
    :meth:`AsyncXProxy.wait_until_controllable` (control mode) first.
    """


class HubBusyError(RuntimeError):
    """A read needed a hub fetch, but an app client holds the hub.

    The proxy is in observe mode: cached data is still served, fresh
    fetches are refused until the app disconnects
    (:meth:`AsyncXProxy.wait_until_controllable`).
    """


class FetchTimeoutError(TimeoutError):
    """A hub fetch was issued but its reply burst never landed."""
