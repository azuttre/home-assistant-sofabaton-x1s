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
    "SnapshotIncompleteError",
    "SnapshotOutdatedError",
    "StateDocumentError",
    "HubRejectedError",
    "IrLearnError",
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


class SnapshotIncompleteError(ValueError):
    """A sync was asked to use a baseline entity that is not editable.

    The entity was never fetched, or its last fetch was incomplete, so the
    engine's stale preflight would have nothing to compare against.
    Refresh the entity (:meth:`AsyncXProxy.refresh`) and edit again.
    """


class SnapshotOutdatedError(ValueError):
    """A sync carried a ``snapshot_id`` that is not the current projection.

    The edit was made on an older snapshot. Take a new
    :meth:`AsyncXProxy.snapshot`, re-apply the edit and sync again. The
    check is cheap (no hub traffic); the engine's stale preflight still
    runs afterwards as the authoritative check against the hub.
    """


class StateDocumentError(ValueError):
    """:meth:`AsyncXProxy.import_state` was given a document it cannot read."""


class HubRejectedError(RuntimeError):
    """A write reached the hub but was refused, not acknowledged, or timed out.

    The hub held the session and the request was valid; the engine's log
    carries the step that failed. Retrying is safe for idempotent writes
    (rename, reorder, sync); check the snapshot first for the others.
    """


class IrLearnError(RuntimeError):
    """:meth:`AsyncXProxy.learn_ir` ended without a capture.

    ``state`` is ``"timed_out"`` (nothing was received within the
    window), ``"interrupted"`` (other hub traffic knocked the hub out of
    learn mode), ``"cancelled"`` (:meth:`AsyncXProxy.cancel_learn`), or
    ``"undecodable"`` (a capture arrived but no payload could be
    extracted).
    """

    def __init__(self, state: str, message: str | None = None) -> None:
        super().__init__(message or f"IR learn ended: {state}")
        self.state = state
