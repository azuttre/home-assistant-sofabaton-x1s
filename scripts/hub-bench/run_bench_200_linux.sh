#!/usr/bin/env bash
# Run bench_200 (sofabaton-x-server live program) on a LINUX host on the
# hubs' LAN. Builds both wheels from this checkout, makes a throwaway
# venv, installs them plus the bench's client libraries, toggles the
# Home Assistant entries when the token files are present, runs the
# program, and prints where the results are.
#
# Requirements on the host: Python 3.11+ with the venv module (the script
# prefers `python3.13` when present; override with PYTHON=/path/to/python),
# a copy of this repository (branch dev; git is optional), same LAN
# segment as the hubs, TCP 8200 and UDP 8102 free (nothing else proxying
# the hubs while it runs).
#
#   bash scripts/hub-bench/run_bench_200_linux.sh [tag]
#   PYTHON=python3.13 bash scripts/hub-bench/run_bench_200_linux.sh diskstation
#
# Without scripts/.ha-token and scripts/.ha-config.json, disable the X1
# and X1S entries in Home Assistant yourself before running, and enable
# them afterwards.
set -euo pipefail

TAG="${1:-linux}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
VENV="$ROOT/.venv-bench"
PY="$VENV/bin/python"
# A NAS often ships several Pythons and `python` / `python3` may be old.
PYTHON="${PYTHON:-$(command -v python3.13 || command -v python3.12 || command -v python3.11 || command -v python3)}"

if command -v git >/dev/null 2>&1 && git -C "$ROOT" rev-parse --short HEAD >/dev/null 2>&1; then
  echo "== repo: $ROOT (branch $(git -C "$ROOT" rev-parse --abbrev-ref HEAD), $(git -C "$ROOT" rev-parse --short HEAD))"
else
  echo "== repo: $ROOT (no git metadata)"
fi
echo "== host: $(uname -srm), interpreter: $PYTHON ($("$PYTHON" --version 2>&1))"
"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 11), f"need Python 3.11+, got {sys.version}"'

if [ ! -x "$PY" ]; then
  echo "== creating venv $VENV"
  "$PYTHON" -m venv "$VENV"
fi
"$PY" -m pip install -q --upgrade pip build >/dev/null
echo "== building wheels"
rm -rf "$ROOT/dist-bench"
"$PY" -m build --wheel --outdir "$ROOT/dist-bench" "$ROOT" >/dev/null
"$PY" -m build --wheel --outdir "$ROOT/dist-bench" "$ROOT/sofabaton-x-server" >/dev/null
# Dependencies explicitly, plain uvicorn (no compiled extras: a NAS may
# have no wheels for uvloop/httptools and no compiler).
"$PY" -m pip install -q zeroconf fastapi uvicorn httpx websockets >/dev/null
"$PY" -m pip install -q --force-reinstall --no-deps "$ROOT"/dist-bench/*.whl >/dev/null
"$PY" -c "import sofabaton, sofabaton_server, sofabaton.hub_listener as h; print('== installed sofabaton', sofabaton.__version__, 'server', sofabaton_server.__version__, 'release window', h.DEFAULT_RELEASE_DOWNTIME_S)"

echo "== closed-port check (must say refused on this host)"
if [ -f "$ROOT/scripts/hub-bench/closed_port_check.py" ]; then
  "$PY" "$ROOT/scripts/hub-bench/closed_port_check.py" || true
else
  "$PY" -c 'import socket, time
s = socket.socket(); s.settimeout(2.0); t = time.monotonic()
try:
    s.connect(("127.0.0.1", 45999)); print("   45999: connected?! something is listening")
except ConnectionRefusedError:
    print(f"   45999: refused after {time.monotonic()-t:.3f}s (good: closed ports answer)")
except OSError as e:
    print(f"   45999: {type(e).__name__} after {time.monotonic()-t:.2f}s (BAD: this host does not refuse; the release check cannot pass here)")
finally:
    s.close()'
fi

HA=0
if [ -f "$ROOT/scripts/.ha-token" ] && [ -f "$ROOT/scripts/.ha-config.json" ]; then
  HA=1
  echo "== disabling HA entries"
  "$PY" "$ROOT/scripts/hub-bench/ha_entry.py" disable "X1S (" | tail -1
  "$PY" "$ROOT/scripts/hub-bench/ha_entry.py" disable "X1 ("  | tail -1
else
  echo "== no HA token files: make sure the X1 and X1S entries are DISABLED in Home Assistant now"
  read -r -p "   press Enter when both hubs are released..." _
fi

mkdir -p "$ROOT/scripts/hub-bench/out"
set +e
"$PY" -u "$ROOT/scripts/hub-bench/bench_200_server.py" "$TAG" 2>&1 | tee "$ROOT/scripts/hub-bench/out/bench_200_${TAG}.txt"
RC=${PIPESTATUS[0]}
set -e

if [ "$HA" = 1 ]; then
  echo "== re-enabling HA entries"
  "$PY" "$ROOT/scripts/hub-bench/ha_entry.py" enable "X1S (" | tail -1
  "$PY" "$ROOT/scripts/hub-bench/ha_entry.py" enable "X1 ("  | tail -1
else
  echo "== re-enable the X1 and X1S entries in Home Assistant now"
fi

echo "== done (exit $RC). Send back:"
echo "   scripts/hub-bench/out/bench_200_${TAG}.json"
echo "   scripts/hub-bench/out/bench_200_${TAG}.txt"
echo "   scripts/hub-bench/out/logs/bench_200_${TAG}-server.log"
