"""Tests for the ``openmind-plug`` command line (stream, list-sources, keyboard)."""

from __future__ import annotations

import importlib.util
import threading
import uuid
from pathlib import Path

import pytest
from typer.testing import CliRunner

from openmind_plug.cli import SOURCES, SourceName, _source_status, _start_keyboard, app
from openmind_plug.keyboard import run_key_loop
from openmind_plug.sources.synthetic import SyntheticSource

runner = CliRunner()


def unique_id() -> str:
    return f"openmind-plug-test:{uuid.uuid4()}"


def test_list_sources_shows_every_source() -> None:
    result = runner.invoke(app, ["list-sources"])
    assert result.exit_code == 0
    for name in SourceName:
        assert name.value in result.output
    assert "experimental" in result.output or "needs" in result.output


def test_list_sources_aligns_descriptions_with_an_unavailable_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openmind_plug.cli as cli

    missing = cli._SourceEntry("Needs a missing module", "lsl", module="openmind_no_such_module")
    monkeypatch.setitem(cli.SOURCES, SourceName.lsl, missing)
    result = runner.invoke(app, ["list-sources"])
    assert result.exit_code == 0
    assert 'needs: pip install "openmind-plug[lsl]"' in result.output
    lines = result.output.splitlines()
    starts = {
        line.index(entry.description)
        for line, entry in zip(lines, cli.SOURCES.values(), strict=True)
    }
    assert len(starts) == 1, lines


def test_source_status() -> None:
    assert _source_status(SOURCES[SourceName.synthetic]) == "available"
    assert _source_status(SOURCES[SourceName.lsl]) == "available"
    expected_ble = "experimental" if importlib.util.find_spec("bleak") else "needs"
    assert _source_status(SOURCES[SourceName.ironbci]).startswith(expected_ble)
    expected = "available" if importlib.util.find_spec("brainflow") else "needs"
    assert _source_status(SOURCES[SourceName.brainflow]).startswith(expected)


@pytest.mark.parametrize("command", ["stream", "check-stream", "list-sources"])
def test_every_command_has_help(command: str) -> None:
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == 0
    assert "Usage" in result.output


def test_stream_synthetic_for_a_duration() -> None:
    source_id = unique_id()
    result = runner.invoke(
        app,
        [
            "stream",
            "-s",
            "synthetic",
            "--duration",
            "0.5",
            "--no-keyboard",
            "--seed",
            "1",
            "--source-id",
            source_id,
            "--montage",
            "b",
            "--blink-rate",
            "60",
        ],
    )
    assert result.exit_code == 0, result.output
    assert f"Streaming {source_id}" in result.output
    assert "C3, Cz, C4, CP3, CP4, Fp1, O1, O2" in result.output
    assert "Stopped after" in result.output


def test_stream_synthetic_subject_and_session() -> None:
    result = runner.invoke(
        app,
        [
            "stream", "-s", "synthetic", "--duration", "0.3", "--no-keyboard",
            "--source-id", unique_id(), "--subject", "3", "--session", "2",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "Synthetic subject 3, session 2" in result.output


def test_stream_synthetic_rejects_a_named_session() -> None:
    result = runner.invoke(
        app, ["stream", "-s", "synthetic", "--session", "session_T", "--no-keyboard"]
    )
    assert result.exit_code == 2
    assert "numbers" in result.output


def test_stream_replay_file_until_the_end(tmp_path: Path) -> None:
    mne = pytest.importorskip("mne")
    import numpy as np

    raw = mne.io.RawArray(
        np.zeros((3, 500)), mne.create_info(["C3", "Cz", "C4"], 250.0, "eeg"), verbose=False
    )
    path = tmp_path / "short_raw.fif"
    raw.save(path, verbose=False)
    result = runner.invoke(
        app,
        [
            "stream",
            "-s",
            "replay",
            "--file",
            str(path),
            "--speed",
            "20",
            "--no-keyboard",
            "--source-id",
            unique_id(),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "(end of recording): 500 samples" in result.output


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["-s", "synthetic", "--montage", "Z"], "unknown montage"),
        (["-s", "synthetic", "--srate", "50"], "srate"),
        (["-s", "synthetic", "--line-freq", "55"], "line_freq_hz"),
        (["-s", "replay"], "exactly one of --dataset or --file"),
    ],
)
def test_stream_reports_usage_errors(args: list[str], message: str) -> None:
    result = runner.invoke(app, ["stream", *args, "--no-keyboard"])
    assert result.exit_code == 2
    assert message in result.output


def test_stream_ironbci_without_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None)
    result = runner.invoke(app, ["stream", "-s", "ironbci", "--no-keyboard"])
    assert result.exit_code == 2
    assert "openmind-plug[ironbci]" in result.output


def test_stream_ironbci_reports_a_missing_headset(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types
    from importlib.machinery import ModuleSpec

    from openmind_plug.sources import ironbci

    fake_bleak = types.ModuleType("bleak")
    fake_bleak.__spec__ = ModuleSpec("bleak", None)
    fake_bleak.BleakClient = object  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "bleak", fake_bleak)

    async def not_found(names: tuple[str, ...], address: str | None, timeout: float) -> str:
        raise ironbci.IronBCIError("no BLE device named EAREEG found")

    monkeypatch.setattr(ironbci, "_bleak_scan", not_found)
    result = runner.invoke(app, ["stream", "-s", "ironbci", "--no-keyboard", "--duration", "1"])
    assert result.exit_code == 2
    assert "experimental" in result.output
    assert "no BLE device" in result.output


def test_stream_ironbci_options_go_through_the_config(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types
    from importlib.machinery import ModuleSpec

    from openmind_plug.sources import ironbci

    fake_bleak = types.ModuleType("bleak")
    fake_bleak.__spec__ = ModuleSpec("bleak", None)
    fake_bleak.BleakClient = object  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "bleak", fake_bleak)
    seen: list[ironbci.IronBCIConfig] = []
    real_init = ironbci.IronBCISource.__init__

    def spy_init(self: ironbci.IronBCISource, *args: object, **kwargs: object) -> None:
        real_init(self, *args, **kwargs)  # type: ignore[arg-type]
        seen.append(self.config)

    async def not_found(names: tuple[str, ...], address: str | None, timeout: float) -> str:
        raise ironbci.IronBCIError(f"no BLE device named {' or '.join(names)} found")

    monkeypatch.setattr(ironbci.IronBCISource, "__init__", spy_init)
    monkeypatch.setattr(ironbci, "_bleak_scan", not_found)
    result = runner.invoke(
        app,
        ["stream", "-s", "ironbci", "--ble-name", "Unit7", "--gain", "24", "--ble-counter",
         "--no-keyboard", "--duration", "1"],
    )  # fmt: skip
    assert result.exit_code == 2
    assert "no BLE device named Unit7" in result.output
    (config,) = seen
    assert config.ble_names == ("Unit7",)
    assert (config.gain, config.counter, config.montage) == (24.0, True, "C")


def test_stream_replay_unknown_dataset() -> None:
    pytest.importorskip("moabb")
    result = runner.invoke(app, ["stream", "-s", "replay", "--dataset", "nope", "--no-keyboard"])
    assert result.exit_code == 2
    assert "unknown dataset" in result.output


def test_stream_replay_dataset_without_moabb(monkeypatch: pytest.MonkeyPatch) -> None:
    import openmind_plug.cli as cli

    monkeypatch.setattr(cli, "_datasets_available", lambda: False)
    result = runner.invoke(
        app, ["stream", "-s", "replay", "--dataset", "bnci2014_001", "--no-keyboard"]
    )
    assert result.exit_code == 2
    assert 'pip install "openmind-plug[replay]"' in result.output

    listing = runner.invoke(app, ["list-sources"])
    assert "datasets need" in listing.output


def test_stream_replay_missing_channels(tmp_path: Path) -> None:
    mne = pytest.importorskip("mne")
    import numpy as np

    raw = mne.io.RawArray(
        np.zeros((2, 250)), mne.create_info(["C3", "C4"], 250.0, "eeg"), verbose=False
    )
    path = tmp_path / "two_raw.fif"
    raw.save(path, verbose=False)
    result = runner.invoke(
        app, ["stream", "-s", "replay", "--file", str(path), "-m", "A", "--no-keyboard"]
    )
    assert result.exit_code == 2
    assert "omit --montage" in result.output


# --- Keyboard -------------------------------------------------------------------


def test_run_key_loop_calls_handler_until_false() -> None:
    keys = iter(["a", None, "b", "q", "z"])
    seen: list[str] = []
    stop = threading.Event()

    def on_key(key: str) -> bool:
        seen.append(key)
        return key != "q"

    run_key_loop(lambda timeout: next(keys), on_key, stop)
    assert seen == ["a", "b", "q"]
    assert stop.is_set()


def test_keyboard_drives_synthetic_source() -> None:
    source = SyntheticSource(seed=0)
    keys = iter(["a", "e", "x", "q"])
    done = threading.Event()

    def read(timeout: float) -> str | None:
        key = next(keys, None)
        if key is None:
            done.wait(timeout)
        return key

    stop = threading.Event()
    _start_keyboard(source, read, stop)
    assert stop.wait(5), "q should stop the stream"
    assert source.state == "left"
    assert source.eyes == "closed"
