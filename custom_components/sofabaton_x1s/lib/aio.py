# aio.py — asyncio facade over the threaded proxy core.
#
# The engine stays thread-based (sockets, ack waiters, mDNS); this module
# owns NO protocol logic. It does exactly two things:
#
#   * runs blocking proxy calls in the event loop's default executor, and
#   * marshals listener callbacks from engine threads onto the loop
#     (plain callables via ``call_soon_threadsafe``, coroutine functions
#     via ``run_coroutine_threadsafe``),
#
# mirroring the executor-job pattern the Home Assistant integration uses
# around ``X1Proxy`` today.
from __future__ import annotations

import asyncio
import functools
import inspect
from typing import Any, AsyncIterator, Callable, Iterable, Optional

from .config import HubConfig
from .discovery import (
    DEFAULT_DISCOVERY_TIMEOUT,
    DiscoveredHub,
    HubBrowser,
    discover_hubs,
)
from .errors import FetchTimeoutError, HubBusyError, HubNotConnectedError
from .hub_listener import release_hub_from_listener
from .hub_versions import HVER_BY_HUB_VERSION
from .devices import parse_device_record
from .models import (
    Activity,
    ActivityChanged,
    Button,
    CatalogReady,
    Command,
    ConnectionState,
    Device,
    Favorite,
    HubEvent,
    HubInfo,
    HubStatus,
    Macro,
    RunningActivity,
    StatusChanged,
)
from .protocol_const import BUTTONNAME_BY_CODE, ButtonName
from .x1_proxy import X1Proxy

__all__ = [
    "AsyncXProxy",
    "AsyncHubBrowser",
    "async_discover_hubs",
]

# Default deadline for an awaited read that has to fetch from the hub.
DEFAULT_FETCH_TIMEOUT = 10.0


def _marshal_callback(loop: asyncio.AbstractEventLoop, callback: Callable) -> Callable:
    """Wrap ``callback`` so engine-thread invocations land on ``loop``.

    Sync callables are queued with ``call_soon_threadsafe``; coroutine
    functions are scheduled as tasks via ``run_coroutine_threadsafe``.
    """

    if inspect.iscoroutinefunction(callback):

        def relay(*args: Any, **kwargs: Any) -> None:
            asyncio.run_coroutine_threadsafe(callback(*args, **kwargs), loop)

    else:

        def relay(*args: Any, **kwargs: Any) -> None:
            loop.call_soon_threadsafe(functools.partial(callback, *args, **kwargs))

    functools.update_wrapper(relay, callback)
    return relay


def _activity_from_row(act_id: int, row: dict) -> Activity:
    return Activity(
        activity_id=int(act_id),
        name=str(row.get("name") or ""),
        active=bool(row.get("active", False)),
        needs_confirm=bool(row.get("needs_confirm", False)),
    )


def _device_from_row(dev_id: int, row: dict, hub_version: Optional[str]) -> Device:
    power_state: Optional[int] = None
    raw_body = row.get("raw_body")
    if hub_version and isinstance(raw_body, (bytes, bytearray)) and raw_body:
        try:
            power_state = int(parse_device_record(bytes(raw_body), hub_version=hub_version).power_state) & 0xFF
        except ValueError:
            power_state = None
    idle = row.get("idle_behavior")
    code = row.get("device_class_code")
    return Device(
        device_id=int(dev_id),
        name=str(row.get("name") or ""),
        brand=row.get("brand") or None,
        device_class=row.get("device_class"),
        device_class_code=int(code) if isinstance(code, int) else None,
        power_state=power_state,
        idle_behavior=int(idle) if isinstance(idle, int) else None,
    )


class AsyncXProxy:
    """Asyncio proxy for a Sofabaton X1/X1S/X2 hub — the library's entry point.

    Construct it with the hub's IP — ``AsyncXProxy(hub_ip=...)`` is enough:
    ports default to the right values and the hub model is confirmed from
    the connect banner. Pass ``mdns_instance=`` / ``mdns_txt=`` only to make
    the proxy advertise itself exactly like the hub it fronts (so the
    official app keeps working pointed at the proxy); ``hub_version=`` is at
    most a pre-connect hint. Construction must happen inside a running event
    loop (or pass ``loop=``). Blocking work runs in the loop's executor and
    listener callbacks are marshaled back onto the loop.

    The common surface is a small set of explicit, human-readable
    coroutines:

    * **read** — :meth:`activities`, :meth:`devices`, :meth:`commands`,
      :meth:`buttons`, :meth:`macros`, :meth:`favorites`. These return
      the data directly (no ``(data, ready)`` tuple): cached results
      come back immediately, otherwise the call fetches from the hub and
      awaits completion, raising :class:`HubBusyError` when the hub is
      held by a connected app client and nothing is cached,
      :class:`HubNotConnectedError` when there is no hub session, or
      :class:`FetchTimeoutError` when the fetch never lands (all stdlib
      subclasses: ``RuntimeError`` / ``TimeoutError``).
    * **status** — :meth:`status` (live connection state and mode, no
      hub traffic) and :meth:`hub_info` (identity from the connect
      banner), both typed dataclasses with ``to_dict()``.
    * **ready** — :meth:`wait_until_ready`: the connect-time initial
      sync (banner, devices, activities) has cached the catalog minimum
      for this hub session; ``HubStatus.catalog_ready`` mirrors it.
    * **events** — :meth:`events`: one async iterator of typed
      :class:`HubEvent` items folding every engine listener (activity
      change, catalog update, hub and app link state, OTA) plus a
      derived ``status_changed`` when the mode flips.
    * **control** — :meth:`press`, :meth:`start_activity`,
      :meth:`stop_activity`, :meth:`find_remote`.
    * **live edit** — :meth:`sync_activity`, :meth:`sync_device`: diff a
      captured backup bundle against an edited copy and write the
      difference in place (see the "live edit surface" section below).

    Anything else in :data:`PROXY_METHODS` (provisioning, cache export,
    explicit requests) is awaitable too and delegates to the engine in
    the executor. Listener registration (``on_*``) accepts plain
    callables and coroutine functions and always delivers on the event
    loop. ``.sync`` exposes the underlying engine for the raw surface
    (including the ``get_*`` snapshot getters that return tuples).
    """

    # Engine methods exposed as bare awaitable executor delegates. The
    # human read/control surface (activities/devices/commands/buttons/
    # macros/favorites/press/start_activity/stop_activity) is defined as
    # explicit methods below and intentionally NOT listed here. Tests
    # assert every entry exists on X1Proxy so the list cannot drift.
    PROXY_METHODS: frozenset[str] = frozenset(
        {
            # advanced getters (already return plain data, not tuples)
            "get_cached_macro_records",
            "get_cached_activity_detail_ids",
            "get_known_device_ids",
            "get_known_activity_ids",
            "get_app_activations",
            # live in-memory cache invalidation (NOT persistence: the
            # library never writes to disk, and the cache-snapshot
            # (de)serializers stay off the public surface — reach them
            # via .sync if a warm-start dump is genuinely needed).
            "clear_entity_cache",
            "clear_devices_catalog",
            "clear_activities_catalog",
            # explicit hub requests
            "request_activities",
            "request_devices",
            "request_activity_mapping",
            "request_ir_command_dump",
            "fetch_device_input_record",
            "fetch_device_key_sort",
            # actions
            "set_hub_name",
            "set_diag_dump",
            "resync_remote",
            "update_discovery_identity",
            "enable_proxy",
            "disable_proxy",
            # provisioning / mutation (whole-entity operations a bundle
            # diff cannot express; row-level edits go through sync_*)
            "delete_device",
            "reorder_activities",
            "create_activity",
            "play_ir_blob",
            "erase_configuration",
            # backup / restore (symmetric, schema-versioned)
            "backup_device",
            "backup_activity",
            "backup_hub_bundle",
            "restore_device",
            "restore_activity",
            "restore_hub_bundle",
        }
    )

    _LISTENER_METHODS: frozenset[str] = frozenset(
        {
            "on_activity_change",
            "on_activity_list_update",
            "on_client_state_change",
            "on_hub_state_change",
            "on_ota_update",
            "on_app_activation",
        }
    )

    # Engine methods the explicit coroutines above are built on (reads,
    # readiness, control, live edit, lifecycle). They are reachable only
    # through those coroutines, never by name. Listed so the triage guard
    # (see ``engine_method_triage``) can prove every public engine method
    # sits in exactly one tier.
    WRAPPED_ENGINE_METHODS: frozenset[str] = frozenset(
        {
            "get_activities",
            "get_devices",
            "get_commands_for_entity",
            "get_buttons_for_entity",
            "get_macros_for_activity",
            "ensure_commands_for_activity",
            "send_command",
            "can_issue_commands",
            "find_remote",
            "sync_activity",
            "sync_device",
            "start",
            "stop",
            "set_zeroconf",
            "on_burst_end",
            "has_banner_identity",
            "fetch_banner_info",
            "get_banner_info",
            "get_proxy_status",
        }
    )

    def __init__(
        self,
        *,
        hub_ip: str,
        hub_port: int = 8102,
        hub_listen_port: int = 8200,
        app_discovery_port: int = 8102,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        initial_sync: bool = True,
        **proxy_kwargs: Any,
    ) -> None:
        """Construct a proxy for the hub at ``hub_ip``.

        ``initial_sync`` (default on) makes the facade read the banner,
        devices and activities every time the hub connects, so the
        catalog minimum is always cached; see :meth:`wait_until_ready`.

        The proxy has two network faces. Only the four arguments below
        describe them; everything else (``mdns_instance``, ``mdns_txt``,
        ``hub_version``, ``proxy_enabled``, ``diag_*`` ...) is forwarded
        verbatim to the engine.

        Hub-facing (the physical hub):

        * ``hub_ip`` — the hub's IPv4 address (from discovery or manual).
        * ``hub_port`` — UDP port *on the hub* we send ``CALL_ME`` to.
          Protocol-fixed at ``8102``; you should rarely change it.
        * ``hub_listen_port`` — TCP port *on this host* the hub connects
          back to after ``CALL_ME``. Change it to avoid a local port
          collision; reserve it in your firewall for the hub's connect-back.

        App-facing (the official mobile app):

        * ``app_discovery_port`` — UDP port *on this host* the app uses to
          discover and call the proxy. Keep it at ``8102``: iOS discovery
          is lost on any other port.

        See the project's ``docs/networking.md`` for the full port map.
        """

        self._loop = loop or asyncio.get_running_loop()
        self._proxy = X1Proxy(
            real_hub_ip=hub_ip,
            real_hub_udp_port=hub_port,
            hub_listen_base=hub_listen_port,
            proxy_udp_port=app_discovery_port,
            **proxy_kwargs,
        )
        self._init_burst_state()
        if initial_sync:
            self._arm_initial_sync()

    @classmethod
    def from_config(
        cls,
        config: HubConfig,
        *,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        **overrides: Any,
    ) -> "AsyncXProxy":
        """Construct a proxy from a :class:`HubConfig` record.

        The record is the one shape every configuration path produces
        (library discovery, a foreign mDNS stack, manual entry, a REST
        body or config file); ``overrides`` are applied on top of the
        record's keyword arguments, e.g. ``diag_dump=False``.
        """

        kwargs = config.proxy_kwargs()
        kwargs.update(overrides)
        return cls(loop=loop, **kwargs)

    @classmethod
    def wrap(
        cls,
        proxy: X1Proxy,
        *,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        initial_sync: bool = False,
    ) -> "AsyncXProxy":
        """Wrap an already-constructed engine (e.g. mid-migration code).

        ``initial_sync`` defaults off here: an application that built the
        engine itself usually runs its own connect-time sync already.
        """

        self = object.__new__(cls)
        self._loop = loop or asyncio.get_running_loop()
        self._proxy = proxy
        self._init_burst_state()
        if initial_sync:
            self._arm_initial_sync()
        return self

    def _init_burst_state(self) -> None:
        # Per-entity futures awaiting a burst completion, keyed by the
        # engine's burst key (e.g. "commands:5", "activities"). One
        # persistent dispatcher is registered per burst-kind on first use
        # so awaited reads never leak listeners.
        self._burst_waiters: dict[str, list[asyncio.Future]] = {}
        self._burst_dispatch_kinds: set[str] = set()
        # Set on any hub/client connection-state change (lazily wired) so
        # the readiness waiters can wake.
        self._state_event: Optional[asyncio.Event] = None
        # events(): per-consumer queues fed by one set of engine listeners.
        self._event_queues: set[asyncio.Queue] = set()
        self._event_listeners_armed = False
        self._event_seq = 0
        self._last_mode: Optional[str] = None
        self.events_dropped = 0
        # Burst keys with a fetch in flight: a second read for the same
        # key joins the pending burst instead of issuing another request.
        self._inflight: set[str] = set()
        # Connect-time initial sync (banner, devices, activities).
        self._initial_sync_armed = False
        self._initial_sync_task: Optional[asyncio.Task] = None
        self._catalog_ready = False
        self._ready_event: Optional[asyncio.Event] = None
        # Bumped on every hub disconnect so a sync started for an earlier
        # session can never mark a later one ready.
        self._session_gen = 0

    # -- escape hatches ----------------------------------------------------

    @property
    def sync(self) -> X1Proxy:
        """The underlying threaded engine."""

        return self._proxy

    @property
    def state(self) -> Any:
        """The engine's :class:`ActivityCache` (read on the loop thread)."""

        return self._proxy.state

    async def run(self, func: Callable, /, *args: Any, **kwargs: Any) -> Any:
        """Run an arbitrary callable in the executor (escape hatch)."""

        return await self._loop.run_in_executor(
            None, functools.partial(func, *args, **kwargs)
        )

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        await self.run(self._proxy.start)

    async def stop(self, *, release_hub: bool = False) -> None:
        """Stop the engine; with ``release_hub`` also let the hub go.

        A hub that has just been dropped keeps dialling the shared
        connect-back port for as long as that port is open for other
        hubs, and while it dials it does not advertise itself, so the
        official app cannot find it. It only gives up on a refused
        connection. ``release_hub=True`` therefore releases this hub from
        the shared listener after the stop: the *listening* socket closes
        for a short window and reopens, and for a grace period every
        further dial-back from this hub closes it again, so one of the
        hub's attempts is guaranteed to meet a closed port. Accepted
        sessions are untouched, so every other hub stays connected
        straight through, and their own CALL_ME loops re-summon any that
        happened to be reconnecting inside a window. It is a no-op when
        no other hub is registered (the port is simply closed). Use it
        when a hub is disabled but stays configured; a plain ``stop()``
        is for shutdown.
        """

        await self.run(self._proxy.stop)
        if release_hub:
            await self.run(release_hub_from_listener, self._proxy.real_hub_ip)

    async def __aenter__(self) -> "AsyncXProxy":
        await self.start()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.stop()

    def set_zeroconf(self, zc: Any) -> None:
        """Adopt a shared Zeroconf instance (cheap; no executor needed)."""

        self._proxy.set_zeroconf(zc)

    # -- readiness -----------------------------------------------------------
    #
    # ``start()`` only spawns the transport thread; the hub TCP connect and
    # banner handshake happen asynchronously after it returns. The proxy
    # has two operating modes, and these waiters gate them:
    #
    #   * observe mode  — the official app is connected through the proxy;
    #     you can watch activity/state changes but cannot issue commands
    #     (the app owns the hub). Gate on :meth:`wait_connected`.
    #   * control mode  — no app attached; the proxy owns the hub, so reads
    #     fetch fresh and commands/backup work. Gate on
    #     :meth:`wait_until_controllable`.
    #
    # Orthogonal to the mode, :meth:`wait_until_discoverable` gates the
    # point at which the official app can *find* the proxy over mDNS — use
    # it when you want the app to attach (e.g. to observe a live session).

    def _ensure_state_watcher(self) -> None:
        if self._state_event is not None:
            return
        self._state_event = asyncio.Event()

        def _on_change(*_args: Any) -> None:
            # Fires on the engine thread; wake the loop.
            self._loop.call_soon_threadsafe(self._state_event.set)

        self._proxy.on_hub_state_change(_on_change)
        self._proxy.on_client_state_change(_on_change)

    async def _wait_for_state(
        self, predicate: Callable[[], bool], timeout: float
    ) -> bool:
        if predicate():
            return True
        self._ensure_state_watcher()
        assert self._state_event is not None
        deadline = self._loop.time() + timeout
        while not predicate():
            remaining = deadline - self._loop.time()
            if remaining <= 0:
                return predicate()
            self._state_event.clear()
            if predicate():
                return True
            try:
                await asyncio.wait_for(self._state_event.wait(), remaining)
            except TimeoutError:
                return predicate()
        return True

    async def wait_connected(self, timeout: float = 30.0) -> bool:
        """Wait until the hub is connected (observe mode can begin).

        Returns ``False`` on timeout.
        """

        return await self._wait_for_state(
            lambda: self._proxy.transport.is_hub_connected, timeout
        )

    async def wait_until_controllable(self, timeout: float = 30.0) -> bool:
        """Wait until the proxy owns the hub (connected, no app attached).

        Reads fetch fresh and commands/backup work once this returns
        ``True``; returns ``False`` on timeout.
        """

        return await self._wait_for_state(self._proxy.can_issue_commands, timeout)

    async def wait_until_discoverable(self, timeout: float = 30.0) -> bool:
        """Wait until the official app can find the proxy over mDNS.

        The app discovers the proxy the same way it discovers a real hub:
        by its mDNS advertisement. The proxy can only advertise once it
        knows which hub it is fronting (model and name), which it reads
        from the hub's connect banner. So this waits for the hub to
        connect, reads that banner, and brings the advertisement up
        aligned to it. Call it after entering the proxy to let the app
        attach (see ``watch``/``minimal_proxy`` examples).

        Returns ``True`` once the proxy is advertising, ``False`` on
        timeout (e.g. the hub never connected). If an app already holds
        the hub it drives the banner itself, so this resolves as soon as
        that identity is known.
        """

        deadline = self._loop.time() + timeout
        # Advertising needs the hub connected so we can read its banner.
        if not await self.wait_connected(timeout=max(0.0, deadline - self._loop.time())):
            return False

        while True:
            # In control mode nothing else asks the hub who it is, so do it
            # ourselves; while an app holds the hub it drives the banner and
            # we just wait for that identity to land.
            if self._proxy.can_issue_commands() and not self._proxy.has_banner_identity():
                await self.run(self._proxy.fetch_banner_info)

            if self._proxy.has_banner_identity():
                # Publish (or realign) the advertisement to the banner
                # identity — update_discovery_identity is what actually
                # starts mDNS once the hub is connected and identified.
                await self.update_discovery_identity(**self._discovery_identity_from_banner())
                return True

            if self._loop.time() >= deadline:
                return False
            await asyncio.sleep(0.1)

    def _discovery_identity_from_banner(self) -> dict[str, Any]:
        """Build the advertised identity from the hub's connect banner.

        The banner is authoritative for the hub's model (-> HVER) and
        name; fold those into the current TXT so the advertisement matches
        the hub the proxy is fronting. Pure in-memory reads, so no executor
        hop is needed.
        """

        info = self._proxy.get_banner_info()
        model = info.get("model") or self._proxy.hub_version
        txt = dict(self._proxy.mdns_txt)
        hver = HVER_BY_HUB_VERSION.get(model)
        if hver:
            txt["HVER"] = hver
        name = str(info.get("name") or "").strip()
        if name:
            txt["NAME"] = name
        return {"mdns_txt": txt, "hub_version": model}

    # -- listeners ------------------------------------------------------------

    def on_burst_end(self, key: str, callback: Callable) -> None:
        """Register a burst-end listener; delivered on the event loop."""

        self._proxy.on_burst_end(key, _marshal_callback(self._loop, callback))

    # -- read surface --------------------------------------------------------

    async def activities(
        self, *, timeout: float = DEFAULT_FETCH_TIMEOUT
    ) -> list[Activity]:
        """Return every activity in the hub's catalog, sorted by id."""

        # The catalog getters gate fetching on ``force_refresh``, not
        # ``fetch_if_missing`` (which the per-entity getters use).
        rows = await self._read(
            self._proxy.get_activities, "activities", timeout=timeout, fetch_kw="force_refresh"
        )
        return [_activity_from_row(act_id, row) for act_id, row in sorted(dict(rows).items())]

    async def devices(
        self, *, timeout: float = DEFAULT_FETCH_TIMEOUT
    ) -> list[Device]:
        """Return every device in the hub's catalog, sorted by id.

        ``Device.power_state`` is projected from the row's stored record
        as of the last devices fetch (see :class:`Device`).
        """

        await self._read(
            self._proxy.get_devices, "devices", timeout=timeout, fetch_kw="force_refresh"
        )
        # The getter returns the JSON export view, which strips the stored
        # record body on purpose; the power-state byte lives in that body,
        # so project from the engine's own state rows instead.
        rows = await self.run(lambda: dict(self._proxy.state.entities("device")))
        hub_version = self._proxy.hub_version
        return [
            _device_from_row(dev_id, row, hub_version)
            for dev_id, row in sorted(rows.items())
        ]

    async def commands(
        self, device_id: int, *, timeout: float = DEFAULT_FETCH_TIMEOUT
    ) -> list[Command]:
        """Return a device's commands, sorted by id.

        Send one with ``send(device_id, command_id)``.
        """

        cmds = await self._read(
            self._proxy.get_commands_for_entity,
            f"commands:{device_id & 0xFF}",
            device_id,
            timeout=timeout,
        )
        return [
            Command(command_id=int(cid), label=str(label))
            for cid, label in sorted(dict(cmds).items())
        ]

    async def buttons(
        self, entity_id: int, *, timeout: float = DEFAULT_FETCH_TIMEOUT
    ) -> list[Button]:
        """Return the buttons bound to an activity or device.

        Each :class:`Button` carries the code you can send to
        ``entity_id`` plus the underlying target device command it maps
        to (``device_id``/``command_id`` are ``None`` for unbound slots).
        """

        codes = await self._read(
            self._proxy.get_buttons_for_entity,
            f"buttons:{entity_id & 0xFF}",
            entity_id,
            timeout=timeout,
        )
        details = await self.run(
            lambda: dict(self._proxy.state.button_details.get(entity_id & 0xFF, {}))
        )
        out: list[Button] = []
        for code in codes:
            bound = details.get(code, {})
            out.append(
                Button(
                    button_code=int(code),
                    name=BUTTONNAME_BY_CODE.get(code),
                    device_id=bound.get("device_id"),
                    command_id=bound.get("command_id"),
                )
            )
        return out

    async def macros(
        self, activity_id: int, *, timeout: float = DEFAULT_FETCH_TIMEOUT
    ) -> list[Macro]:
        """Return an activity's macros.

        Send one with ``send(activity_id, command_id)``.
        """

        macros = await self._read(
            self._proxy.get_macros_for_activity,
            f"macros:{activity_id & 0xFF}",
            activity_id,
            timeout=timeout,
        )
        return [
            Macro(command_id=int(m.get("command_id")), label=m.get("label"))
            for m in macros
            if m.get("command_id") is not None
        ]

    async def favorites(
        self, activity_id: int, *, timeout: float = DEFAULT_FETCH_TIMEOUT
    ) -> list[Favorite]:
        """Return an activity's favorites.

        Each favorite is a device command; send one with
        ``send(device_id, command_id)``. Returns an empty list when the
        activity has no favorites.
        """

        # The favorite slots come from the activity keymap; fetching the
        # buttons populates them (best-effort: don't fail favorites if the
        # keymap can't be fetched).
        try:
            await self.buttons(activity_id, timeout=timeout)
        except (RuntimeError, TimeoutError):
            pass

        # ensure_commands_for_activity resolves each favorite's command
        # label, but the per-command fetches it kicks complete
        # asynchronously: poll until it reports ready (or timeout).
        deadline = self._loop.time() + timeout
        while True:
            _, ready = await self.run(
                self._proxy.ensure_commands_for_activity,
                activity_id,
                fetch_if_missing=True,
            )
            if ready or self._loop.time() >= deadline:
                break
            await asyncio.sleep(0.2)

        rich = await self.run(
            self._proxy.state.get_activity_favorite_labels, activity_id & 0xFF
        )
        return [
            Favorite(
                device_id=int(fav.get("device_id")),
                command_id=int(fav.get("command_id")),
                label=fav.get("name"),
            )
            for fav in rich
            if fav.get("device_id") is not None and fav.get("command_id") is not None
        ]

    async def current_activity(self) -> dict | None:
        """Return the activity currently running on the hub, or ``None`` when idle.

        ``{"activity_id": int, "name": str | None}`` — ``activity_id``
        matches the keys of :meth:`activities`. Tracked live from the hub's
        activity-state frames, so it needs no fetch and is available in both
        observe and control mode; transitions also fire
        :meth:`on_activity_change`.
        """

        def _read() -> dict | None:
            act = self._proxy.state.current_activity
            if act is None:
                return None
            act &= 0xFF
            return {
                "activity_id": act,
                "name": self._proxy.state.get_activity_name(act),
            }

        return await self.run(_read)

    # -- status surface ------------------------------------------------------

    async def status(self) -> HubStatus:
        """Return the live connection state of the proxied hub.

        Pure state read, no hub traffic, available in every mode. ``mode``
        is ``"disconnected"`` (no hub session), ``"observe"`` (an app
        client holds the hub through the proxy: reads serve cache, sends
        are refused) or ``"control"`` (the proxy owns the hub).
        """

        def _read() -> HubStatus:
            transport = self._proxy.transport
            hub_connected = bool(transport.is_hub_connected)
            app_connected = bool(transport.is_client_connected)
            controllable = bool(self._proxy.can_issue_commands())
            if controllable:
                mode = "control"
            elif hub_connected:
                mode = "observe"
            else:
                mode = "disconnected"
            # Counts come from the engine's state, never from the catalog
            # getters: those are fetch-if-missing and would enqueue a hub
            # request on a cold engine, which a status poll must not do.
            acts = self._proxy.state.entities("activity")
            devs = self._proxy.state.entities("device")
            act = self._proxy.state.current_activity
            running = None
            if act is not None:
                act &= 0xFF
                running = RunningActivity(
                    activity_id=act, name=self._proxy.state.get_activity_name(act)
                )
            return HubStatus(
                hub_connected=hub_connected,
                app_connected=app_connected,
                controllable=controllable,
                mode=mode,
                hub_version=self._proxy.hub_version,
                proxy_enabled=bool(self._proxy.get_proxy_status()),
                running_activity=running,
                activities_cached=len(acts or {}),
                devices_cached=len(devs or {}),
                catalog_ready=self._catalog_ready,
            )

        return await self.run(_read)

    async def hub_info(self, *, refresh: bool = False) -> HubInfo:
        """Return the hub's identity as read from its connect banner.

        Cached-else-fetch like the reads: the banner known from the
        session is returned directly; ``refresh=True`` (or an unknown
        banner) re-reads it from the hub, which needs control mode and
        raises :class:`HubBusyError` / :class:`HubNotConnectedError`
        otherwise. When nothing is known yet and no fetch is possible the
        result has ``known=False`` rather than raising, so a status page
        can render before the first banner lands.
        """

        def _from_banner(info: dict) -> HubInfo:
            if not info:
                return HubInfo(
                    known=False,
                    model=None,
                    name=None,
                    mac=None,
                    firmware_version=None,
                    production_batch=None,
                )
            return HubInfo(
                known=True,
                model=info.get("model"),
                name=info.get("name") or None,
                mac=info.get("mac"),
                firmware_version=info.get("firmware_version"),
                production_batch=info.get("production_batch"),
            )

        cached = await self.run(self._proxy.get_banner_info)
        if cached and not refresh:
            return _from_banner(cached)
        if not self._proxy.can_issue_commands():
            # An explicit refresh is refused with the typed reason; a plain
            # read degrades to whatever is known (possibly nothing).
            if refresh:
                self._raise_if_cannot_fetch("banner")
            return _from_banner(cached or {})
        await self.run(
            functools.partial(self._proxy.fetch_banner_info, force_refresh=True)
        )
        # Re-read the engine's cache rather than trusting the fetch's return
        # value: the banner lands through the frame handler and the getter
        # is the one place it is guaranteed to be.
        return _from_banner(await self.run(self._proxy.get_banner_info))

    # -- connect-time initial sync ------------------------------------------

    def _arm_initial_sync(self) -> None:
        """Run the catalog minimum fetch on every hub connect (once armed)."""

        if self._initial_sync_armed:
            return
        self._initial_sync_armed = True

        # The transition itself is carried to the loop, not re-derived
        # there: a drop and reconnect that both land before the loop runs
        # would otherwise look like "still connected" and let the previous
        # session's readiness survive into the new one.
        def _on_hub_link(connected: bool) -> None:
            self._loop.call_soon_threadsafe(self._on_hub_link_for_sync, bool(connected))

        def _on_app_link(_connected: bool) -> None:
            self._loop.call_soon_threadsafe(self._maybe_start_initial_sync)

        self._proxy.on_hub_state_change(_on_hub_link)
        self._proxy.on_client_state_change(_on_app_link)

    def _on_hub_link_for_sync(self, connected: bool) -> None:
        if not connected:
            # Session gone: what was cached is no longer known-good, a sync
            # parked on a fetch that can no longer land is abandoned, and
            # the generation moves on so its late completion is ignored.
            self._session_gen += 1
            self._set_catalog_ready(False)
            task = self._initial_sync_task
            if task is not None and not task.done():
                task.cancel()
            return
        self._maybe_start_initial_sync()

    def _maybe_start_initial_sync(self) -> None:
        if not self._proxy.transport.is_hub_connected:
            return
        if self._catalog_ready or not self._proxy.can_issue_commands():
            return
        if self._initial_sync_task is not None and not self._initial_sync_task.done():
            return
        self._initial_sync_task = self._loop.create_task(
            self._run_initial_sync(self._session_gen)
        )

    async def _run_initial_sync(self, session_gen: int) -> None:
        """Banner, devices, activities: the minimum every session caches.

        Runs in order, sharing the burst bridge with concurrent reads, and
        only counts a step as done when the reply actually landed (banner
        ready flag; catalog getter ready flag plus the burst commit flag,
        via ``_await_fetch``). Never raises: a failure (hub dropped
        mid-fetch, app attached, reply never landed) leaves
        ``catalog_ready`` False and the next link-state change tries
        again. A completion for an earlier session generation is ignored.
        """

        try:
            _info, banner_ready = await self.run(
                functools.partial(self._proxy.fetch_banner_info, force_refresh=True)
            )
            if not banner_ready:
                return
            await self._await_fetch(
                self._proxy.get_devices, "devices",
                timeout=DEFAULT_FETCH_TIMEOUT, fetch_kw="force_refresh",
            )
            await self._await_fetch(
                self._proxy.get_activities, "activities",
                timeout=DEFAULT_FETCH_TIMEOUT, fetch_kw="force_refresh",
            )
        except (RuntimeError, TimeoutError):
            return
        if session_gen == self._session_gen and self._proxy.transport.is_hub_connected:
            self._set_catalog_ready(True)

    def _set_catalog_ready(self, ready: bool) -> None:
        if ready == self._catalog_ready:
            return
        self._catalog_ready = ready
        if self._ready_event is None:
            self._ready_event = asyncio.Event()
        if ready:
            self._ready_event.set()
        else:
            self._ready_event.clear()
        self._dispatch_event("catalog_ready", CatalogReady(ready=ready))

    async def wait_until_ready(self, timeout: float = 30.0) -> bool:
        """Wait until the connect-time initial sync has cached the catalog.

        True once banner, devices and activities are known for the
        current hub session (``HubStatus.catalog_ready``); False on
        timeout, or immediately when the facade was constructed with
        ``initial_sync=False``.
        """

        if self._catalog_ready:
            return True
        if not self._initial_sync_armed:
            return False
        if self._ready_event is None:
            self._ready_event = asyncio.Event()
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout)
        except TimeoutError:
            return self._catalog_ready
        return self._catalog_ready

    # -- event stream --------------------------------------------------------

    def _current_mode(self) -> str:
        if self._proxy.can_issue_commands():
            return "control"
        if self._proxy.transport.is_hub_connected:
            return "observe"
        return "disconnected"

    def _ensure_event_listeners(self) -> None:
        """Register the engine listeners that feed :meth:`events` (once)."""

        if self._event_listeners_armed:
            return
        self._event_listeners_armed = True
        self._last_mode = self._current_mode()
        emit = self._emit_event_threadsafe

        def on_activity(new_id, old_id, name) -> None:
            emit(
                "activity_changed",
                ActivityChanged(
                    activity_id=None if new_id is None else int(new_id) & 0xFF,
                    previous_activity_id=None if old_id is None else int(old_id) & 0xFF,
                    name=name,
                ),
            )

        # These run on the engine thread, possibly INSIDE the transport's
        # own locks (its stop() notifies hub state while holding the socket
        # lock). Nothing here may call back into the transport, so the
        # mode is derived on the loop after the callback has returned.
        def on_hub_state(connected: bool) -> None:
            emit("hub_state", ConnectionState(connected=bool(connected)))
            self._loop.call_soon_threadsafe(self._emit_mode_change)

        def on_app_state(connected: bool) -> None:
            emit("app_state", ConnectionState(connected=bool(connected)))
            self._loop.call_soon_threadsafe(self._emit_mode_change)

        self._proxy.on_activity_change(on_activity)
        self._proxy.on_activity_list_update(lambda: emit("activity_list_updated", None))
        self._proxy.on_hub_state_change(on_hub_state)
        self._proxy.on_client_state_change(on_app_state)
        self._proxy.on_ota_update(lambda: emit("ota", None))

    def _emit_mode_change(self) -> None:
        # Loop thread only: reads the transport flags, which take the
        # transport's locks.
        mode = self._current_mode()
        previous = self._last_mode
        if mode == previous:
            return
        self._last_mode = mode
        self._dispatch_event(
            "status_changed", StatusChanged(mode=mode, previous_mode=previous or "disconnected")
        )

    def _emit_event_threadsafe(self, kind: str, payload: Any) -> None:
        # Engine thread -> loop. Sequence numbers are assigned on the loop
        # so they are strictly ordered as consumers observe them.
        self._loop.call_soon_threadsafe(self._dispatch_event, kind, payload)

    def _dispatch_event(self, kind: str, payload: Any) -> None:
        self._event_seq += 1
        event = HubEvent(seq=self._event_seq, kind=kind, payload=payload)
        for queue in list(self._event_queues):
            if queue.full():
                # Bounded, drop-oldest: a slow consumer loses history, never
                # stalls the engine. The gap shows up as a jump in ``seq``.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                self.events_dropped += 1
            queue.put_nowait(event)

    async def events(self, *, maxsize: int = 256) -> AsyncIterator[HubEvent]:
        """Iterate over hub events as they happen, as typed :class:`HubEvent`.

        Kinds: ``activity_changed`` (:class:`ActivityChanged`),
        ``activity_list_updated`` (no payload), ``hub_state`` and
        ``app_state`` (:class:`ConnectionState`), ``status_changed``
        (:class:`StatusChanged`, derived: fires once whenever the mode
        flips between disconnected / observe / control) and ``ota`` (no
        payload). Each consumer gets its own bounded queue; when it falls
        ``maxsize`` events behind the oldest are dropped, counted in
        ``events_dropped``, and visible as a gap in ``seq``. The
        ``on_*`` listener registrations keep working alongside.

        Usage::

            async for event in proxy.events():
                print(event.kind, event.to_dict()["payload"])
        """

        self._ensure_event_listeners()
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._event_queues.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._event_queues.discard(queue)

    # -- control surface -----------------------------------------------------

    async def send(self, entity_id: int, command_id: int) -> bool:
        """Send a command to a device or activity.

        ``command_id`` is the id from :meth:`commands`/:meth:`macros`/
        :meth:`favorites` (or a button code from :meth:`buttons`). Returns
        ``False`` if refused (a real app client holds the hub).
        """

        return await self.run(self._proxy.send_command, entity_id, command_id)

    # ``press`` is the remote-button-oriented alias of :meth:`send`.
    press = send

    async def start_activity(self, activity_id: int) -> bool:
        """Switch to an activity (sends its power-on)."""

        return await self.run(self._proxy.send_command, activity_id, ButtonName.POWER_ON)

    async def stop_activity(self, activity_id: int) -> bool:
        """Power off an activity."""

        return await self.run(self._proxy.send_command, activity_id, ButtonName.POWER_OFF)

    async def find_remote(self) -> bool:
        """Trigger the hub's find-my-remote signal.

        Returns ``False`` if refused (a real app client holds the hub).
        """

        return await self.run(self._proxy.find_remote)

    # -- live edit surface -----------------------------------------------------
    #
    # In-place editing of one activity or one device: capture a
    # ``hub_bundle`` (``backup_hub_bundle``, typically with
    # ``include_blobs=False``) as the baseline, produce an edited copy,
    # then sync. The engine diffs the two
    # bundles into targeted writes (plan → stale pre-flight → serial
    # ack-gated steps); nothing is deleted-and-restored. For a dry-run
    # preview of what a sync would write, feed the same bundle pair to the
    # pure planners ``build_activity_sync_plan``/``build_device_sync_plan``
    # (exported from the package root).
    #
    # These are explicit methods (not PROXY_METHODS delegates) so the
    # ``progress_callback`` is marshaled onto the event loop instead of
    # firing on the engine thread.

    async def sync_activity(
        self,
        *,
        baseline: dict,
        edited: dict,
        activity_id: int,
        progress_callback: Optional[Callable] = None,
    ) -> dict:
        """Write the ``baseline`` → ``edited`` diff for one activity to the hub.

        Returns the engine's result dict: ``{"status": "success",
        "completed_steps", "total_steps", "counters"}`` on success, or
        ``{"status": "failed", "failed_at", "message", ...}`` when the plan
        is out of scope, the activity changed on the hub since ``baseline``
        was captured (``failed_at: "stale_check"``), or the hub rejected a
        step. ``progress_callback`` (sync or async) receives keyword-only
        progress payloads (``phase``, ``message``, ``completed_steps``,
        ``total_steps``, ...) on the event loop.
        """

        return await self.run(
            self._proxy.sync_activity,
            baseline=baseline,
            edited=edited,
            activity_id=activity_id,
            progress_callback=self._marshal_optional(progress_callback),
        )

    async def sync_device(
        self,
        *,
        baseline: dict,
        edited: dict,
        device_id: int,
        progress_callback: Optional[Callable] = None,
    ) -> dict:
        """Device-scoped counterpart of :meth:`sync_activity`.

        Same bundle-pair contract and result dict, with the device id as
        the entity being edited (command adds/renames/payload edits, idle
        behaviour, input records).
        """

        return await self.run(
            self._proxy.sync_device,
            baseline=baseline,
            edited=edited,
            device_id=device_id,
            progress_callback=self._marshal_optional(progress_callback),
        )

    def _marshal_optional(self, callback: Optional[Callable]) -> Optional[Callable]:
        if callback is None:
            return None
        return _marshal_callback(self._loop, callback)

    # -- lazy-read plumbing --------------------------------------------------

    async def _read(
        self,
        getter: Callable,
        key: str,
        *args: Any,
        timeout: float,
        fetch_kw: str = "fetch_if_missing",
    ) -> Any:
        """Resolve a lazy ``(data, ready)`` getter to complete data.

        Returns cached data when already complete; otherwise kicks a hub
        fetch and awaits the matching burst. ``fetch_kw`` names the
        getter's "trigger a fetch" keyword — ``fetch_if_missing`` for the
        per-entity getters, ``force_refresh`` for the catalog getters.
        Raises ``RuntimeError`` when the hub can't be queried and nothing
        is cached, ``TimeoutError`` when the burst never lands.
        """

        data, ready = await self.run(getter, *args, **{fetch_kw: False})
        if ready:
            return data
        return await self._await_fetch(getter, key, *args, timeout=timeout, fetch_kw=fetch_kw)

    async def _await_fetch(
        self,
        getter: Callable,
        key: str,
        *args: Any,
        timeout: float,
        fetch_kw: str = "fetch_if_missing",
    ) -> Any:
        """Issue (or join) the hub fetch behind ``key`` and await its burst.

        When a fetch for ``key`` is already in flight the call only
        registers for its completion, so concurrent reads and the
        connect-time initial sync share one request.
        """

        self._raise_if_cannot_fetch(key)

        future = self._loop.create_future()
        self._burst_waiters.setdefault(key, []).append(future)
        self._ensure_burst_dispatch(key.split(":", 1)[0])

        # Ownership is released and the waiter dropped on EVERY exit path,
        # including a cancellation that lands while the request is still
        # being issued in the executor; otherwise the key would stay marked
        # in flight and every later read for it would join a fetch nobody
        # owns.
        owner = key not in self._inflight
        if owner:
            self._inflight.add(key)
        try:
            if owner:
                await self.run(getter, *args, **{fetch_kw: True})
            try:
                await asyncio.wait_for(future, timeout)
            except TimeoutError:
                raise FetchTimeoutError(f"timed out after {timeout}s fetching {key!r}")
        except BaseException:
            self._drop_burst_waiter(key, future)
            raise
        finally:
            if owner:
                self._inflight.discard(key)

        # A burst also ends on the engine's idle timeout without any reply
        # having landed; the getter's ready flag (and, for the catalogs,
        # the commit flag of the burst that just ended) is what proves the
        # data is real.
        # The engine may notify the burst end a few instructions before it
        # records completeness (an empty-keymap ACK finishes the burst,
        # then marks the entity), so give the flag a short grace window
        # before calling the fetch a failure.
        for attempt in range(5):
            data, ready = await self.run(getter, *args, **{fetch_kw: False})
            if ready and self._burst_committed(key):
                return data
            await asyncio.sleep(0.02 * (attempt + 1))
        raise FetchTimeoutError(
            f"fetch of {key!r} ended without a complete reply from the hub"
        )

    def _burst_committed(self, key: str) -> bool:
        """Whether the catalog burst behind ``key`` committed a full row set.

        Only the two catalogs carry this signal (``last_*_burst_committed``
        on the engine); every other key is judged by its getter's ready
        flag alone. Engines without the property are trusted.
        """

        attr = {
            "devices": "last_devices_burst_committed",
            "activities": "last_activities_burst_committed",
        }.get(key)
        if attr is None:
            return True
        return bool(getattr(self._proxy, attr, True))

    def _drop_burst_waiter(self, key: str, future: asyncio.Future) -> None:
        pending = self._burst_waiters.get(key)
        if pending and future in pending:
            pending.remove(future)

    def _raise_if_cannot_fetch(self, what: str) -> None:
        """Raise the typed reason a hub fetch is impossible right now."""

        if self._proxy.can_issue_commands():
            return
        if not self._proxy.transport.is_hub_connected:
            raise HubNotConnectedError(
                f"cannot fetch {what!r}: the hub is not connected yet "
                "(await wait_until_controllable() first)"
            )
        raise HubBusyError(
            f"cannot fetch {what!r}: an app client is connected and holds the hub"
        )

    def _ensure_burst_dispatch(self, kind: str) -> None:
        if kind in self._burst_dispatch_kinds:
            return
        self._burst_dispatch_kinds.add(kind)

        def dispatcher(full_key: str) -> None:
            # Fires on the engine thread; hop to the loop to resolve.
            self._loop.call_soon_threadsafe(self._resolve_burst, full_key)

        self._proxy.on_burst_end(kind, dispatcher)

    def _resolve_burst(self, full_key: str) -> None:
        for future in self._burst_waiters.pop(full_key, []):
            if not future.done():
                future.set_result(None)

    def __getattr__(self, name: str) -> Any:
        # Note: only consulted for names not found on the class/instance,
        # so explicit methods above always win.
        if name in self.PROXY_METHODS:
            target = getattr(self._proxy, name)

            async def delegate(*args: Any, **kwargs: Any) -> Any:
                return await self._loop.run_in_executor(
                    None, functools.partial(target, *args, **kwargs)
                )

            functools.update_wrapper(delegate, target)
            return delegate
        if name in self._LISTENER_METHODS:
            register = getattr(self._proxy, name)

            def add_listener(callback: Callable) -> None:
                register(_marshal_callback(self._loop, callback))

            functools.update_wrapper(add_listener, register)
            return add_listener
        raise AttributeError(
            f"{type(self).__name__!s} has no attribute {name!r}; "
            "use .sync to reach the underlying X1Proxy"
        )


class AsyncHubBrowser:
    """Asyncio wrapper around :class:`HubBrowser`.

    Accepts the same callbacks (sync or async); they are delivered on
    the event loop instead of the zeroconf engine thread. Start/stop run
    in the executor because zeroconf engine setup/teardown blocks.
    """

    def __init__(
        self,
        *,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        zc: Any = None,
        include_proxies: bool = False,
        service_types: Optional[Iterable[str]] = None,
        on_added: Optional[Callable] = None,
        on_updated: Optional[Callable] = None,
        on_removed: Optional[Callable] = None,
    ) -> None:
        self._loop = loop or asyncio.get_running_loop()
        kwargs: dict[str, Any] = {
            "zc": zc,
            "include_proxies": include_proxies,
            "on_added": self._wrap(on_added),
            "on_updated": self._wrap(on_updated),
            "on_removed": self._wrap(on_removed),
        }
        if service_types is not None:
            kwargs["service_types"] = service_types
        self._browser = HubBrowser(**kwargs)

    def _wrap(self, callback: Optional[Callable]) -> Optional[Callable]:
        if callback is None:
            return None
        return _marshal_callback(self._loop, callback)

    @property
    def sync(self) -> HubBrowser:
        return self._browser

    @property
    def hubs(self) -> list[DiscoveredHub]:
        return self._browser.hubs

    async def start(self) -> "AsyncHubBrowser":
        await self._loop.run_in_executor(None, self._browser.start)
        return self

    async def stop(self) -> None:
        await self._loop.run_in_executor(None, self._browser.stop)

    async def __aenter__(self) -> "AsyncHubBrowser":
        return await self.start()

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.stop()


# ---------------------------------------------------------------------------
# Engine-method triage
# ---------------------------------------------------------------------------
#
# The facade is curated by hand on purpose: it is a service layer built one
# feature at a time, not a pass-through of X1Proxy. That only stays honest
# if every public engine method was *placed* somewhere deliberately. The
# tiers are:
#
#   * wrapped   -- behind an explicit coroutine (WRAPPED_ENGINE_METHODS)
#   * delegated -- awaitable by name, raw engine signature (PROXY_METHODS)
#   * listener  -- loop-marshaled registration (_LISTENER_METHODS)
#   * engine-only -- not on the facade, with the reason recorded below
#
# tests/lib/test_aio.py asserts the four sets partition the engine's public
# methods exactly, so a new engine method fails CI until it is placed. The
# guard forces a decision, never exposure. Roadmap for the parked entries:
# docs/internal/sofabaton-x-phase1-facade-plan.md.

_R_TRANSPORT = (
    "transport plumbing: invoked by the bridge, deframer and opcode "
    "handlers, never by a consumer"
)
_R_ACK = "ack/exchange primitive composed by higher-level engine operations"
_R_SYNC = (
    "single-shot write primitive composed by sync_activity/sync_device "
    "(phase 1 plan, decision 2)"
)
_R_PHASE3 = "write operation parked for phase 3 (phase 1 plan, section 11)"
_R_INTEGRATION = (
    "Home Assistant orchestration hosted in the library, not promoted "
    "(phase 1 plan, decision 3)"
)
_R_F2 = "superseded by phase 1 F2 status()/hub_info()"
_R_F6 = "cache snapshot serializer; phase 1 F6 state document"
_R_INTERNAL_READ = (
    "per-entity request/assembly internal behind the facade reads and backup_*"
)
_R_CARD = "Home Assistant card concern"


def _reasons(reason: str, names: Iterable[str]) -> dict[str, str]:
    return {name: reason for name in names}


ENGINE_ONLY: dict[str, str] = {
    **_reasons(
        _R_TRANSPORT,
        (
            "handle_active_state",
            "notify_ack",
            "notify_activity_inputs_frame",
            "notify_hub_ready",
            "notify_ota_in_progress",
            "note_ack_ready_refresh",
            "note_buttons_frame",
            "note_catalog_status_ack",
            "ingest_activity_row",
            "ingest_device_row",
            "record_app_activation",
            "record_banner_payload",
            "record_hub_name",
            "record_idle_behavior_value",
            "try_finish_activities_burst",
            "try_finish_activity_map_burst",
            "try_finish_buttons_burst",
            "try_finish_devices_burst",
            "try_finish_ir_dump_burst",
            "flag_pending_redundant_off_check",
            "parse_device_commands",
            "cache_macro_record",
            "drop_cached_macro_records",
            "set_assigned_device_id",
            "update_x2_remote_sync_id",
            "get_routed_local_ip",
            "enqueue_cmd",
        ),
    ),
    **_reasons(
        _R_ACK,
        (
            "wait_for_ack",
            "wait_for_ack_any",
            "wait_for_ack_family_low",
            "wait_for_any_response",
            "wait_for_assigned_device_id",
            "wait_for_macro_record",
            "wait_for_activity_inputs_burst",
            "wait_for_read_burst_quiesce",
            "wait_for_x2_remote_sync_id",
            "wait_for_virtual_device",
            "clear_ack_queue",
            "reset_ack_queues",
            "exchange",
            "execute_exchange",
            "query_device_input_index",
            "fetch_device_input_entries",
            "start_virtual_device",
            "update_virtual_device",
        ),
    ),
    **_reasons(
        _R_SYNC,
        (
            "set_idle_behavior",
            "overwrite_command_payload",
            "persist_command_record",
            # demoted from PROXY_METHODS in 0.2.0 (phase 1 plan, decision 12)
            "command_to_button",
            "command_to_favorite",
            "delete_favorite",
            "reorder_favorites",
            "add_device_to_activity",
            "persist_ir_blob",
        ),
    ),
    **_reasons(
        _R_PHASE3,
        (
            "create_device",
            "reorder_devices",
            "set_ir_learn_mode",
            "ir_learn_command",
            "cancel_ir_learn",
            "get_idle_behavior",
            "fetch_idle_behavior",
            "request_idle_behavior",
            "request_favorites_order",
            "apply_external_activity_state",
        ),
    ),
    **_reasons(
        _R_INTEGRATION,
        ("create_wifi_device", "create_wifi_mqtt_device", "run_wifi_inplace_plan"),
    ),
    **_reasons(_R_F2, ("request_banner_info",)),
    **_reasons(_R_F6, ("export_cache_state", "import_cache_state", "wipe_all_cached_state")),
    **_reasons(
        _R_INTERNAL_READ,
        (
            "assemble_activity_backup_from_state",
            "assemble_device_backup_from_state",
            "assemble_hub_bundle_from_state",
            "clear_cached_entity_detail",
            "activities_referencing_device",
            "get_single_command_for_entity",
            "request_buttons_for_entity",
            "request_commands_for_entity",
            "request_macros_for_activity",
            "request_ip_commands_for_device",
        ),
    ),
    **_reasons(_R_CARD, ("on_redundant_off_press",)),
}


def public_engine_methods(engine_cls: type = X1Proxy) -> frozenset[str]:
    """Names of the public *methods* on ``engine_cls`` (properties excluded)."""

    return frozenset(
        name
        for name, member in inspect.getmembers(engine_cls)
        if not name.startswith("_") and inspect.isfunction(member)
    )


def engine_method_triage(engine_cls: type = X1Proxy) -> dict[str, set[str]]:
    """Check that the facade tiers partition the engine's public methods.

    Returns three sets, all empty when the triage is complete:

    * ``untriaged`` -- public engine methods placed in no tier;
    * ``overlap`` -- names placed in more than one tier;
    * ``stale`` -- names placed in a tier that the engine no longer has.
    """

    tiers = {
        "wrapped": set(AsyncXProxy.WRAPPED_ENGINE_METHODS),
        "delegated": set(AsyncXProxy.PROXY_METHODS),
        "listener": set(AsyncXProxy._LISTENER_METHODS),
        "engine_only": set(ENGINE_ONLY),
    }
    placed: dict[str, int] = {}
    for names in tiers.values():
        for name in names:
            placed[name] = placed.get(name, 0) + 1
    public = public_engine_methods(engine_cls)
    return {
        "untriaged": set(public) - set(placed),
        "overlap": {name for name, count in placed.items() if count > 1},
        "stale": set(placed) - set(public),
    }


async def async_discover_hubs(
    timeout: float = DEFAULT_DISCOVERY_TIMEOUT,
    *,
    zc: Any = None,
    include_proxies: bool = False,
) -> list[DiscoveredHub]:
    """Async one-shot hub scan; the blocking browse runs in the executor."""

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        functools.partial(
            discover_hubs, timeout=timeout, zc=zc, include_proxies=include_proxies
        ),
    )
