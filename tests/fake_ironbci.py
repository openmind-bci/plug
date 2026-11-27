"""A simulated IronBCI-8 for tests: packet builder and fake BLE client.

Packets follow the documented layout (8 channels x 3 bytes, 24-bit big-endian
two's complement), with the optional one-byte counter that PacketDecoder can
be told to expect.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

import numpy as np
import numpy.typing as npt

from openmind_plug.sources.ironbci import DEFAULT_VREF

FloatArray = npt.NDArray[np.floating[Any]]


def encode_samples(uv: FloatArray, gain: float = 1.0, vref: float = DEFAULT_VREF) -> bytes:
    """Encode microvolts, shape ``(n, 8)``, as 24-bit big-endian two's complement."""
    counts = np.rint(np.asarray(uv, dtype=np.float64) / (1e6 * vref / (2**24 - 1) / gain))
    counts = counts.astype(np.int64) & 0xFFFFFF
    out = np.empty((*counts.shape, 3), dtype=np.uint8)
    out[..., 0] = (counts >> 16) & 0xFF
    out[..., 1] = (counts >> 8) & 0xFF
    out[..., 2] = counts & 0xFF
    return out.tobytes()


def build_packet(
    uv: FloatArray,
    counter: int | None = None,
    gain: float = 1.0,
    vref: float = DEFAULT_VREF,
) -> bytes:
    """Build one notification payload, with a counter byte if ``counter`` is given."""
    body = encode_samples(uv, gain=gain, vref=vref)
    return body if counter is None else bytes([counter % 256]) + body


def packet_stream(
    blocks: Iterable[FloatArray],
    start_counter: int | None = 0,
    drop: frozenset[int] = frozenset(),
    gain: float = 1.0,
) -> list[bytes]:
    """Build payloads for ``blocks``; indices in ``drop`` are numbered but not returned."""
    payloads: list[bytes] = []
    for i, block in enumerate(blocks):
        counter = None if start_counter is None else start_counter + i
        if i not in drop:
            payloads.append(build_packet(block, counter=counter, gain=gain))
    return payloads


class FakeBLEClient:
    """Stands in for ``bleak.BleakClient``; tests call :meth:`notify`."""

    def __init__(self, address: str, fail_connect: bool = False) -> None:
        self.address = address
        self.fail_connect = fail_connect
        self.is_connected = False
        self.callback: Callable[[Any, bytearray], Any] | None = None
        self.notify_char: str | None = None
        self.disconnected = False

    async def connect(self) -> None:
        if self.fail_connect:
            raise OSError("connection refused")
        self.is_connected = True

    async def disconnect(self) -> None:
        self.is_connected = False
        self.disconnected = True

    async def start_notify(
        self, char_specifier: str, callback: Callable[[Any, bytearray], Any]
    ) -> None:
        self.notify_char = char_specifier
        self.callback = callback

    async def stop_notify(self, char_specifier: str) -> None:
        self.callback = None

    def notify(self, payload: bytes) -> None:
        """Deliver one notification, as the BLE stack would."""
        assert self.callback is not None, "not subscribed"
        self.callback(None, bytearray(payload))


class FakeBLEEnvironment:
    """Scanner and client factory sharing one fake client."""

    def __init__(self, found: bool = True, fail_connect: bool = False) -> None:
        self.found = found
        self.fail_connect = fail_connect
        self.client: FakeBLEClient | None = None
        self.scan_args: tuple[tuple[str, ...], str | None, float] | None = None

    async def scanner(self, names: tuple[str, ...], address: str | None, timeout: float) -> str:
        self.scan_args = (names, address, timeout)
        if not self.found:
            raise RuntimeError("device not found")
        return address or "AA:BB:CC:DD:EE:FF"

    def client_factory(self, address: str) -> FakeBLEClient:
        self.client = FakeBLEClient(address, fail_connect=self.fail_connect)
        return self.client
