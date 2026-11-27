"""IronBCI-8 source: the 8-channel BLE headset, as a standard stream.

.. warning::

   **Experimental.** This driver was written from the public documentation of
   the headset only (see ``docs/hardware/ironbci.md``), without the hardware.
   Several points could not be confirmed (gain, packet counter, channel order,
   clock drift) and stay unverified until the hardware validation task
   (PLUG-090).

The headset advertises over Bluetooth LE and sends ADS1299 samples as GATT
notifications on one characteristic: 8 channels x 3 bytes (24-bit, big-endian,
two's complement) per sample, 250 Hz. No cloud service or browser streaming is
involved: everything stays between the headset and this computer.

- :class:`PacketDecoder` turns notification payloads into microvolts. It is a
  pure class, tested without Bluetooth.
- :class:`IronBCISource` finds and connects to the headset with ``bleak`` and
  feeds the decoded samples to the standard source interface. The BLE scanner
  and client are injectable, so tests use a simulated headset.

Needs the ``ironbci`` extra: ``pip install "openmind-plug[ironbci]"``.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any, Final, Protocol

import numpy as np
import numpy.typing as npt
import pylsl

from openmind_plug.montage import Montage, get_montage
from openmind_plug.sources.base import (
    EEGSource,
    SampleArray,
    SourceStateError,
    StreamInfo,
    TimestampArray,
)

#: The driver has not been validated with a real headset yet (PLUG-090).
EXPERIMENTAL: Final[bool] = True

#: Install hint shown when the extra is missing.
IRONBCI_INSTALL: Final[str] = 'pip install "openmind-plug[ironbci]"'

#: GATT characteristic that notifies the samples.
DATA_CHARACTERISTIC: Final[str] = "0000fe42-8e22-4541-9d4c-21edae82ed19"

#: Advertised names of the headset (compared case-insensitively).
DEFAULT_BLE_NAMES: Final[tuple[str, ...]] = ("EAREEG", "IronBCI")

N_CHANNELS: Final[int] = 8
SAMPLE_RATE: Final[float] = 250.0
BYTES_PER_CHANNEL: Final[int] = 3
BYTES_PER_SAMPLE: Final[int] = N_CHANNELS * BYTES_PER_CHANNEL

#: Reference voltage of the ADS1299 as used by the official scripts, in volts.
DEFAULT_VREF: Final[float] = 4.5

#: Default montage: the planned IronBCI-8 wiring.
DEFAULT_MONTAGE: Final[str] = "C"

#: Seconds to look for the headset.
SCAN_TIMEOUT: Final[float] = 10.0

_FULL_SCALE: Final[int] = (1 << 24) - 1
_COUNTER_MODULO: Final[int] = 256

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class IronBCIConfig:
    """Every assumption the driver makes about the IronBCI-8, in one place.

    The defaults come from the public documentation and are **unverified** until
    the hardware validation (PLUG-090). The numbers refer to "Assumptions to check
    with the headset" in ``docs/hardware/ironbci.md``; once a value is measured,
    change it here and nowhere else.

    Attributes:
        ble_names: Advertised names to look for, compared case-insensitively
            (assumption 7).
        data_characteristic: GATT characteristic that notifies the samples.
        srate: Sampling rate of the device clock, used for timestamps
            (assumption 4).
        montage: Montage naming the 8 inputs in order, i.e. the channel order
            (assumption 5).
        reference: Reference of the recording, written in the stream metadata
            (assumption 6).
        gain: Divisor applied to the ADC counts (assumption 1).
        vref: Reference voltage of the ADS1299 in volts (assumption 1).
        counter: Whether payloads start with a one-byte packet counter
            (assumptions 2 and 3).
        scan_timeout: Seconds to look for the headset.

    Raises:
        ValueError: If a number is not positive or ``ble_names`` is empty.
    """

    ble_names: tuple[str, ...] = DEFAULT_BLE_NAMES
    data_characteristic: str = DATA_CHARACTERISTIC
    srate: float = SAMPLE_RATE
    montage: str | Montage = DEFAULT_MONTAGE
    reference: str = "unknown"
    gain: float = 1.0
    vref: float = DEFAULT_VREF
    counter: bool = False
    scan_timeout: float = SCAN_TIMEOUT

    def __post_init__(self) -> None:
        for name in ("srate", "gain", "vref", "scan_timeout"):
            if not getattr(self, name) > 0:
                value = getattr(self, name)
                raise ValueError(f"IronBCIConfig.{name} must be positive, got {value}")
        if not self.ble_names:
            raise ValueError("IronBCIConfig.ble_names must name at least one device")


class IronBCIError(RuntimeError):
    """Raised when the IronBCI-8 cannot be found, connected or decoded."""


@dataclass(frozen=True)
class DecodedPacket:
    """One decoded notification.

    Args:
        samples: Microvolts, shape ``(n_samples, 8)``.
        counter: Packet counter, or ``None`` when the layout has no counter.
        lost_packets: Packets missing between the previous packet and this one.
    """

    samples: SampleArray
    counter: int | None
    lost_packets: int


class PacketDecoder:
    """Decode IronBCI-8 notification payloads into microvolts.

    Documented layout: a payload holds one or more samples, each 8 channels of
    3 bytes (24-bit big-endian two's complement). The documentation mentions no
    packet counter. With ``counter=True`` the decoder assumes one unsigned byte
    in front of the samples, incremented by one per packet and wrapping at 256;
    this layout is an **assumption** to confirm with the hardware.

    The conversion is ``uV = counts * 1e6 * vref / (2**24 - 1) / gain``. The
    official scripts use ``vref=4.5`` and no gain (``gain=1``), i.e. about
    0.268 uV per count.

    Args:
        gain: Divisor applied to the counts (ADS1299 PGA gain, if known).
        vref: Reference voltage in volts.
        counter: Whether payloads start with a one-byte packet counter.

    Raises:
        ValueError: If ``gain`` or ``vref`` is not positive.
    """

    def __init__(
        self, gain: float = 1.0, vref: float = DEFAULT_VREF, counter: bool = False
    ) -> None:
        if not gain > 0 or not vref > 0:
            raise ValueError(f"gain and vref must be positive, got gain={gain}, vref={vref}")
        self._gain = float(gain)
        self._vref = float(vref)
        self._has_counter = counter
        self._last_counter: int | None = None
        self._lost = 0

    @property
    def uv_per_count(self) -> float:
        """Microvolts per ADC count."""
        return 1e6 * self._vref / _FULL_SCALE / self._gain

    @property
    def lost_packets(self) -> int:
        """Total number of packets detected as lost since the last reset."""
        return self._lost

    def reset(self) -> None:
        """Forget the counter and the lost-packet total (new connection)."""
        self._last_counter = None
        self._lost = 0

    def decode(self, payload: bytes | bytearray) -> DecodedPacket:
        """Decode one notification.

        Raises:
            ValueError: If the payload is empty or its size does not match the
                layout (a multiple of 24 bytes, after the counter if any).
        """
        header = 1 if self._has_counter else 0
        body = bytes(payload)[header:]
        if len(payload) < header or not body or len(body) % BYTES_PER_SAMPLE:
            raise ValueError(
                f"invalid IronBCI-8 payload of {len(payload)} bytes: expected "
                f"{header} counter byte(s) plus a multiple of {BYTES_PER_SAMPLE} bytes"
            )
        counter: int | None = None
        lost = 0
        if self._has_counter:
            counter = payload[0]
            if self._last_counter is not None:
                lost = (counter - self._last_counter - 1) % _COUNTER_MODULO
            self._last_counter = counter
            self._lost += lost
        raw = np.frombuffer(body, dtype=np.uint8).reshape(-1, N_CHANNELS, BYTES_PER_CHANNEL)
        counts = (
            (raw[..., 0].astype(np.int32) << 16)
            | (raw[..., 1].astype(np.int32) << 8)
            | raw[..., 2].astype(np.int32)
        )
        counts = np.where(counts >= 1 << 23, counts - (1 << 24), counts)
        samples = (counts * self.uv_per_count).astype(np.float32)
        return DecodedPacket(samples=samples, counter=counter, lost_packets=lost)


class BLEClient(Protocol):
    """The part of ``bleak.BleakClient`` that the source uses."""

    @property
    def is_connected(self) -> bool: ...
    async def connect(self) -> Any: ...
    async def disconnect(self) -> Any: ...
    async def start_notify(
        self, char_specifier: str, callback: Callable[[Any, bytearray], Any]
    ) -> None: ...
    async def stop_notify(self, char_specifier: str) -> None: ...


#: ``scanner(names, address, timeout)`` returns the address of the headset.
BLEScanner = Callable[[tuple[str, ...], str | None, float], Awaitable[str]]

#: ``client_factory(address)`` returns an unconnected client.
BLEClientFactory = Callable[[str], BLEClient]


@dataclass(frozen=True)
class _Chunk:
    arrival: float
    samples: SampleArray
    lost_samples: int


class IronBCISource(EEGSource):
    """Read the IronBCI-8 over Bluetooth LE.

    **Experimental** until hardware validation (PLUG-090). Timestamps are on
    the LSL clock: the first notification anchors the clock once, then each
    sample is stamped ``t0 + index / srate``; samples of lost packets (when a
    counter is available) advance the index without producing values, so the
    gap is visible downstream.

    The hardware assumptions (names, characteristic, rate, channel order,
    reference, scale, counter) come from ``config`` only; the keyword arguments
    below are shortcuts that replace one field of it.

    Args:
        name: Advertised BLE name to look for (replaces ``config.ble_names``).
        address: BLE address (a UUID on macOS); skips the scan.
        montage: Montage name or object naming the 8 inputs
            (``config.montage``).
        gain: Divisor applied to the counts (``config.gain``).
        vref: Reference voltage in volts (``config.vref``).
        counter: Whether payloads carry a packet counter (``config.counter``).
        scan_timeout: Seconds to look for the headset (``config.scan_timeout``).
        config: The driver's assumptions (default :class:`IronBCIConfig`).
        scanner: Replaces the ``bleak`` scanner (tests).
        client_factory: Replaces ``bleak.BleakClient`` (tests).
        clock: Replaces ``pylsl.local_clock`` (tests).

    Raises:
        ImportError: If ``bleak`` is needed and the ``ironbci`` extra is missing.
        ValueError: If the montage does not have 8 electrodes.
    """

    def __init__(
        self,
        name: str | None = None,
        address: str | None = None,
        montage: str | Montage | None = None,
        gain: float | None = None,
        vref: float | None = None,
        counter: bool | None = None,
        scan_timeout: float | None = None,
        *,
        config: IronBCIConfig | None = None,
        scanner: BLEScanner | None = None,
        client_factory: BLEClientFactory | None = None,
        clock: Callable[[], float] = pylsl.local_clock,
    ) -> None:
        super().__init__()
        overrides: dict[str, Any] = {
            "ble_names": None if name is None else (name,),
            "montage": montage,
            "gain": gain,
            "vref": vref,
            "counter": counter,
            "scan_timeout": scan_timeout,
        }
        self._config = replace(
            config or IronBCIConfig(),
            **{key: value for key, value in overrides.items() if value is not None},
        )
        cfg = self._config
        if scanner is None or client_factory is None:
            _import_bleak()  # fail early with the install hint
        self._scanner = scanner or _bleak_scan
        self._client_factory = client_factory or _bleak_client
        resolved = get_montage(cfg.montage) if isinstance(cfg.montage, str) else cfg.montage
        if resolved.n_channels != N_CHANNELS:
            raise ValueError(
                f"the IronBCI-8 has {N_CHANNELS} inputs; montage {resolved.name!r} "
                f"has {resolved.n_channels}"
            )
        self._address = address
        self._clock = clock
        self._decoder = PacketDecoder(gain=cfg.gain, vref=cfg.vref, counter=cfg.counter)
        self._info = StreamInfo(
            srate=cfg.srate,
            channels=resolved.channels(unit="uV"),
            manufacturer="IronBCI",
            model="IronBCI-8",
            driver="ironbci",
            reference=cfg.reference,
            montage=resolved.name,
        )
        self._queue: queue.Queue[_Chunk] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._malformed = 0
        self._t0: float | None = None
        self._index = 0
        self._pending_samples: list[SampleArray] = []
        self._pending_stamps: list[npt.NDArray[np.float64]] = []

    @property
    def info(self) -> StreamInfo:
        return self._info

    @property
    def config(self) -> IronBCIConfig:
        """The assumptions this source runs with."""
        return self._config

    @property
    def lost_packets(self) -> int:
        """Packets detected as lost (always 0 without a packet counter)."""
        return self._decoder.lost_packets

    @property
    def malformed_packets(self) -> int:
        """Notifications dropped because their size did not match the layout."""
        return self._malformed

    def _start(self) -> None:
        _log.warning(
            "IronBCI-8 driver is EXPERIMENTAL: written from the public documentation, "
            "not validated with the hardware yet (PLUG-090)"
        )
        self._decoder.reset()
        self._queue = queue.Queue()
        self._pending_samples, self._pending_stamps = [], []
        self._t0, self._index = None, 0
        self._malformed = 0
        self._error = None
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="ironbci-ble", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=self._config.scan_timeout + 30.0)
        if self._error is not None or not self._ready.is_set():
            error = self._error
            self._shutdown_thread()
            raise IronBCIError(f"cannot connect to the IronBCI-8: {error}") from error

    def _stop(self) -> None:
        self._shutdown_thread()

    def _shutdown_thread(self) -> None:
        loop, event, thread = self._loop, self._stop_event, self._thread
        if loop is not None and event is not None and not loop.is_closed():
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(event.set)
        if thread is not None:
            thread.join(timeout=10.0)
        self._thread = None

    def _run(self) -> None:
        try:
            asyncio.run(self._session())
        except BaseException as error:  # reported to the reading thread
            self._error = self._error or error
        finally:
            self._loop = None
            self._ready.set()

    async def _session(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        cfg = self._config
        address = await self._scanner(cfg.ble_names, self._address, cfg.scan_timeout)
        client = self._client_factory(address)
        await client.connect()
        try:
            await client.start_notify(cfg.data_characteristic, self._on_notify)
            self._ready.set()
            while not self._stop_event.is_set():
                if not client.is_connected:
                    self._error = ConnectionError("BLE connection to the IronBCI-8 was lost")
                    return
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=0.25)
        finally:
            with suppress(Exception):
                await client.stop_notify(cfg.data_characteristic)
            with suppress(Exception):
                await client.disconnect()

    def _on_notify(self, _sender: Any, data: bytearray) -> None:
        arrival = self._clock()
        try:
            packet = self._decoder.decode(data)
        except ValueError as error:
            self._malformed += 1
            _log.warning("dropping malformed IronBCI-8 notification: %s", error)
            return
        lost = packet.lost_packets * packet.samples.shape[0]
        self._queue.put(_Chunk(arrival, packet.samples, lost))

    def _read(self, max_samples: int) -> tuple[SampleArray, TimestampArray]:
        self._drain()
        if not self._pending_samples:
            if self._error is not None:
                raise SourceStateError(f"IronBCI-8 stream failed: {self._error}") from self._error
            return (
                np.empty((0, N_CHANNELS), dtype=np.float32),
                np.empty(0, dtype=np.float64),
            )
        samples = np.concatenate(self._pending_samples)
        stamps = np.concatenate(self._pending_stamps)
        self._pending_samples = [samples[max_samples:]] if len(samples) > max_samples else []
        self._pending_stamps = [stamps[max_samples:]] if len(stamps) > max_samples else []
        return samples[:max_samples], stamps[:max_samples]

    def _drain(self) -> None:
        srate = self._info.srate
        while True:
            try:
                chunk = self._queue.get_nowait()
            except queue.Empty:
                return
            n = chunk.samples.shape[0]
            if self._t0 is None:
                # Anchor once: a notification arrives right after its last sample.
                self._t0 = chunk.arrival - n / srate
            self._index += chunk.lost_samples
            stamps = self._t0 + (self._index + np.arange(n)) / srate
            self._index += n
            self._pending_samples.append(chunk.samples)
            self._pending_stamps.append(stamps)


def _import_bleak() -> Any:
    try:
        import bleak
    except ImportError as error:
        raise ImportError(
            f"the IronBCI source needs the ironbci extra (bleak): {IRONBCI_INSTALL}"
        ) from error
    return bleak


async def _bleak_scan(names: tuple[str, ...], address: str | None, timeout: float) -> str:
    """Return the address of the headset, scanning by name unless an address is given."""
    if address is not None:
        return address
    bleak = _import_bleak()
    accepted = {n.casefold() for n in names}

    def matches(device: Any, advertisement: Any) -> bool:
        found = device.name or getattr(advertisement, "local_name", None) or ""
        return bool(found.casefold() in accepted)

    device = await bleak.BleakScanner.find_device_by_filter(matches, timeout=timeout)
    if device is None:
        wanted = " or ".join(names)
        raise IronBCIError(
            f"no BLE device named {wanted} found in {timeout:g} s: is the headset on, "
            "charged, unplugged from the charger, and not connected to another app?"
        )
    return str(device.address)


def _bleak_client(address: str) -> BLEClient:
    client: BLEClient = _import_bleak().BleakClient(address)
    return client
