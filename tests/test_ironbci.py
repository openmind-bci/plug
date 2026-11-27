"""Tests for the IronBCI-8 driver, with a simulated headset (no Bluetooth)."""

from __future__ import annotations

import builtins
import logging
import sys
import time
from typing import Any

import numpy as np
import pytest

from fake_ironbci import FakeBLEEnvironment, build_packet, packet_stream
from openmind_plug.sources import EEGSource, SourceStateError
from openmind_plug.sources import ironbci as ironbci_module
from openmind_plug.sources.ironbci import (
    DATA_CHARACTERISTIC,
    EXPERIMENTAL,
    IronBCIConfig,
    IronBCIError,
    IronBCISource,
    PacketDecoder,
)

LSB_UV = 1e6 * 4.5 / (2**24 - 1)  # about 0.268 uV


def random_uv(n: int, seed: int = 0, scale: float = 200.0) -> np.ndarray[Any, Any]:
    return np.random.default_rng(seed).normal(0, scale, size=(n, 8))


class TestPacketDecoder:
    def test_round_trip_within_quantization(self) -> None:
        uv = random_uv(25)
        packet = PacketDecoder().decode(build_packet(uv))
        assert packet.samples.shape == (25, 8)
        assert packet.samples.dtype == np.float32
        assert packet.counter is None
        np.testing.assert_allclose(packet.samples, uv, atol=LSB_UV)

    def test_known_bytes(self) -> None:
        payload = bytes([0x00, 0x00, 0x01] + [0xFF, 0xFF, 0xFF] + [0x80, 0x00, 0x00] + [0] * 15)
        samples = PacketDecoder().decode(payload).samples[0]
        assert samples[0] == pytest.approx(LSB_UV, rel=1e-6)
        assert samples[1] == pytest.approx(-LSB_UV, rel=1e-6)  # 0xFFFFFF = -1 count
        assert samples[2] == pytest.approx(-(2**23) * LSB_UV, rel=1e-6)
        assert samples[3] == 0.0

    def test_negative_values_and_extremes(self) -> None:
        uv = np.full((1, 8), -1000.0)
        uv[0, 1] = 1000.0
        samples = PacketDecoder().decode(build_packet(uv)).samples
        np.testing.assert_allclose(samples, uv, atol=LSB_UV)

    def test_gain_divides_counts(self) -> None:
        payload = build_packet(random_uv(4, scale=50.0))
        base = PacketDecoder(gain=1.0).decode(payload).samples
        scaled = PacketDecoder(gain=24.0).decode(payload).samples
        np.testing.assert_allclose(scaled * 24.0, base, rtol=1e-5, atol=1e-5)
        assert PacketDecoder(gain=24.0).uv_per_count == pytest.approx(LSB_UV / 24.0)

    def test_vref(self) -> None:
        assert PacketDecoder(vref=2.25).uv_per_count == pytest.approx(LSB_UV / 2)

    @pytest.mark.parametrize("kwargs", [{"gain": 0}, {"gain": -1}, {"vref": 0}])
    def test_rejects_bad_scale(self, kwargs: dict[str, float]) -> None:
        with pytest.raises(ValueError, match="positive"):
            PacketDecoder(**kwargs)

    @pytest.mark.parametrize("size", [0, 1, 23, 25, 47])
    def test_rejects_bad_sizes(self, size: int) -> None:
        with pytest.raises(ValueError, match="invalid IronBCI-8 payload"):
            PacketDecoder().decode(bytes(size))

    def test_counter_layout(self) -> None:
        uv = random_uv(3)
        packet = PacketDecoder(counter=True).decode(build_packet(uv, counter=7))
        assert packet.counter == 7
        assert packet.lost_packets == 0
        np.testing.assert_allclose(packet.samples, uv, atol=LSB_UV)

    def test_counter_only_payload_is_invalid(self) -> None:
        with pytest.raises(ValueError):
            PacketDecoder(counter=True).decode(bytes([3]))

    def test_lost_packets_and_wrap_around(self) -> None:
        decoder = PacketDecoder(counter=True)
        block = random_uv(2)
        counters = [253, 254, 255, 0, 1, 4, 5]  # wraps, then 2 and 3 are missing
        lost = [decoder.decode(build_packet(block, counter=c)).lost_packets for c in counters]
        assert lost == [0, 0, 0, 0, 0, 2, 0]
        assert decoder.lost_packets == 2

    def test_lost_across_wrap(self) -> None:
        decoder = PacketDecoder(counter=True)
        decoder.decode(build_packet(random_uv(1), counter=254))
        assert decoder.decode(build_packet(random_uv(1), counter=1)).lost_packets == 2

    def test_reset(self) -> None:
        decoder = PacketDecoder(counter=True)
        decoder.decode(build_packet(random_uv(1), counter=1))
        decoder.decode(build_packet(random_uv(1), counter=9))
        assert decoder.lost_packets == 7
        decoder.reset()
        assert decoder.lost_packets == 0
        assert decoder.decode(build_packet(random_uv(1), counter=200)).lost_packets == 0


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_source(
    env: FakeBLEEnvironment, clock: FakeClock | None = None, **kwargs: Any
) -> IronBCISource:
    return IronBCISource(
        scanner=env.scanner,
        client_factory=env.client_factory,
        clock=clock or FakeClock(),
        **kwargs,
    )


def read_all(source: EEGSource, expected: int, timeout: float = 5.0) -> tuple[Any, Any]:
    samples, stamps = [], []
    deadline = time.monotonic() + timeout
    while sum(len(s) for s in samples) < expected and time.monotonic() < deadline:
        s, t = source.read(1000)
        if len(s):
            samples.append(s)
            stamps.append(t)
        else:
            time.sleep(0.01)
    if not samples:
        return np.empty((0, 8)), np.empty(0)
    return np.concatenate(samples), np.concatenate(stamps)


class TestSource:
    def test_info(self) -> None:
        source = make_source(FakeBLEEnvironment())
        info = source.info
        assert info.srate == 250.0
        assert info.model == "IronBCI-8"
        assert info.driver == "ironbci"
        assert info.montage == "C"
        assert info.labels == ("FC3", "FC4", "C3", "Cz", "C4", "CP3", "CP4", "CPz")
        assert all(c.unit == "uV" and c.type == "EEG" for c in info.channels)

    def test_experimental_flag(self) -> None:
        assert EXPERIMENTAL is True
        assert "EXPERIMENTAL" in (ironbci_module.__doc__ or "").upper()

    def test_rejects_montage_with_wrong_size(self) -> None:
        from openmind_plug.montage import Montage

        with pytest.raises(ValueError, match="8 inputs"):
            make_source(FakeBLEEnvironment(), montage=Montage("X", ("C3", "C4")))

    def test_end_to_end(self, caplog: pytest.LogCaptureFixture) -> None:
        env = FakeBLEEnvironment()
        clock = FakeClock(1000.0)
        source = make_source(env, clock, name="EAREEG", address="11:22")
        uv = random_uv(40)
        with caplog.at_level(logging.WARNING), source:
            assert source.is_running
            assert env.scan_args == (("EAREEG",), "11:22", ironbci_module.SCAN_TIMEOUT)
            client = env.client
            assert client is not None and client.address == "11:22"
            assert client.notify_char == DATA_CHARACTERISTIC
            for i in range(0, 40, 10):
                client.notify(build_packet(uv[i : i + 10]))
            samples, stamps = read_all(source, 40)
        assert "EXPERIMENTAL" in caplog.text
        assert samples.shape == (40, 8)
        np.testing.assert_allclose(samples, uv, atol=LSB_UV)
        # Anchored once on the first packet, then counted by sample index.
        assert stamps[0] == pytest.approx(1000.0 - 10 / 250.0)
        np.testing.assert_allclose(np.diff(stamps), 1 / 250.0)
        assert not source.is_running
        assert client.disconnected

    def test_read_respects_max_samples(self) -> None:
        env = FakeBLEEnvironment()
        with make_source(env) as source:
            assert env.client is not None
            env.client.notify(build_packet(random_uv(30)))
            deadline = time.monotonic() + 5
            first = np.empty((0, 8))
            while not len(first) and time.monotonic() < deadline:
                first, t1 = source.read(12)
                time.sleep(0.01)
            second, t2 = source.read(100)
        assert len(first) == 12 and len(second) == 18
        assert t2[0] == pytest.approx(t1[-1] + 1 / 250.0)

    def test_lost_packets_leave_a_timestamp_gap(self) -> None:
        env = FakeBLEEnvironment()
        blocks = [random_uv(10, seed=i) for i in range(6)]
        payloads = packet_stream(blocks, start_counter=254, drop=frozenset({2, 3}))
        source = make_source(env, counter=True)
        with source:
            assert env.client is not None
            for payload in payloads:
                env.client.notify(payload)
            samples, stamps = read_all(source, 40)
            assert source.lost_packets == 2
        assert samples.shape == (40, 8)
        gaps = np.diff(stamps)
        # 20 samples were lost between the second and third received packets.
        assert gaps[19] == pytest.approx(21 / 250.0)
        np.testing.assert_allclose(np.delete(gaps, 19), 1 / 250.0)

    def test_malformed_notification_is_dropped(self) -> None:
        env = FakeBLEEnvironment()
        with make_source(env) as source:
            assert env.client is not None
            env.client.notify(bytes(10))
            env.client.notify(build_packet(random_uv(2)))
            samples, _ = read_all(source, 2)
            assert source.malformed_packets == 1
        assert samples.shape == (2, 8)

    def test_read_before_start(self) -> None:
        source = make_source(FakeBLEEnvironment())
        with pytest.raises(SourceStateError):
            source.read(10)

    def test_restart_resets_the_clock_anchor(self) -> None:
        env = FakeBLEEnvironment()
        clock = FakeClock(10.0)
        source = make_source(env, clock)
        with source:
            assert env.client is not None
            env.client.notify(build_packet(random_uv(5)))
            _, first = read_all(source, 5)
        clock.now = 500.0
        with source:
            assert env.client is not None
            env.client.notify(build_packet(random_uv(5)))
            _, second = read_all(source, 5)
        assert second[0] == pytest.approx(500.0 - 5 / 250.0)
        assert first[0] == pytest.approx(10.0 - 5 / 250.0)

    def test_device_not_found(self) -> None:
        with pytest.raises(IronBCIError, match="cannot connect"):
            make_source(FakeBLEEnvironment(found=False)).start()

    def test_connection_refused(self) -> None:
        source = make_source(FakeBLEEnvironment(fail_connect=True))
        with pytest.raises(IronBCIError, match="connection refused"):
            source.start()
        assert not source.is_running

    def test_connection_lost_is_reported(self) -> None:
        env = FakeBLEEnvironment()
        with make_source(env) as source:
            assert env.client is not None
            env.client.is_connected = False
            deadline = time.monotonic() + 5
            with pytest.raises(SourceStateError, match="connection"):
                while time.monotonic() < deadline:
                    source.read(10)
                    time.sleep(0.05)


class TestMissingBleak:
    def test_clear_error_without_bleak(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "bleak", None)  # makes `import bleak` fail
        with pytest.raises(ImportError, match=r"openmind-plug\[ironbci\]"):
            IronBCISource()

    def test_injection_needs_no_bleak(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_import = builtins.__import__

        def no_bleak(name: str, *args: Any, **kwargs: Any) -> Any:
            if name.split(".")[0] == "bleak":
                raise ImportError("blocked")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_bleak)
        env = FakeBLEEnvironment()
        assert make_source(env).info.n_channels == 8


class TestBleakDefaults:
    def test_scan_by_address_needs_no_bleak(self) -> None:
        import asyncio

        assert asyncio.run(ironbci_module._bleak_scan(None, "AA:BB", 1.0)) == "AA:BB"

    def test_default_client_is_a_bleak_client(self) -> None:
        pytest.importorskip("bleak")
        client = ironbci_module._bleak_client("AA:BB:CC:DD:EE:FF")
        assert hasattr(client, "start_notify")


class TestConfig:
    """Every hardware assumption lives in IronBCIConfig (PLUG-090 checklist)."""

    def test_defaults_are_the_documented_assumptions(self) -> None:
        config = IronBCIConfig()
        assert config.ble_names == ("EAREEG", "IronBCI")
        assert config.data_characteristic == DATA_CHARACTERISTIC
        assert config.srate == 250.0
        assert config.montage == "C"
        assert config.reference == "unknown"
        assert (config.gain, config.vref, config.counter) == (1.0, 4.5, False)
        assert config.scan_timeout == ironbci_module.SCAN_TIMEOUT

    @pytest.mark.parametrize(
        "kwargs",
        [{"gain": 0.0}, {"vref": -1.0}, {"srate": 0.0}, {"ble_names": ()}, {"scan_timeout": 0.0}],
    )
    def test_invalid_values_are_refused(self, kwargs: dict[str, Any]) -> None:
        with pytest.raises(ValueError):
            IronBCIConfig(**kwargs)

    def test_source_uses_only_the_config(self) -> None:
        from openmind_plug.montage import Montage

        order = ("C4", "C3", "Cz", "FC3", "FC4", "CP3", "CP4", "CPz")
        config = IronBCIConfig(
            ble_names=("MyIron",),
            data_characteristic="0000aaaa-0000-1000-8000-00805f9b34fb",
            srate=200.0,
            montage=Montage("measured", order),
            reference="linked ears",
            gain=2.0,
            counter=True,
            scan_timeout=3.0,
        )
        env = FakeBLEEnvironment()
        clock = FakeClock(500.0)
        source = make_source(env, clock, config=config)
        assert source.config == config
        assert source.info.srate == 200.0
        assert source.info.labels == order
        assert source.info.reference == "linked ears"
        uv = random_uv(20)
        with source:
            assert env.scan_args == (("MyIron",), None, 3.0)
            client = env.client
            assert client is not None
            assert client.notify_char == "0000aaaa-0000-1000-8000-00805f9b34fb"
            client.notify(build_packet(uv, gain=2.0, counter=0))
            samples, stamps = read_all(source, 20)
        np.testing.assert_allclose(samples, uv, atol=LSB_UV)
        np.testing.assert_allclose(np.diff(stamps), 1 / 200.0)

    def test_keyword_arguments_override_the_config(self) -> None:
        env = FakeBLEEnvironment()
        source = make_source(env, config=IronBCIConfig(gain=4.0), name="Unit7", gain=2.0)
        assert source.config.ble_names == ("Unit7",)
        assert source.config.gain == 2.0
        with source:
            assert env.scan_args == (("Unit7",), None, ironbci_module.SCAN_TIMEOUT)
