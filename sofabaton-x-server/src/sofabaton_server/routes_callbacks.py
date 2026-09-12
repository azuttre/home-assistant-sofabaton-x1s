"""``/api/v1/hubs/{hub_id}/callback-device``, ``/presses`` and the listener
routes (callbacks plan, C2 to C4).

The rule the routes follow: an immediate 409 is something the record
alone decides (a device already deployed, a stale one, an X1 with the
wrong port, references the client must clear); anything that needs the
hub happens inside the accepted job and shows up as a failed job with a
coded error (``callback_update_declined``, ``callback_update_failed``,
the usual hub problems).
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any, Optional

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from sofabaton import WIFI_SLOT_COUNT, AsyncXProxy

from . import API_PREFIX
from .callbacks import (
    CallbackDeviceExists,
    CallbackDeviceMissing,
    CallbackDeviceNotStale,
    CallbackDeviceStale,
    CallbackPortRefused,
    CallbackService,
    ListenerState,
)
from .jobs import JobView
from .manager import HubDisabled, HubNotFound
from .models import Problem
from .problems import ApiProblem, hub_disabled, hub_not_found
from .routes_edit import _require_control
from .routes_snapshot import start_job

log = logging.getLogger(__name__)

router = APIRouter(prefix=f"{API_PREFIX}/hubs/{{hub_id}}", tags=["callbacks"])
server_router = APIRouter(prefix=f"{API_PREFIX}/server", tags=["server"])

_ERRORS = {404: {"model": Problem}, 409: {"model": Problem}, 422: {"model": Problem}, 503: {"model": Problem}}


# -- bodies and views -------------------------------------------------------------


class CallbackSlot(BaseModel):
    label: str = Field(min_length=1, max_length=30)
    long_label: Optional[str] = Field(None, max_length=30, description="default: '<label> Long'")


class CallbackDeviceRequest(BaseModel):
    """Complete desired spec for POST and PUT, not a partial update.

    Every slot is written; omitted slots become ``Button n`` and omitted
    power/input hooks are cleared. For a rename, copy all fields from the
    current record's spec and change only the intended labels. Preserving
    device/command IDs and generic bindings does not preserve omitted fields.
    Hook slots are one-based (1..10); callback URL indexes are zero-based (0..9).
    """

    name: str = Field("Server", min_length=1, max_length=30)
    slots: list[CallbackSlot] = Field(default_factory=list, max_length=WIFI_SLOT_COUNT)
    power_on_slot: Optional[int] = Field(None, ge=1, le=WIFI_SLOT_COUNT,
                                         description="slot the hub fires when an activity powers on (X1S/X2)")
    power_off_slot: Optional[int] = Field(None, ge=1, le=WIFI_SLOT_COUNT)
    input_slots: list[int] = Field(default_factory=list, description="slots offered as activity-start inputs (X1S/X2)")


class CallbackTargetView(BaseModel):
    host: str
    port: int
    action_id: str


class EffectiveDestination(BaseModel):
    """What a deploy would bake into the records right now (settings, else the routed local IP)."""

    host: str
    port: int


class CallbackPendingView(BaseModel):
    op: str
    started_at: str
    spec: Optional[dict[str, Any]] = None


class CallbackLastPress(BaseModel):
    seq: int
    received_at: str


class CallbackDeviceView(BaseModel):
    """The deployed record plus the destination a new deploy would use now.

    ``target`` is the address already written to the device;
    ``effective_destination`` follows current settings and can differ.
    ``stale`` flags a missing device; ``callback_device_stale`` is an event/error name.
    """

    device_id: Optional[int]
    spec: dict[str, Any]
    target: CallbackTargetView
    labels: dict[str, str]
    hub_version: str
    deployed_at: Optional[str]
    adopted: bool
    stale: bool
    deployed: bool
    pending: Optional[CallbackPendingView] = None
    last_press: Optional[CallbackLastPress] = None
    effective_destination: Optional[EffectiveDestination] = None


class CallbackReference(BaseModel):
    activity_id: int
    name: Optional[str]
    kinds: list[str]
    complete: bool


class PressView(BaseModel):
    seq: int
    hub_id: str
    device_id: int
    command_id: Optional[int]
    slot: Optional[int]
    label: Optional[str]
    press_type: str
    resolution: str
    transport: str
    source: str
    received_at: str


class PressPage(BaseModel):
    """``GET /hubs/{id}/presses``: the ring, oldest first when ``after`` is given.

    ``expired`` means presses newer than ``after`` were already evicted
    from the ring; the client missed some and must accept the gap.
    ``instance_id`` changes on every server restart, and so does the
    sequence; on a new instance start from the current ``hello``.
    """

    instance_id: str
    last_seq: int
    expired: bool
    presses: list[PressView]


class CallbackListenerView(BaseModel):
    wanted: bool
    bound: bool
    port: int
    bound_port: Optional[int]
    last_error: Optional[str]
    next_retry_at: Optional[str]


# -- helpers -------------------------------------------------------------------------


def _service(request: Request) -> CallbackService:
    return request.app.state.callbacks


def _proxy(request: Request, hub_id: str) -> AsyncXProxy:
    try:
        return request.app.state.hub_manager.proxy(hub_id)
    except HubNotFound:
        raise hub_not_found(hub_id) from None
    except HubDisabled:
        raise hub_disabled(hub_id) from None


def _known_hub(request: Request, hub_id: str) -> None:
    try:
        request.app.state.hub_manager.record(hub_id)
    except HubNotFound:
        raise hub_not_found(hub_id) from None


def _view(service: CallbackService, hub_id: str, request: Request) -> CallbackDeviceView:
    record = service.record(hub_id)
    if record is None:
        raise ApiProblem(404, "callback_device_not_found", "No callback device on this hub",
                         detail="deploy one with POST /callback-device", hub_id=hub_id)
    destination = None
    try:
        proxy = request.app.state.hub_manager.proxy(hub_id)
    except (HubNotFound, HubDisabled):
        proxy = None
    if proxy is not None:
        host, port = service.target_for(proxy)
        destination = {"host": host, "port": port}
    return CallbackDeviceView(**record.view(effective_destination=destination))


def _listener_view(state: ListenerState) -> CallbackListenerView:
    return CallbackListenerView(**asdict(state))


# -- the callback device ---------------------------------------------------------------


@router.get("/callback-device", operation_id="getCallbackDevice", response_model=CallbackDeviceView,
            summary="The hub's callback device record", responses={404: {"model": Problem}})
async def get_callback_device(request: Request, hub_id: str) -> CallbackDeviceView:
    _known_hub(request, hub_id)
    return _view(_service(request), hub_id, request)


@router.post("/callback-device", operation_id="deployCallbackDevice", response_model=JobView, status_code=202,
             summary="Deploy the callback device (a job); the result is the record", responses=_ERRORS)
async def deploy_callback_device(request: Request, hub_id: str, body: CallbackDeviceRequest) -> JobView:
    service = _service(request)
    proxy = _proxy(request, hub_id)
    existing = service.record(hub_id)
    if existing is not None and existing.device_id is not None and existing.pending is None:
        raise ApiProblem(409, "callback_device_exists", "A callback device is already deployed",
                         detail=f"device {existing.device_id}; update it, or remove it first", hub_id=hub_id)
    try:
        spec = service.spec_from_body(body.model_dump())
        service.check_port((await proxy.status()).hub_version)
    except CallbackPortRefused as err:
        raise ApiProblem(409, "callback_port_x1", "An X1 hub can only call back on port 8060",
                         detail=str(err), hub_id=hub_id) from err
    except ValueError as err:
        raise ApiProblem(422, "invalid_request", "Invalid callback device", detail=str(err), hub_id=hub_id) from err
    await _require_control(proxy, hub_id)

    async def run(progress) -> dict[str, Any]:
        try:
            record = await service.deploy(hub_id, proxy, spec)
        except CallbackDeviceExists as err:
            raise ApiProblem(409, "callback_device_exists", "A callback device is already deployed",
                             detail=str(err), hub_id=hub_id) from err
        host, port = service.target_for(proxy)
        return record.view(effective_destination={"host": host, "port": port})

    return start_job(request, hub_id, "deploy_callback_device", run, cancellable=False)


@router.put("/callback-device", operation_id="updateCallbackDevice", response_model=JobView, status_code=202,
            summary="Edit the callback device in place (a job); declined drift fails the job", responses=_ERRORS)
async def update_callback_device(request: Request, hub_id: str, body: CallbackDeviceRequest) -> JobView:
    service = _service(request)
    proxy = _proxy(request, hub_id)
    record = service.record(hub_id)
    if record is None or record.device_id is None:
        raise ApiProblem(404, "callback_device_not_found", "No callback device on this hub", hub_id=hub_id)
    if record.stale:
        raise ApiProblem(409, "callback_device_stale", "The callback device is stale",
                         detail="the hub no longer has it; POST /callback-device/redeploy", hub_id=hub_id)
    try:
        spec = service.spec_from_body(body.model_dump())
    except ValueError as err:
        raise ApiProblem(422, "invalid_request", "Invalid callback device", detail=str(err), hub_id=hub_id) from err
    await _require_control(proxy, hub_id)

    async def run(progress) -> dict[str, Any]:
        updated = await service.update(hub_id, proxy, spec)
        host, port = service.target_for(proxy)
        return updated.view(effective_destination={"host": host, "port": port})

    return start_job(request, hub_id, "update_callback_device", run, cancellable=False)


@router.delete("/callback-device", operation_id="removeCallbackDevice", response_model=JobView, status_code=202,
               summary="Remove the callback device from the hub and forget it (a job)", responses=_ERRORS)
async def remove_callback_device(
    request: Request, hub_id: str,
    force: bool = Query(False, description="remove even when activities still reference the device"),
) -> JobView:
    service = _service(request)
    proxy = _proxy(request, hub_id)
    record = service.record(hub_id)
    if record is None:
        raise ApiProblem(404, "callback_device_not_found", "No callback device on this hub", hub_id=hub_id)
    if record.device_id is not None and not force:
        references = service.references(await proxy.snapshot(), record.device_id)
        if references:
            names = ", ".join(f"{r['activity_id']} ({', '.join(r['kinds'])})" for r in references)
            raise ApiProblem(409, "callback_device_referenced", "Activities still reference the callback device",
                             detail=f"referenced by activity {names}; clear them or pass ?force=true", hub_id=hub_id)
    await _require_control(proxy, hub_id)

    async def run(progress) -> dict[str, Any]:
        return await service.remove(hub_id, proxy)

    return start_job(request, hub_id, "remove_callback_device", run, cancellable=False)


@router.post("/callback-device/redeploy", operation_id="redeployCallbackDevice", response_model=JobView,
             status_code=202, summary="Deploy a stale callback device again from its stored spec (a job)",
             responses=_ERRORS)
async def redeploy_callback_device(request: Request, hub_id: str) -> JobView:
    service = _service(request)
    proxy = _proxy(request, hub_id)
    record = service.record(hub_id)
    if record is None or record.device_id is None:
        raise ApiProblem(404, "callback_device_not_found", "No callback device on this hub", hub_id=hub_id)
    if not record.stale:
        raise ApiProblem(409, "callback_device_not_stale", "The callback device is not stale",
                         detail="it is still on the hub; update it instead", hub_id=hub_id)
    try:
        service.check_port((await proxy.status()).hub_version)
    except CallbackPortRefused as err:
        raise ApiProblem(409, "callback_port_x1", "An X1 hub can only call back on port 8060",
                         detail=str(err), hub_id=hub_id) from err
    await _require_control(proxy, hub_id)

    async def run(progress) -> dict[str, Any]:
        try:
            fresh = await service.redeploy(hub_id, proxy)
        except CallbackDeviceNotStale as err:
            raise ApiProblem(409, "callback_device_not_stale", "The callback device is not stale",
                             detail=str(err), hub_id=hub_id) from err
        except CallbackDeviceMissing as err:
            raise ApiProblem(404, "callback_device_not_found", "No callback device on this hub", hub_id=hub_id) from err
        host, port = service.target_for(proxy)
        return fresh.view(effective_destination={"host": host, "port": port})

    return start_job(request, hub_id, "redeploy_callback_device", run, cancellable=False)


# -- presses -----------------------------------------------------------------------------


@router.get("/presses", operation_id="listPresses", response_model=PressPage,
            summary="Recent button presses (the catch-up view of the press stream)",
            responses={404: {"model": Problem}})
async def list_presses(
    request: Request, hub_id: str,
    after: Optional[int] = Query(None, ge=0, description="return presses with seq greater than this, oldest first"),
    limit: int = Query(100, ge=1, le=1000),
) -> PressPage:
    _known_hub(request, hub_id)
    ring = _service(request).ring
    if after is None:
        rows, expired = ring.newest(hub_id, limit), False
    else:
        rows, expired = ring.since(hub_id, after, limit)
    return PressPage(instance_id=ring.instance_id, last_seq=ring.last_seq, expired=expired,
                     presses=[PressView(**row.to_dict()) for row in rows])


# -- the listener (server level) ------------------------------------------------------------


@server_router.get("/callback-listener", operation_id="getCallbackListener", response_model=CallbackListenerView,
                   summary="State of the callback listener")
async def get_callback_listener(request: Request) -> CallbackListenerView:
    return _listener_view(_service(request).listener_state())


@server_router.post("/callback-listener/retry", operation_id="retryCallbackListener",
                    response_model=CallbackListenerView, summary="Try to bind the callback listener now")
async def retry_callback_listener(request: Request) -> CallbackListenerView:
    service = _service(request)
    await service.listener.retry_now()
    return _listener_view(service.listener_state())
