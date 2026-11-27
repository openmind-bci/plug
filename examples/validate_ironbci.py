"""Guided hardware validation of the IronBCI-8 (PLUG-090), rehearsable without it.

The same script runs today against the synthetic source (montage C) and, once the
headset has arrived, against the real one::

    uv run python examples/validate_ironbci.py --source synthetic --subject P01
    uv run python examples/validate_ironbci.py --source ironbci --subject P01 \
        [--ble-name NAME | --ble-address ADDRESS] [--ble-counter]

Steps (select some with ``--steps a,b``; default: all):

a. connection (Wi-Fi off, battery only) and device information;
b. effective sampling rate and packet loss over ``--duration`` seconds (600 s);
c. channel order: touch the electrodes one by one, the channel with the largest
   artifact is compared with the expected label;
d. alpha test, eyes open then closed (weak on the motor montage C, which has no
   posterior electrode: move one electrode to Pz/O1 or use montage B for it);
e. jaw clench: "clench twice" ``--clenches`` times, bursts are counted;
f. latency from the driver to LSL.

The result is a Markdown report in ``$OPENMIND_DATA_DIR/validation/`` (default
``~/openmind-data``), never inside a Git repository. The subject is a pseudonym
(``P01``), never a name. No EEG samples are stored, only figures. The report table
mirrors the checklist of ``docs/hardware/ironbci.md``.

The jaw detector here is a rough high-frequency envelope threshold, enough to
validate the hardware; the reference detector lives in OpenMind Core.
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import socket
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import numpy as np
import numpy.typing as npt
import pylsl
from scipy import signal

from openmind_plug import __version__
from openmind_plug.inlet import EEGInlet
from openmind_plug.outlet import EEGOutlet
from openmind_plug.paths import data_dir
from openmind_plug.quality import ALPHA_MIN_RELIABLE_PHASE_S, alpha_test, assess
from openmind_plug.recorder import validate_subject
from openmind_plug.sources.base import EEGSource, Marker
from openmind_plug.sources.synthetic import SyntheticSource

Array = npt.NDArray[np.float64]

STEPS: Final = ("a", "b", "c", "d", "e", "f")
#: Effective rate within this fraction of nominal passes; twice this warns.
RATE_TOLERANCE: Final = 0.02
#: 95th percentile of the driver-to-LSL delay (s) below which latency passes.
LATENCY_PASS_S: Final = 0.25
#: Seconds ignored at the start of the latency measurement.
LATENCY_WARMUP_S: Final = 1.0
#: Jaw detector: band, envelope smoothing, threshold in robust standard deviations.
JAW_BAND_HZ: Final = 30.0
JAW_SMOOTH_S: Final = 0.05
JAW_THRESHOLD_MAD: Final = 8.0
JAW_MIN_BURST_S: Final = 0.08
JAW_MERGE_GAP_S: Final = 0.15
#: A channel tap must stand out this many robust standard deviations of its own noise.
TAP_MIN_SCORE: Final = 8.0


@dataclass
class Result:
    """One line of the report: a checklist item and its outcome."""

    item: str
    status: str  # PASS, FAIL, WARN, SKIP or MANUAL
    detail: str
    step: str = ""


@dataclass
class Session:
    """Everything the steps share."""

    source: EEGSource
    yes: bool
    simulated: bool
    results: list[Result] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, step: str, item: str, status: str, detail: str) -> None:
        print(f"  [{status}] {item}: {detail}")
        self.results.append(Result(item, status, detail, step))

    def ask(self, message: str) -> None:
        """Prompt the user (answered automatically with ``--yes``)."""
        if self.yes:
            print(f"  > {message} (auto)")
        else:
            input(f"  > {message} [Enter] ")

    @property
    def labels(self) -> tuple[str, ...]:
        return self.source.info.labels

    @property
    def srate(self) -> float:
        return self.source.info.srate


# --- acquisition helpers ---------------------------------------------------------


def collect(
    source: EEGSource,
    seconds: float,
    progress: Callable[[float], None] | None = None,
) -> tuple[Array, Array, Array]:
    """Read ``source`` for ``seconds``.

    Returns:
        ``(samples, timestamps, arrivals)``: microvolts ``(n, channels)``, the
        sample timestamps and, per sample, the LSL clock time when it was read.
    """
    chunks: list[npt.NDArray[np.float32]] = []
    stamps: list[Array] = []
    arrivals: list[Array] = []
    start = pylsl.local_clock()
    last_report = start
    while (now := pylsl.local_clock()) - start < seconds:
        samples, timestamps = source.read(1024)
        if samples.shape[0]:
            chunks.append(samples)
            stamps.append(timestamps)
            arrivals.append(np.full(samples.shape[0], pylsl.local_clock()))
        if progress is not None and now - last_report >= 60.0:
            last_report = now
            progress(now - start)
        time.sleep(0.02)
    n_channels = source.info.n_channels
    if not chunks:
        return np.zeros((0, n_channels)), np.zeros(0), np.zeros(0)
    return (
        np.vstack(chunks).astype(np.float64),
        np.concatenate(stamps),
        np.concatenate(arrivals),
    )


# --- step a: connection ------------------------------------------------------------


def _internet_reachable() -> bool:
    """Whether the machine has a route to the internet, without sending anything.

    ``connect`` on a UDP socket only asks the operating system for a route to
    the address: no packet leaves the machine. It fails when there is no route
    (Wi-Fi, Ethernet and mobile data off).
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("1.1.1.1", 53))
            return True
    except OSError:
        return False


def step_connection(session: Session) -> None:
    print("Step a - connection and device information")
    session.ask("Turn Wi-Fi AND mobile data off, run the headset on its battery only")
    if session.simulated:
        session.add("a", "Connection without internet", "SKIP", "synthetic source, no Bluetooth")
    elif _internet_reachable():
        session.add(
            "a",
            "Connection without internet",
            "WARN",
            "internet still reachable: cut Wi-Fi/Ethernet and run step a again",
        )
    else:
        session.add("a", "Connection without internet", "PASS", "no internet route while streaming")

    began = pylsl.local_clock()
    session.source.start()
    deadline = began + 15.0
    got = 0
    while got == 0 and pylsl.local_clock() < deadline:
        got = session.source.read(256)[0].shape[0]
        time.sleep(0.05)
    delay = pylsl.local_clock() - began
    info = session.source.info
    session.notes.append(
        f"Device: {info.manufacturer} {info.model}, driver `{info.driver}`, "
        f"{info.srate:g} Hz nominal, montage {info.montage}, reference {info.reference}, "
        f"channels {', '.join(info.labels)}."
    )
    impedances = session.source.impedances()
    if impedances:
        session.notes.append(f"Impedances (kOhm): {impedances}")
    if got:
        session.add("a", "First samples", "PASS", f"received {delay:.1f} s after start")
    else:
        session.add("a", "First samples", "FAIL", "no sample within 15 s of connecting")


# --- step b: rate and packet loss ----------------------------------------------------


def step_rate(session: Session, duration: float) -> None:
    print(f"Step b - sampling rate and packet loss over {duration:g} s")
    session.ask("Keep the headset still and close to the computer")
    lost_before = int(getattr(session.source, "lost_packets", 0))
    bad_before = int(getattr(session.source, "malformed_packets", 0))
    samples, stamps, arrivals = collect(
        session.source,
        duration,
        lambda elapsed: print(f"    ... {elapsed:.0f} s / {duration:g} s"),
    )
    n = samples.shape[0]
    if n < 3:
        session.add("b", "Effective sampling rate", "FAIL", f"only {n} samples received")
        return
    nominal = session.srate
    # Arrival-based rate: timestamps of some drivers are derived from the nominal
    # rate and could not reveal a wrong clock.
    wall = float(arrivals[-1] - arrivals[0])
    first_burst = int(np.sum(arrivals == arrivals[0]))
    rate = (n - first_burst) / wall if wall > 0 else math.nan
    deviation = abs(rate - nominal) / nominal
    status = (
        "PASS"
        if deviation <= RATE_TOLERANCE
        else "WARN"
        if deviation <= 2 * RATE_TOLERANCE
        else "FAIL"
    )
    session.add(
        "b",
        "Effective sampling rate",
        status,
        f"{rate:.2f} Hz over {wall:.0f} s ({deviation:+.2%} vs {nominal:g} Hz; {n} samples)",
    )
    gaps = np.diff(stamps)
    big = gaps > 1.5 / nominal
    lag = arrivals - stamps
    edge = max(int(nominal), 1)  # compare the first and last second
    drift_ms = float((np.median(lag[-edge:]) - np.median(lag[:edge])) * 1000)
    per_10_min = drift_ms * 600 / max(wall, 1.0)
    session.add(
        "b",
        "Timestamp gaps",
        "PASS" if not big.any() else "WARN",
        f"{int(big.sum())} gap(s) > 1.5 samples, largest {float(np.max(gaps)) * 1000:.1f} ms; "
        f"arrival-vs-timestamp drift {drift_ms:+.1f} ms ({per_10_min:+.1f} ms per 10 min)",
    )
    lost = int(getattr(session.source, "lost_packets", 0)) - lost_before
    bad = int(getattr(session.source, "malformed_packets", 0)) - bad_before
    counter = hasattr(session.source, "lost_packets")
    if counter:
        session.add(
            "b",
            "Lost packets",
            "PASS" if lost == 0 and bad == 0 else "FAIL",
            f"{lost} lost, {bad} malformed (lost packets are only counted with "
            "--ble-counter; without it only the rate and gaps above tell)",
        )
    else:
        session.add("b", "Lost packets", "SKIP", "this source reports no packet counter")


# --- step c: channel order -------------------------------------------------------------


def tap_scores(window: Array, srate: float) -> Array:
    """Largest excursion of each channel over its own robust noise level.

    The window is high-passed at 1 Hz; the score is ``max |x| / (1.4826 MAD)``.
    """
    sos = signal.butter(2, 1.0, btype="highpass", fs=srate, output="sos")
    filtered = np.asarray(signal.sosfiltfilt(sos, window - window.mean(axis=0), axis=0))
    mad = np.median(np.abs(filtered - np.median(filtered, axis=0)), axis=0)
    return np.asarray(np.max(np.abs(filtered), axis=0) / np.maximum(1.4826 * mad, 1e-9))


def step_channel_order(session: Session, window_s: float) -> None:
    print("Step c - channel order (touch each electrode when asked)")
    labels = session.labels
    detected: list[str] = []
    for index, label in enumerate(labels):
        session.ask(
            f"Tap firmly on electrode {label} (input {index + 1}) during the next {window_s:g} s"
        )
        window, _, _ = collect(session.source, window_s)
        if session.simulated and window.shape[0]:
            # The synthetic source has no electrode to touch: add a tap-like transient
            # on the expected channel so that the detector itself is exercised.
            at = int(window.shape[0] * 0.4)
            ramp = np.exp(-np.arange(window.shape[0] - at) / (0.05 * session.srate))
            window[at:, index] += 500.0 * ramp
        if window.shape[0] < session.srate:
            detected.append("?")
            continue
        scores = tap_scores(window, session.srate)
        best = int(np.argmax(scores))
        ok = scores[best] >= TAP_MIN_SCORE
        detected.append(labels[best] if ok else "?")
        print(f"    strongest: {labels[best]} (score {scores[best]:.0f})")
    wrong = [
        f"input {i + 1}: expected {e}, saw {d}"
        for i, (e, d) in enumerate(zip(labels, detected, strict=True))
        if e != d
    ]
    suffix = " (taps simulated by the script)" if session.simulated else ""
    if not wrong:
        session.add(
            "c",
            "Channel order",
            "PASS",
            f"all {len(labels)} inputs match {', '.join(labels)}{suffix}",
        )
    else:
        session.add(
            "c", "Channel order", "FAIL", "; ".join(wrong) + " - fix the label order in the driver"
        )


# --- step d: alpha ----------------------------------------------------------------------


def step_alpha(session: Session, phase_s: float) -> None:
    print(f"Step d - alpha test ({phase_s:g} s eyes open, {phase_s:g} s eyes closed)")
    simulated = session.source if isinstance(session.source, SyntheticSource) else None
    if simulated is not None:
        simulated.set_eyes("open")
    session.ask(f"Eyes OPEN, look at a fixed point, relax for {phase_s:g} s")
    opened, _, _ = collect(session.source, phase_s)
    if simulated is not None:
        simulated.set_eyes("closed")
    session.ask(f"Eyes CLOSED, relax for {phase_s:g} s")
    closed, _, _ = collect(session.source, phase_s)
    if simulated is not None:
        simulated.set_eyes("open")
    if min(opened.shape[0], closed.shape[0]) < session.srate * 2:
        session.add("d", "Alpha test", "FAIL", "not enough samples")
        return
    report = alpha_test(opened, closed, session.srate, session.labels)
    detail = f"best x{report.best_gain:.2f} on {report.best_channel} (threshold x2)"
    if phase_s < ALPHA_MIN_RELIABLE_PHASE_S:
        session.add("d", "Alpha test", "SKIP", f"phase shorter than 20 s, not reliable; {detail}")
    elif report.passed:
        session.add("d", "Alpha test", "PASS", detail)
    else:
        session.add(
            "d",
            "Alpha test",
            "WARN",
            f"{detail}; expected on the motor montage C (no posterior electrode): "
            "move one electrode to Pz/O1 and repeat before concluding",
        )


# --- step e: jaw clench ----------------------------------------------------------------


def jaw_envelope(window: Array, srate: float) -> Array:
    """Smoothed high-frequency envelope, averaged over the channels."""
    sos = signal.butter(4, JAW_BAND_HZ, btype="highpass", fs=srate, output="sos")
    high = np.asarray(signal.sosfiltfilt(sos, window - window.mean(axis=0), axis=0))
    width = max(int(JAW_SMOOTH_S * srate), 1)
    return np.asarray(np.convolve(np.abs(high).mean(axis=1), np.ones(width) / width, mode="same"))


def count_bursts(envelope: Array, srate: float, baseline: Array | None = None) -> int:
    """Count the bursts of a jaw envelope above a robust threshold.

    The threshold is ``median + 8 x 1.4826 MAD`` of ``baseline`` (a quiet period,
    default: the envelope itself). Bursts shorter than 80 ms are ignored and bursts
    closer than 150 ms are merged.
    """
    ref = envelope if baseline is None else baseline
    median = float(np.median(ref))
    sigma = 1.4826 * float(np.median(np.abs(ref - median)))
    above = envelope > median + JAW_THRESHOLD_MAD * max(sigma, 1e-9)
    edges = np.flatnonzero(np.diff(np.r_[0, above.astype(int), 0]))
    starts, ends = edges[::2], edges[1::2]
    bursts: list[list[int]] = []
    for start, end in zip(starts, ends, strict=True):
        if (end - start) / srate < JAW_MIN_BURST_S:
            continue
        if bursts and (start - bursts[-1][1]) / srate < JAW_MERGE_GAP_S:
            bursts[-1][1] = int(end)
        else:
            bursts.append([int(start), int(end)])
    return len(bursts)


def step_jaw(session: Session, rounds: int) -> None:
    print(f"Step e - jaw clench ({rounds} double clenches; rough check)")
    simulated = session.source if isinstance(session.source, SyntheticSource) else None
    session.ask("Relax the jaw and the face for 5 s: this sets the noise baseline")
    rest, _, _ = collect(session.source, 5.0)
    if rest.shape[0] < session.srate * 2:
        session.add("e", "Jaw clench", "FAIL", "not enough samples")
        return
    baseline = jaw_envelope(rest, session.srate)
    exact = 0
    detected_per_round: list[int] = []
    channels_hit = np.zeros(len(session.labels), dtype=int)
    for index in range(rounds):
        session.ask(f"Round {index + 1}/{rounds}: when the window opens, clench the jaw TWICE")
        if simulated is not None:
            simulated.clench(2)
        window, _, _ = collect(session.source, 4.0)
        count = count_bursts(jaw_envelope(window, session.srate), session.srate, baseline)
        detected_per_round.append(count)
        exact += count == 2
        print(f"    bursts detected: {count}")
        for ch in range(window.shape[1]):
            channel_env = jaw_envelope(window[:, [ch]], session.srate)
            channel_base = jaw_envelope(rest[:, [ch]], session.srate)
            channels_hit[ch] += count_bursts(channel_env, session.srate, channel_base) >= 2
    needed = math.ceil(0.8 * rounds)
    session.add(
        "e",
        "Double jaw clench detected",
        "PASS" if exact >= needed else "FAIL",
        f"exactly 2 bursts in {exact}/{rounds} rounds (need {needed}); counts {detected_per_round}",
    )
    per_channel = ", ".join(
        f"{label} {hits}/{rounds}" for label, hits in zip(session.labels, channels_hit, strict=True)
    )
    weak = [
        label for label, hits in zip(session.labels, channels_hit, strict=True) if hits < needed
    ]
    session.add(
        "e",
        "Jaw visible on all 8 electrodes",
        "PASS" if not weak else "WARN",
        f"rounds with 2+ bursts per channel: {per_channel}"
        + (f"; weak: {', '.join(weak)} (check contact and REF/BIAS placement)" if weak else ""),
    )


# --- step f: latency ---------------------------------------------------------------------


def step_latency(session: Session, seconds: float) -> None:
    print(f"Step f - driver to LSL latency over {seconds:g} s")
    sample_ages: list[Array] = []
    marker_ages: list[float] = []
    outlet_id = "openmind-plug:validate-ironbci"
    with (
        EEGOutlet(session.source.info, source_id=outlet_id) as outlet,
        EEGInlet(timeout=10, source_id=outlet_id) as inlet,
    ):
        # The first second is a warm-up: the inlet connection delivers a backlog that
        # is not latency. Only samples stamped after it count.
        loop_start = pylsl.local_clock() + LATENCY_WARMUP_S
        end = loop_start + seconds
        next_marker = pylsl.local_clock() + 0.5
        while (now := pylsl.local_clock()) < end:
            samples, stamps = session.source.read(1024)
            outlet.push(samples, stamps)
            if now >= next_marker:
                outlet.push_markers(
                    [Marker(pylsl.local_clock(), {"type": "state", "label": "latency"})]
                )
                next_marker = now + 0.5
            _, got_stamps = inlet.read(timeout=0.0)
            got_stamps = got_stamps[got_stamps >= loop_start]
            if got_stamps.size:
                sample_ages.append(pylsl.local_clock() - got_stamps)
            marker_ages += [pylsl.local_clock() - m.timestamp for m in inlet.read_markers()]
        time.sleep(0.2)
        _, got_stamps = inlet.read(timeout=0.0)
        got_stamps = got_stamps[got_stamps >= loop_start]
        if got_stamps.size:
            sample_ages.append(pylsl.local_clock() - got_stamps)
    if not sample_ages:
        session.add("f", "Latency to LSL", "FAIL", "no sample came back through LSL")
        return
    ages = np.concatenate(sample_ages) * 1000
    p50, p95, worst = (float(np.percentile(ages, q)) for q in (50, 95, 100))
    detail = (
        f"sample age at the LSL inlet: median {p50:.0f} ms, p95 {p95:.0f} ms, max {worst:.0f} ms"
    )
    if marker_ages:
        detail += f"; marker transit median {float(np.median(marker_ages)) * 1000:.1f} ms"
    detail += (
        " (measures driver buffering + LSL transport; for a BLE driver whose timestamps are "
        "anchored on arrival it does NOT include the Bluetooth transit)"
    )
    status = "PASS" if p95 / 1000 <= LATENCY_PASS_S else "WARN"
    session.add("f", "Latency to LSL", status, detail)


# --- report -------------------------------------------------------------------------------

MANUAL_ROWS: Final = (
    (
        "REF/BIAS pin-out confirmed",
        "Check the ear-clip wiring against the vendor diagram; note the placement used.",
    ),
    (
        "Blink artifact (Fp1, or FC3+FC4 fallback)",
        "Blink 10 times and look at `openmind-plug stream` plus a viewer.",
    ),
    (
        "No false jaw triggers",
        "Talk 1 min, swallow, drink, chew; count the false bursts with step e's detector or Core.",
    ),
    (
        "Effect of REF/BIAS position on the jaw check",
        "Run step e again with another placement, using `--label`.",
    ),
)


def inside_git_repository(path: Path) -> bool:
    """Whether ``path`` would be written inside a Git working tree."""
    return any((parent / ".git").exists() for parent in path.resolve().parents)


def report_path(subject: str, label: str, now: dt.datetime) -> Path:
    suffix = f"_{label}" if label else ""
    name = f"{subject}_{now:%Y%m%d-%H%M%S}{suffix}_ironbci.md"
    return data_dir() / "validation" / name


def render(session: Session, args: argparse.Namespace, now: dt.datetime, steps: list[str]) -> str:
    lines = [
        f"# IronBCI-8 validation - {args.subject}{' - ' + args.label if args.label else ''}",
        "",
        f"- Date: {now:%Y-%m-%d %H:%M}",
        f"- Source: `{args.source}`"
        + (" (rehearsal, not the headset)" if session.simulated else ""),
        f"- openmind-plug: {__version__}",
        f"- Steps run: {', '.join(steps)}; duration {args.duration:g} s",
        "- Research and experimentation project, not a medical device.",
        "",
        "## Results",
        "",
        "| Step | Checklist item | Status | Detail |",
        "|---|---|---|---|",
    ]
    for result in session.results:
        detail = result.detail.replace("|", "/")
        lines.append(f"| {result.step} | {result.item} | {result.status} | {detail} |")
    for item, how in MANUAL_ROWS:
        lines.append(f"| - | {item} | MANUAL | {how} |")
    if session.notes:
        lines += ["", "## Notes", ""] + [f"- {note}" for note in session.notes]
    lines += ["", "No EEG samples are stored in this report.", ""]
    return "\n".join(lines)


# --- main ----------------------------------------------------------------------------------


def parse_steps(text: str) -> list[str]:
    chosen = [part.strip().lower() for part in text.replace(" ", ",").split(",") if part.strip()]
    unknown = [s for s in chosen if s not in STEPS]
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown step(s) {unknown}; choose from {list(STEPS)}")
    return [s for s in STEPS if s in chosen]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--source", choices=("synthetic", "ironbci"), default="synthetic")
    parser.add_argument("--subject", default="P01", help="pseudonym such as P01 (never a name)")
    parser.add_argument("--steps", type=parse_steps, default=list(STEPS), help="e.g. a,b,e")
    parser.add_argument("--duration", type=float, default=600.0, help="step b length (s)")
    parser.add_argument("--phase", type=float, default=30.0, help="alpha phase length (s)")
    parser.add_argument("--clenches", type=int, default=5, help="rounds of step e")
    parser.add_argument(
        "--tap-window", type=float, default=4.0, help="seconds per electrode, step c"
    )
    parser.add_argument("--latency-seconds", type=float, default=10.0)
    parser.add_argument("--label", default="", help="short tag in the report, e.g. ref-mastoid")
    parser.add_argument("--yes", action="store_true", help="answer the prompts automatically")
    parser.add_argument("--seed", type=int, default=None, help="synthetic source seed")
    parser.add_argument("--ble-name")
    parser.add_argument("--ble-address")
    parser.add_argument("--ble-counter", action="store_true")
    parser.add_argument("--gain", type=float, default=1.0)
    return parser


def build_source(args: argparse.Namespace) -> EEGSource:
    if args.source == "synthetic":
        return SyntheticSource(montage="C", seed=args.seed)
    from openmind_plug.sources.ironbci import IronBCISource

    return IronBCISource(
        name=args.ble_name,
        address=args.ble_address,
        montage="C",
        gain=args.gain,
        counter=args.ble_counter,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        validate_subject(args.subject)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if not args.label.replace("-", "").replace("_", "").isalnum() and args.label:
        print("error: --label may only contain letters, digits, - and _", file=sys.stderr)
        return 2
    now = dt.datetime.now()
    path = report_path(args.subject, args.label, now)
    if inside_git_repository(path):
        print(
            f"error: refusing to write the report inside a Git repository ({path}); "
            "set OPENMIND_DATA_DIR outside any repository",
            file=sys.stderr,
        )
        return 2

    steps: list[str] = args.steps
    session = Session(build_source(args), args.yes, simulated=args.source == "synthetic")
    print(
        f"IronBCI-8 validation, source {args.source}, subject {args.subject}, "
        f"steps {','.join(steps)}"
    )
    try:
        step_connection(session)  # always: the source must be running
        if "b" in steps:
            step_rate(session, args.duration)
        if "c" in steps:
            step_channel_order(session, args.tap_window)
        if "d" in steps:
            step_alpha(session, args.phase)
        if "e" in steps:
            step_jaw(session, args.clenches)
        if "f" in steps:
            step_latency(session, args.latency_seconds)
        if "a" in steps and session.source.is_running:
            window, _, _ = collect(session.source, 2.0)
            if window.shape[0] >= session.srate:
                bad = [q.label for q in assess(window, session.srate, session.labels) if not q.ok]
                session.notes.append(
                    "Signal quality over 2 s: "
                    + (f"problems on {', '.join(bad)}." if bad else "no problem found.")
                )
    except KeyboardInterrupt:
        session.add("-", "Run", "FAIL", "interrupted")
    except Exception as error:  # a driver error must still leave a report
        session.add("-", "Run", "FAIL", f"{type(error).__name__}: {error}")
    finally:
        session.source.stop()

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(session, args, now, steps), encoding="utf-8")
    print(f"Report written to {path}")
    failed = any(r.status == "FAIL" for r in session.results)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
