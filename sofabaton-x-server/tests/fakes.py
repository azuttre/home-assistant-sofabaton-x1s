"""A fake AsyncXProxy for manager and route tests.

Models what the server touches: lifecycle, ``status()``, ``hub_info()``,
``events()``, the six reads, control, and the catalog refresh delegate.
``ready(mac)`` emulates the connect-time initial sync completing, which
is when the manager learns the MAC. ``fail_with`` injects one of the
library's typed errors into every read; ``refuse`` makes control calls
return False the way the facade does when the proxy does not own the
hub.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from sofabaton import (
    Activity,
    Button,
    CatalogReady,
    Command,
    Device,
    Favorite,
    HubConfig,
    HubEvent,
    HubInfo,
    HubStatus,
    Macro,
    RunningActivity,
)


class FakeProxy:
    def __init__(self, config: HubConfig) -> None:
        self.config = config
        self.started = False
        self.stops: list[bool] = []          # release_hub flag per stop()
        self.mac: Optional[str] = None
        self.model = "X1S"
        self._queue: asyncio.Queue = asyncio.Queue()
        self._seq = 0
        self.catalog_ready = False
        self.fail_with: Optional[BaseException] = None
        self.refuse = False
        self.running: Optional[RunningActivity] = None
        self.sent: list[tuple[str, tuple]] = []
        self.catalog_clears = 0
        self.refreshes = 0
        self.start_error: Optional[BaseException] = None
        self.activities_data = [
            Activity(activity_id=101, name="Watch TV", active=False, needs_confirm=False),
            Activity(activity_id=102, name="Music", active=False, needs_confirm=False),
        ]
        self.devices_data = [
            Device(device_id=1, name="TV", brand="Sony", device_class="ir", device_class_code=1, power_state=0, idle_behavior=None),
            Device(device_id=2, name="Amp", brand="Denon", device_class="ir", device_class_code=1, power_state=1, idle_behavior=2),
        ]

    # -- lifecycle -----------------------------------------------------------

    def set_zeroconf(self, zc) -> None:
        self.zeroconf = zc

    async def start(self) -> None:
        if self.start_error is not None:
            raise self.start_error
        self.started = True

    async def stop(self, *, release_hub: bool = False) -> None:
        self.started = False
        self.stops.append(release_hub)

    # -- status --------------------------------------------------------------

    async def status(self) -> HubStatus:
        return HubStatus(
            hub_connected=self.started,
            app_connected=self.refuse,
            controllable=self.started and not self.refuse,
            mode="observe" if (self.started and self.refuse) else ("control" if self.started else "disconnected"),
            hub_version=self.model,
            proxy_enabled=True,
            running_activity=self.running,
            activities_cached=len(self.activities_data),
            devices_cached=len(self.devices_data),
            catalog_ready=self.catalog_ready,
        )

    async def hub_info(self, *, refresh: bool = False) -> HubInfo:
        self._maybe_fail()
        if self.mac is None:
            return HubInfo(known=False, model=None, name=None, mac=None, firmware_version=None, production_batch=None)
        return HubInfo(known=True, model=self.model, name="X1 HUB", mac=self.mac, firmware_version=5, production_batch="20221120")

    # -- reads ---------------------------------------------------------------

    def _maybe_fail(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    async def activities(self) -> list[Activity]:
        self._maybe_fail()
        return list(self.activities_data)

    async def devices(self, *, refresh: bool = False) -> list[Device]:
        self._maybe_fail()
        if refresh:
            self.refreshes += 1
        return list(self.devices_data)

    async def clear_devices_catalog(self) -> None:
        self.catalog_clears += 1

    async def commands(self, device_id: int) -> list[Command]:
        self._maybe_fail()
        return [Command(command_id=1, label="Power"), Command(command_id=2, label="Mute")]

    async def buttons(self, entity_id: int) -> list[Button]:
        self._maybe_fail()
        return [Button(button_code=174, name="UP", device_id=1, command_id=17)]

    async def macros(self, activity_id: int) -> list[Macro]:
        self._maybe_fail()
        return [Macro(command_id=200, label="All On")]

    async def favorites(self, activity_id: int) -> list[Favorite]:
        self._maybe_fail()
        return [Favorite(device_id=1, command_id=1, label="Power")]

    async def current_activity(self):
        return None if self.running is None else {"activity_id": self.running.activity_id, "name": self.running.name}

    # -- control -------------------------------------------------------------

    async def send(self, entity_id: int, command_id: int) -> bool:
        self.sent.append(("send", (entity_id, command_id)))
        return not self.refuse

    async def start_activity(self, activity_id: int) -> bool:
        self.sent.append(("start", (activity_id,)))
        if not self.refuse:
            self.running = RunningActivity(activity_id=activity_id, name=next((a.name for a in self.activities_data if a.activity_id == activity_id), None))
        return not self.refuse

    async def stop_activity(self, activity_id: int) -> bool:
        self.sent.append(("stop", (activity_id,)))
        if not self.refuse:
            self.running = None
        return not self.refuse

    async def find_remote(self) -> bool:
        self.sent.append(("find", ()))
        return not self.refuse

    # -- events --------------------------------------------------------------

    async def events(self):
        while True:
            yield await self._queue.get()

    def emit(self, kind: str, payload=None) -> None:
        self._seq += 1
        self._queue.put_nowait(HubEvent(seq=self._seq, kind=kind, payload=payload))

    def ready(self, mac: str) -> None:
        self.mac = mac
        self.catalog_ready = True
        self.emit("catalog_ready", CatalogReady(ready=True))


class Factory:
    """Records every proxy it built, keyed by host."""

    def __init__(self) -> None:
        self.built: dict[str, list[FakeProxy]] = {}
        self.start_error: Optional[BaseException] = None   # injected into new proxies

    def __call__(self, config: HubConfig) -> FakeProxy:
        proxy = FakeProxy(config)
        proxy.start_error = self.start_error
        self.built.setdefault(config.host, []).append(proxy)
        return proxy

    def latest(self, host: str) -> FakeProxy:
        return self.built[host][-1]


# -- discovery fakes ---------------------------------------------------------------


class FakeBrowser:
    """Stands in for AsyncHubBrowser: the test feeds advertisements through it."""

    instances: list["FakeBrowser"] = []

    def __init__(self, *, zc=None, include_proxies=False, on_added=None, on_updated=None, on_removed=None) -> None:
        self.zc = zc
        self.include_proxies = include_proxies
        self.on_added, self.on_updated, self.on_removed = on_added, on_updated, on_removed
        self.running = False
        FakeBrowser.instances.append(self)

    async def start(self):
        self.running = True
        return self

    async def stop(self) -> None:
        self.running = False


class FakeAdvertiser:
    def __init__(self) -> None:
        self.started: list[tuple[int, dict]] = []
        self.updates: list[dict] = []
        self.stopped = False

    def start(self, zc, settings, hub_count) -> None:
        from sofabaton_server.discovery import advertisement_txt
        self.started.append((settings.port, advertisement_txt(settings, hub_count)))

    def update(self, settings, hub_count) -> None:
        from sofabaton_server.discovery import advertisement_txt
        self.updates.append(advertisement_txt(settings, hub_count))

    def stop(self) -> None:
        self.stopped = True


class FakeZeroconf:
    def close(self) -> None:
        pass


def no_network_discovery(settings, manager, *, scanner=None):
    """A DiscoveryService that never touches the network (for route tests)."""

    from sofabaton_server.discovery import DiscoveryService

    async def _no_hubs(**kwargs):
        return []

    return DiscoveryService(
        settings, manager,
        browser_factory=FakeBrowser,
        scanner=scanner or _no_hubs,
        advertiser=FakeAdvertiser(),
        zeroconf=FakeZeroconf(),
    )
