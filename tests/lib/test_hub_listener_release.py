"""The shared listener's release feedback loop, over real loopback sockets.

Found live on 2026-09-09 (bench_200): both hubs waited ~2.9 s after a
dropped session before dialling again, missed the 2.5 s bounce window,
were accepted-and-dropped as unrecognised, and then retried every ~12 ms
forever. ``release_hub`` turns the release into a feedback loop: a
released hub that dials back triggers another bounce, so one of its own
retries meets a closed port.
"""

from __future__ import annotations

import importlib
import importlib.util
import socket
import sys
import time
import types
from pathlib import Path

LIB_DIR = (
    Path(__file__).resolve().parents[2]
    / "custom_components"
    / "sofabaton_x1s"
    / "lib"
)


def _load_lib() -> types.ModuleType:
    name = "sofabaton_listener_test_pkg"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, LIB_DIR / "__init__.py", submodule_search_locations=[str(LIB_DIR)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_pkg = _load_lib()
hub_listener = importlib.import_module(f"{_pkg.__name__}.hub_listener")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _connect(port: int) -> str:
    """'accepted' when the listener took (and dropped) us, 'closed' otherwise.

    A closed port answers with a refusal on Linux; on Windows the SYN can
    simply go unanswered while a previous accept call is winding down, so
    a short connect timeout counts as closed too.
    """

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.4)
    try:
        s.connect(("127.0.0.1", port))
    except (ConnectionRefusedError, OSError):
        return "closed"
    finally:
        try:
            s.close()
        except OSError:
            pass
    return "accepted"


def _wait(pred, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_released_hub_that_dials_back_is_refused_again() -> None:
    port = _free_port()
    listener = hub_listener.HubListener(port)
    try:
        # Another hub keeps the shared port open; the connecting peer (us,
        # 127.0.0.1) is the hub being released.
        listener.register_hub(proxy_id="other", real_hub_ip="10.255.255.1", on_socket=lambda *_: None)
        assert _wait(lambda: _connect(port) == "accepted"), "listener never came up"

        listener.release_hub("127.0.0.1", downtime=1.6, grace=10.0, max_bounces=3)
        # First bounce: closed right away.
        assert _connect(port) == "closed"
        # Window over: the port is back for the other hub.
        assert _wait(lambda: _connect(port) == "accepted", 5.0), "listener did not come back"

        # That accepted attempt was the released hub dialling back, which
        # must trigger a fresh bounce (feedback) rather than a silent drop.
        assert _wait(lambda: _connect(port) == "closed", 2.0), "no bounce after the released hub dialled back"
        assert _wait(lambda: _connect(port) == "accepted", 5.0)
        with listener._lock:
            rel = listener._released["127.0.0.1"]
        assert rel.bounces_left <= 2

        # Registering the hub again ends its release: dial-backs are no
        # longer refused-again (it is a recognised hub now).
        listener.register_hub(proxy_id="me", real_hub_ip="127.0.0.1", on_socket=lambda sock, addr: sock.close())
        with listener._lock:
            assert "127.0.0.1" not in listener._released
        # The dial-back above was accepted by the kernel before the accept
        # loop saw it, so the bounce it earns (the third) may only start
        # now, after the registration. Let that in-flight bounce play out;
        # a registered hub earns no further ones.
        _wait(lambda: _connect(port) == "closed", 2.0)
        assert _wait(lambda: _connect(port) == "accepted", 5.0), "listener did not come back"
        time.sleep(0.5)
        assert _connect(port) == "accepted"                    # no bounce for a registered hub
    finally:
        listener.shutdown()


def test_release_bounces_are_bounded_and_expire() -> None:
    port = _free_port()
    listener = hub_listener.HubListener(port)
    try:
        listener.register_hub(proxy_id="other", real_hub_ip="10.255.255.1", on_socket=lambda *_: None)
        assert _wait(lambda: _connect(port) == "accepted")
        listener.release_hub("127.0.0.1", downtime=1.6, grace=10.0, max_bounces=1)
        assert _connect(port) == "closed"
        assert _wait(lambda: _connect(port) == "accepted", 5.0)     # dial-back #1: consumes the last bounce
        assert _wait(lambda: _connect(port) == "closed", 2.0)
        assert _wait(lambda: _connect(port) == "accepted", 5.0)     # dial-back #2: no bounces left
        time.sleep(2.0)
        assert _connect(port) == "accepted"                         # stays up: budget exhausted
        with listener._lock:
            assert "127.0.0.1" not in listener._released           # entry dropped once spent
    finally:
        listener.shutdown()


def test_release_without_listener_is_a_noop() -> None:
    hub_listener.reset_hub_listener_for_tests()
    hub_listener.release_hub_from_listener("127.0.0.1")           # nothing running: no error
