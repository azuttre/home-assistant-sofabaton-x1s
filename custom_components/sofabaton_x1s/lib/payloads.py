# payloads.py: the IR payload value type of the asyncio facade.
#
# ``IrPayload`` wraps one command's ``library_data`` body (the bytes the hub
# stores and replays; the same bytes ``play_ir_blob`` takes and a blob dump
# returns, without the replay-tail checksum byte). Its only jobs are to be
# built from the formats payloads circulate in and to produce the bundle
# row fields a ``sync_device`` command add needs. New source formats are new
# constructors, never new facade methods (phase 3 plan, W3).
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional, Sequence

from .blob_decoders import (
    build_raw_ir_blob_body,
    looks_like_descriptive_ir_blob,
    parse_pronto_hex,
)
from .commands import build_descriptive_ir_blob_body

__all__ = ["IrPayload", "MIN_PAYLOAD_BYTES"]

# The persist path refuses anything shorter (proxy_ir_blob.persist_ir_blob).
MIN_PAYLOAD_BYTES = 10
# Raw-blob layout: declared length (BE16), zeros, carrier Hz (BE16 at 6:8).
_RAW_CARRIER_OFFSET = slice(6, 8)
_DESCRIPTOR_OFFSET = 8

IrPayloadKind = Literal["raw", "descriptive"]


def _parse_hex(text: str) -> bytes:
    """Bytes from pasted hex: space/newline pairs, contiguous, commas, ``0x``."""

    tokens = str(text or "").replace(",", " ").split()
    cleaned = "".join(t[2:] if t.lower().startswith("0x") else t for t in tokens)
    if not cleaned:
        raise ValueError("empty hex payload")
    return bytes.fromhex(cleaned)


@dataclass(frozen=True)
class IrPayload:
    """One IR command payload as the hub stores it.

    ``blob`` is the ``library_data`` body. ``kind`` is ``"descriptive"`` for
    a protocol descriptor the hub renders itself (``P:NEC1 D:... F:...``)
    and ``"raw"`` for mark/space timings with a carrier.
    """

    blob: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.blob, (bytes, bytearray)):
            raise TypeError("IrPayload.blob must be bytes")
        object.__setattr__(self, "blob", bytes(self.blob))
        if len(self.blob) < MIN_PAYLOAD_BYTES:
            raise ValueError(
                f"payload too short ({len(self.blob)} bytes) to be a stored IR payload"
            )

    # -- constructors -----------------------------------------------------

    @classmethod
    def from_bytes(cls, blob: bytes) -> "IrPayload":
        """A body exactly as the hub stores it (a blob dump, a learn capture)."""

        return cls(bytes(blob))

    @classmethod
    def from_hex(cls, text: str) -> "IrPayload":
        """The hub body as hex text, in any of the layouts payloads circulate in."""

        return cls(_parse_hex(text))

    @classmethod
    def from_pronto(cls, text: str) -> "IrPayload":
        """A learned-format (``0000``) Pronto hex code."""

        timings, carrier_hz = parse_pronto_hex(text)
        return cls(build_raw_ir_blob_body(timings, carrier_hz))

    @classmethod
    def from_raw_timings(cls, timings_us: Sequence[int], carrier_hz: int) -> "IrPayload":
        """Alternating mark/space durations in microseconds, mark first, plus the carrier."""

        return cls(build_raw_ir_blob_body(timings_us, int(carrier_hz)))

    @classmethod
    def from_descriptor(cls, descriptor: str) -> "IrPayload":
        """A descriptive protocol line (``P:NEC1 D:4 S:5 F:21`` ...)."""

        return cls(build_descriptive_ir_blob_body(descriptor))

    # -- views -------------------------------------------------------------

    @property
    def kind(self) -> IrPayloadKind:
        return "descriptive" if looks_like_descriptive_ir_blob(self.blob) else "raw"

    @property
    def descriptor(self) -> Optional[str]:
        """The protocol descriptor of a descriptive payload, else None."""

        if self.kind != "descriptive":
            return None
        length = int.from_bytes(self.blob[0:2], "big")
        raw = self.blob[_DESCRIPTOR_OFFSET:_DESCRIPTOR_OFFSET + length]
        return raw.decode("ascii", errors="replace")

    @property
    def carrier_hz(self) -> Optional[int]:
        """The carrier of a raw payload, else None."""

        if self.kind != "raw":
            return None
        return int.from_bytes(self.blob[_RAW_CARRIER_OFFSET], "big") or None

    @property
    def hex(self) -> str:
        return self.blob.hex(" ")

    def to_command_row(self, command_id: int, name: str) -> dict[str, Any]:
        """The ``commands`` row a ``sync_device`` command add takes.

        Append it to the edited device's ``commands`` (with a command id not
        on the device yet) and sync; the planner recognises the
        ``restore_data.new`` marker and persists the payload.
        """

        restore_data: dict[str, Any] = {
            "transport": "hub_code_record",
            "library_type": 0x0D,
            "button_code": 0,
            "data_hex": self.blob.hex(),
            "new": True,
        }
        descriptor = self.descriptor
        if descriptor is not None:
            restore_data["decoded"] = {"class": "ir", "fields": {"descriptor": descriptor}}
        return {
            "command_id": int(command_id) & 0xFF,
            "name": str(name or "").strip() or f"Command {int(command_id) & 0xFF}",
            "restore_data": restore_data,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "hex": self.hex,
            "descriptor": self.descriptor,
            "carrier_hz": self.carrier_hz,
        }
