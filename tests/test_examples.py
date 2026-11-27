"""Smoke tests: the examples run against each other over LSL."""

from __future__ import annotations

import os
import py_compile
import subprocess
import sys
import threading
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.mark.parametrize("path", sorted(EXAMPLES.glob("*.py")), ids=lambda p: p.name)
def test_examples_compile(path: Path) -> None:
    py_compile.compile(str(path), doraise=True)


def run(script: str, *args: str, timeout: float = 30) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, str(EXAMPLES / script), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )


class Output:
    """Collect a process's output in the background and wait for a given line."""

    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.lines: list[str] = []
        self._changed = threading.Condition()
        self._thread = threading.Thread(target=self._read, args=(process,), daemon=True)
        self._thread.start()

    def _read(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            with self._changed:
                self.lines.append(line)
                self._changed.notify_all()

    def wait_for(self, text: str, timeout: float) -> bool:
        """Wait until a line contains ``text``; ``False`` after ``timeout`` seconds."""
        with self._changed:
            return self._changed.wait_for(lambda: any(text in x for x in self.lines), timeout)

    def text(self, timeout: float) -> str:
        """All the output, once the process has closed it."""
        self._thread.join(timeout)
        return "".join(self.lines)


def test_stream_synthetic_feeds_print_band_power() -> None:
    # Start the consumer only once the producer streams, and keep streaming well
    # past the 3 s the consumer needs: a busy machine can be slow to start either.
    producer = run("stream_synthetic.py", "--duration", "15", "--period", "1")
    producer_output = Output(producer)
    assert producer_output.wait_for("Streaming openmind-eeg", timeout=60), producer_output.text(5)
    consumer = run("print_band_power.py", "--duration", "3")
    consumer_out, _ = consumer.communicate(timeout=60)
    producer.wait(timeout=60)
    producer_out = producer_output.text(timeout=10)
    assert consumer.returncode == 0, consumer_out
    assert producer.returncode == 0, producer_out
    assert "state: left" in producer_out
    assert "Sent" in producer_out
    bars = [line for line in consumer_out.splitlines() if " C3 " in f" {line}"]
    assert len(bars) >= 2, consumer_out


@pytest.mark.slow
def test_record_and_replay(tmp_path: Path) -> None:
    process = subprocess.run(
        [sys.executable, str(EXAMPLES / "record_and_replay.py")],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "OPENMIND_DATA_DIR": str(tmp_path)},
    )
    assert process.returncode == 0, process.stdout + process.stderr
    assert "Replayed 2500 samples" in process.stdout
    assert list((tmp_path / "recordings" / "P00").glob("P00_*_raw.fif"))


def run_validation(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(EXAMPLES / "validate_ironbci.py"), *args],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "OPENMIND_DATA_DIR": str(tmp_path)},
    )


def test_validate_ironbci_runs_on_synthetic_source(tmp_path: Path) -> None:
    process = run_validation(
        tmp_path,
        "--source", "synthetic", "--subject", "P01", "--seed", "1", "--yes",
        "--duration", "3", "--phase", "2", "--clenches", "2",
        "--tap-window", "2", "--latency-seconds", "2",
    )  # fmt: skip
    assert process.returncode == 0, process.stdout + process.stderr
    reports = list((tmp_path / "validation").glob("P01_*_ironbci.md"))
    assert len(reports) == 1
    text = reports[0].read_text(encoding="utf-8")
    for item in (
        "Effective sampling rate",
        "Channel order | PASS",
        "Double jaw clench detected | PASS",
        "Latency to LSL | PASS",
        "Alpha test",
        "REF/BIAS",
    ):
        assert item in text, text


def test_validate_ironbci_refuses_a_path_inside_a_git_repository(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    process = run_validation(tmp_path / "data", "--yes", "--steps", "a")
    assert process.returncode == 2
    assert "Git repository" in process.stderr
    assert not (tmp_path / "data").exists()


def test_validate_ironbci_rejects_a_real_name(tmp_path: Path) -> None:
    process = run_validation(tmp_path, "--subject", "alice", "--yes")
    assert process.returncode == 2
    assert not (tmp_path / "validation").exists()
