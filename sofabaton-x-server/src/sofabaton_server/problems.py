"""One failure shape for the whole API: raise ``ApiProblem``, get a ``Problem`` body.

The mapping from the library's typed errors to HTTP status (plan
section 7) lives here so every route uses the same table:

| condition                                  | status |
| unknown hub id or entity                   | 404    |
| hub configured but disabled                | 409    |
| ``HubBusyError`` (an app holds the hub)    | 409    |
| ``HubNotConnectedError``                   | 503    |
| ``FetchTimeoutError``                      | 504    |
| send refused (``False`` from the facade)   | 409    |
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import AsyncIterator, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from sofabaton import FetchTimeoutError, HubBusyError, HubNotConnectedError

from .models import Problem


class ApiProblem(Exception):
    def __init__(
        self,
        status: int,
        type_: str,
        title: str,
        *,
        detail: Optional[str] = None,
        hub_id: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> None:
        super().__init__(f"{status} {type_}: {detail or title}")
        self.problem = Problem(type=type_, title=title, status=status, detail=detail, hub_id=hub_id, mode=mode)


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiProblem)
    async def _handle(_request: Request, err: ApiProblem) -> JSONResponse:
        return JSONResponse(status_code=err.problem.status, content=asdict(err.problem))

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(_request: Request, err: RequestValidationError) -> JSONResponse:
        # One failure shape for the whole API: a bad body or query is a
        # Problem too, not the framework's list-of-errors document.
        problem = Problem(type="validation_error", title="Invalid request", status=422,
                          detail=summarize_validation(err))
        return JSONResponse(status_code=422, content=asdict(problem))


def summarize_validation(err: RequestValidationError) -> str:
    parts = []
    for item in err.errors():
        loc = ".".join(str(p) for p in item.get("loc", ()) if p != "body") or "body"
        parts.append(f"{loc}: {item.get('msg', 'invalid')}")
    return "; ".join(parts) or "invalid request"


def hub_not_found(hub_id: str) -> ApiProblem:
    return ApiProblem(404, "hub_not_found", "Unknown hub", hub_id=hub_id)


def entity_not_found(hub_id: str, kind: str, entity_id: int) -> ApiProblem:
    return ApiProblem(404, f"{kind}_not_found", f"Unknown {kind}", detail=f"{kind} {entity_id} is not in the hub's catalog", hub_id=hub_id)


def hub_start_failed(hub_id: str, cause: BaseException) -> ApiProblem:
    return ApiProblem(503, "hub_start_failed", "The hub's proxy could not start",
                      detail=str(cause), hub_id=hub_id, mode="disconnected")


def hub_disabled(hub_id: str) -> ApiProblem:
    return ApiProblem(409, "hub_disabled", "Hub is disabled", detail="enable it first", hub_id=hub_id, mode="disconnected")


@asynccontextmanager
async def hub_errors(hub_id: str) -> AsyncIterator[None]:
    """Translate the library's typed errors raised inside the block."""

    try:
        yield
    except HubBusyError as err:
        raise ApiProblem(409, "hub_busy", "An app client holds the hub", detail=str(err), hub_id=hub_id, mode="observe") from err
    except HubNotConnectedError as err:
        raise ApiProblem(503, "hub_not_connected", "Hub is not connected", detail=str(err), hub_id=hub_id, mode="disconnected") from err
    except FetchTimeoutError as err:
        raise ApiProblem(504, "hub_timeout", "The hub did not reply in time", detail=str(err), hub_id=hub_id) from err
