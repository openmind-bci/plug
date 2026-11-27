"""Command-line interface for ``openmind-plug``."""

from __future__ import annotations

import dataclasses
import importlib.util
import threading
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, NoReturn

import typer

from openmind_plug import __version__
from openmind_plug.sources.base import EEGSource, SampleArray

if TYPE_CHECKING:
    from openmind_plug.inlet import EEGInlet
    from openmind_plug.montage import Montage

app = typer.Typer(
    name="openmind-plug",
    help="Read any EEG source and stream it as standard LSL.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"openmind-plug {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show the version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """OpenMind Plug: universal EEG plug for LSL."""


class SourceName(StrEnum):
    """Sources accepted by ``stream --source``."""

    synthetic = "synthetic"
    replay = "replay"
    lsl = "lsl"
    brainflow = "brainflow"
    ironbci = "ironbci"


@dataclass(frozen=True)
class _SourceEntry:
    description: str
    extra: str | None = None  # optional dependency group, if any
    module: str | None = None  # module that must be importable
    planned: bool = False
    experimental: bool = False


SOURCES: dict[SourceName, _SourceEntry] = {
    SourceName.synthetic: _SourceEntry("Realistic synthetic EEG with keyboard-controlled state"),
    SourceName.replay: _SourceEntry(
        "Recorded files (any MNE format) or public datasets, in real time"
    ),
    SourceName.lsl: _SourceEntry("Relay of a third-party LSL stream, in standard form"),
    SourceName.brainflow: _SourceEntry(
        "Any BrainFlow-supported board", "brainflow", module="brainflow"
    ),
    SourceName.ironbci: _SourceEntry(
        "IronBCI-8 headset over BLE (experimental, not validated on hardware)",
        "ironbci",
        module="bleak",
        experimental=True,
    ),
}


def _source_status(entry: _SourceEntry) -> str:
    if entry.planned:
        return "planned"
    if entry.module and importlib.util.find_spec(entry.module) is None:
        return f'needs: pip install "openmind-plug[{entry.extra}]"'
    return "experimental" if entry.experimental else "available"


@app.command("list-sources")
def list_sources() -> None:
    """List the EEG sources and whether they can be used."""
    width = max(len(name.value) for name in SOURCES)
    statuses = {name: _source_status(entry) for name, entry in SOURCES.items()}
    status_width = max(len(status) for status in statuses.values())
    for name, entry in SOURCES.items():
        status = statuses[name]
        color = typer.colors.GREEN if status == "available" else typer.colors.YELLOW
        typer.echo(f"{name.value:<{width}}  ", nl=False)
        typer.secho(f"{status:<{status_width}}", fg=color, nl=False)
        note = ""
        if name is SourceName.replay and not _datasets_available():
            note = f" (datasets need: {_REPLAY_INSTALL})"
        typer.echo(f"  {entry.description}{note}")


#: Keys of ``openmind_plug.sources.replay.DATASETS`` (kept in sync by a test).
_DATASET_KEYS = "bnci2014_001, physionetmi, lee2019_mi, cho2017, schirrmeister2017"
_REPLAY_INSTALL = 'pip install "openmind-plug[replay]"'


def _datasets_available() -> bool:
    """Whether MOABB, needed to download public datasets, is installed."""
    return importlib.util.find_spec("moabb") is not None


_SYNTH = "Synthetic source"
_REPLAY = "Replay source"
_RELAY = "LSL relay"
_BRAINFLOW = "BrainFlow source"
_IRONBCI = "IronBCI source (experimental)"


@app.command("stream")
def stream_command(
    source: Annotated[
        SourceName, typer.Option("--source", "-s", help="Source to stream.", show_default=False)
    ],
    montage: Annotated[
        str | None,
        typer.Option(
            "--montage",
            "-m",
            help="Montage (A, B, C). Default: C for synthetic, every EEG channel for replay.",
            show_default=False,
        ),
    ] = None,
    srate: Annotated[float, typer.Option(help="Sampling rate in Hz.", min=1)] = 250.0,
    duration: Annotated[
        float | None,
        typer.Option(help="Stop after this many seconds (default: until Ctrl+C).", min=0.1),
    ] = None,
    source_id: Annotated[
        str | None, typer.Option(help="LSL source_id (default: openmind-plug:<driver>:<model>).")
    ] = None,
    keyboard: Annotated[
        bool | None,
        typer.Option(
            "--keyboard/--no-keyboard",
            help="Control the synthetic source from the keyboard (default: on in a terminal).",
            show_default=False,
        ),
    ] = None,
    seed: Annotated[int | None, typer.Option(help="Random seed.", rich_help_panel=_SYNTH)] = None,
    blink_rate: Annotated[
        float, typer.Option(help="Random blinks per minute.", min=0, rich_help_panel=_SYNTH)
    ] = 0.0,
    muscle_rate: Annotated[
        float,
        typer.Option(help="Random muscle bursts per minute.", min=0, rich_help_panel=_SYNTH),
    ] = 0.0,
    line_noise: Annotated[
        float,
        typer.Option(help="Power-line noise amplitude in µV.", min=0, rich_help_panel=_SYNTH),
    ] = 0.0,
    line_freq: Annotated[
        int, typer.Option(help="Power-line frequency (50 or 60 Hz).", rich_help_panel=_SYNTH)
    ] = 50,
    dataset: Annotated[
        str | None,
        typer.Option(help="Public dataset: " + _DATASET_KEYS + ".", rich_help_panel=_REPLAY),
    ] = None,
    subject: Annotated[
        int | None,
        typer.Option(
            help="Subject: dataset subject number (replay, default 1) or synthetic subject "
            "id (synthetic, default: neutral subject).",
            min=0,
            show_default=False,
            rich_help_panel=_REPLAY,
        ),
    ] = None,
    session: Annotated[
        str | None,
        typer.Option(
            help="Session: dataset session name (replay, default: first) or session number "
            "with small drift (synthetic).",
            show_default=False,
            rich_help_panel=_REPLAY,
        ),
    ] = None,
    file: Annotated[
        Path | None,
        typer.Option(
            help="Recording to replay (FIF, EDF, BDF...), instead of a dataset.",
            exists=True,
            dir_okay=False,
            rich_help_panel=_REPLAY,
        ),
    ] = None,
    speed: Annotated[
        float,
        typer.Option(help="Playback speed (1 = real time).", min=0.01, rich_help_panel=_REPLAY),
    ] = 1.0,
    loop: Annotated[
        bool, typer.Option("--loop", help="Restart at the end.", rich_help_panel=_REPLAY)
    ] = False,
    lsl_name: Annotated[
        str | None, typer.Option(help="Name of the stream to relay.", rich_help_panel=_RELAY)
    ] = None,
    lsl_type: Annotated[
        str, typer.Option(help="Type of the stream to relay.", rich_help_panel=_RELAY)
    ] = "EEG",
    lsl_source_id: Annotated[
        str | None,
        typer.Option(help="source_id of the stream to relay.", rich_help_panel=_RELAY),
    ] = None,
    unit: Annotated[
        str | None,
        typer.Option(
            help="Unit of the incoming samples (uV, mV, V), if the stream does not say.",
            rich_help_panel=_RELAY,
        ),
    ] = None,
    rename: Annotated[
        str | None,
        typer.Option(
            help='Channel renames, e.g. "EXG Channel 0=C3,EXG Channel 1=C4".',
            rich_help_panel=_RELAY,
        ),
    ] = None,
    board: Annotated[
        str,
        typer.Option(
            help="BoardIds name (CYTON_BOARD...) or numeric id; 'synthetic' needs no hardware.",
            rich_help_panel=_BRAINFLOW,
        ),
    ] = "synthetic",
    board_param: Annotated[
        list[str] | None,
        typer.Option(
            "--board-param",
            help="Board connection parameter KEY=VALUE, e.g. serial_port=/dev/ttyUSB0 "
            "(repeatable).",
            show_default=False,
            rich_help_panel=_BRAINFLOW,
        ),
    ] = None,
    labels: Annotated[
        str | None,
        typer.Option(
            help="Electrodes wired to the board inputs, in board order, e.g. C3,Cz,C4 "
            "(default: the board's own names).",
            show_default=False,
            rich_help_panel=_BRAINFLOW,
        ),
    ] = None,
    ble_name: Annotated[
        str | None,
        typer.Option(
            help="Advertised BLE name (default: EAREEG or IronBCI).",
            show_default=False,
            rich_help_panel=_IRONBCI,
        ),
    ] = None,
    ble_address: Annotated[
        str | None,
        typer.Option(
            help="BLE address (a UUID on macOS); skips the scan.",
            show_default=False,
            rich_help_panel=_IRONBCI,
        ),
    ] = None,
    gain: Annotated[
        float,
        typer.Option(
            help="Divisor applied to the ADC counts (default 1, as the official scripts).",
            rich_help_panel=_IRONBCI,
        ),
    ] = 1.0,
    ble_counter: Annotated[
        bool,
        typer.Option(
            "--ble-counter",
            help="Payloads start with a one-byte packet counter (unconfirmed; "
            "enables lost-packet detection).",
            rich_help_panel=_IRONBCI,
        ),
    ] = False,
) -> None:
    """Stream an EEG source as the standard OpenMind LSL streams.

    Examples:

      openmind-plug stream --source synthetic --montage C

      openmind-plug stream --source synthetic --subject 3 --session 2

      openmind-plug stream --source replay --dataset bnci2014_001 --subject 1

      openmind-plug stream --source lsl --lsl-name obci_eeg1 --unit uV --montage A

      openmind-plug stream --source brainflow --board synthetic

      openmind-plug stream --source ironbci --montage C   (experimental)
    """
    from openmind_plug.keyboard import key_reader, stdin_is_terminal
    from openmind_plug.outlet import EEGOutlet, stream

    entry = SOURCES[source]
    status = _source_status(entry)
    if status not in ("available", "experimental"):
        _fail(f"source {source.value!r} is not available: {status}")

    built = _build_source(
        source,
        montage=montage,
        srate=srate,
        seed=seed,
        blink_rate=blink_rate,
        muscle_rate=muscle_rate,
        line_noise=line_noise,
        line_freq=line_freq,
        dataset=dataset,
        subject=subject,
        session=session,
        file=file,
        speed=speed,
        loop=loop,
        relay=_RelayOptions(lsl_name, lsl_type, lsl_source_id, unit, rename),
        brainflow=_BrainFlowOptions(board, tuple(board_param or ()), labels),
        ironbci=_IronBCIOptions(ble_name, ble_address, gain, ble_counter),
    )
    info = built.info
    use_keyboard = source is SourceName.synthetic and (
        keyboard if keyboard is not None else stdin_is_terminal()
    )

    try:
        with EEGOutlet(info, source_id=source_id) as outlet, ExitStack() as stack:
            typer.secho(f"Streaming {outlet.source_id}", bold=True)
            typer.echo(
                f"  {info.n_channels} channels @ {info.srate:g} Hz: {', '.join(info.labels)}"
            )
            stop = threading.Event()
            if use_keyboard:
                _start_keyboard(built, stack.enter_context(key_reader()), stop)
            else:
                typer.echo("  Press Ctrl+C to stop.")
            stats = stream(built, duration=duration, stop_event=stop, outlet=outlet)
            stop.set()
    except Exception as error:
        if source is SourceName.brainflow and _is_brainflow_error(error):
            _fail(f"BrainFlow board error: {error}")
        if source is SourceName.ironbci and _is_ironbci_error(error):
            _fail(str(error))
        raise

    ending = " (end of recording)" if getattr(built, "finished", False) else ""
    typer.echo(
        f"Stopped after {stats.elapsed:.1f} s{ending}: "
        f"{stats.n_samples} samples, {stats.n_markers} markers."
    )


@app.command("record")
def record_command(
    subject: Annotated[
        str,
        typer.Option(
            help="Pseudonymous subject id (P01, P02...), never a name.", show_default=False
        ),
    ],
    out: Annotated[
        Path | None,
        typer.Option(
            help="Output file, ending with _raw.fif "
            "(default: $OPENMIND_DATA_DIR/recordings/<subject>/<subject>_<date>_raw.fif).",
            dir_okay=False,
            show_default=False,
        ),
    ] = None,
    duration: Annotated[
        float | None,
        typer.Option(help="Stop after this many seconds (default: until Ctrl+C).", min=0.1),
    ] = None,
    timeout: Annotated[
        float, typer.Option(help="Seconds to wait for the stream to appear.", min=0.1)
    ] = 5.0,
    source_id: Annotated[
        str | None, typer.Option(help="Record the stream with this LSL source_id.")
    ] = None,
    overwrite: Annotated[bool, typer.Option(help="Replace an existing file.")] = False,
) -> None:
    """Record the standard LSL stream (EEG and markers) to a FIF file.

    Example:

      openmind-plug record --subject P01 --duration 60
    """
    from openmind_plug.inlet import StreamNotFoundError
    from openmind_plug.recorder import default_path, record_stream, validate_subject

    try:
        validate_subject(subject)
    except ValueError as error:
        _fail(str(error))
    target = out or default_path(subject)
    if not target.name.endswith("_raw.fif"):
        _fail(f"recording file names must end with '_raw.fif', got {target.name}")
    if target.exists() and not overwrite:
        _fail(f"{target} already exists (use --overwrite to replace it)")
    if _inside_git_repository(target):
        typer.secho(
            f"Warning: {target.parent} is inside a Git repository; EEG recordings are "
            "health data and must never be committed.",
            fg=typer.colors.YELLOW,
            err=True,
        )

    try:
        typer.echo("Waiting for the stream...")
        recorder = record_stream(subject, duration=duration, timeout=timeout, source_id=source_id)
    except StreamNotFoundError as error:
        _fail(str(error))
    if recorder.n_samples == 0:
        _fail("no sample received; nothing was saved")
    path = recorder.save(target, overwrite=overwrite)
    typer.echo(
        f"Saved {recorder.duration:.1f} s ({recorder.n_samples} samples, "
        f"{recorder.n_markers} markers) to {path}"
    )


@app.command("quality")
def quality_command(
    window: Annotated[float, typer.Option(help="Seconds of signal per assessment.", min=1.0)] = 2.0,
    duration: Annotated[
        float | None,
        typer.Option(help="Stop after this many seconds (default: until Ctrl+C).", min=1.0),
    ] = None,
    line_freq: Annotated[float, typer.Option(help="Power-line frequency in Hz (50 or 60).")] = 50.0,
    saturation_uv: Annotated[
        float | None,
        typer.Option(help="Amplifier range in µV, if known, to detect saturation.", min=1.0),
    ] = None,
    alpha_test: Annotated[
        bool,
        typer.Option("--alpha-test", help="Run the guided eyes open / eyes closed test."),
    ] = False,
    phase: Annotated[float, typer.Option(help="Seconds of each alpha-test phase.", min=2.0)] = 30.0,
    timeout: Annotated[
        float, typer.Option(help="Seconds to wait for the stream to appear.", min=0.1)
    ] = 5.0,
    source_id: Annotated[
        str | None, typer.Option(help="Check the stream with this LSL source_id.")
    ] = None,
) -> None:
    """Show live signal quality per channel, or run the alpha test.

    Live mode flags flat, saturated, noisy (50/60 Hz or high variance) and weak
    channels every WINDOW seconds. With --alpha-test, follow the instructions: eyes
    open, then eyes closed; the test passes when alpha power rises on at least one
    channel (exit code 1 otherwise).
    """
    from openmind_plug.inlet import EEGInlet, StreamNotFoundError

    try:
        inlet = EEGInlet(timeout=timeout, source_id=source_id, with_markers=False)
    except StreamNotFoundError as error:
        _fail(str(error))
    with inlet:
        info = inlet.info
        typer.secho(f"Quality of {inlet.source_id} ({info.n_channels} channels)", bold=True)
        if alpha_test:
            _run_alpha_test(inlet, phase)
        else:
            _run_live_quality(inlet, window, duration, line_freq, saturation_uv)


def _collect(inlet: EEGInlet, seconds: float) -> SampleArray:
    """Receive ``seconds`` of samples from an :class:`EEGInlet`."""
    import numpy as np
    import pylsl

    n_target = round(seconds * inlet.info.srate)
    blocks, count = [], 0
    deadline = pylsl.local_clock() + seconds * 2 + 5
    while count < n_target and pylsl.local_clock() < deadline:
        samples, _ = inlet.read(max_samples=n_target - count, timeout=0.2)
        blocks.append(samples)
        count += samples.shape[0]
    if count < n_target:
        _fail(f"received only {count} of {n_target} samples; is the stream running?")
    return np.vstack(blocks)


_STATUS_COLORS = {"good": typer.colors.GREEN, "weak": typer.colors.YELLOW}


def _run_live_quality(
    inlet: EEGInlet,
    window: float,
    duration: float | None,
    line_freq: float,
    saturation_uv: float | None,
) -> None:
    import pylsl

    from openmind_plug.quality import assess

    typer.echo("  Press Ctrl+C to stop.")
    start = pylsl.local_clock()
    try:
        while duration is None or pylsl.local_clock() - start + window <= duration + 0.5:
            samples = _collect(inlet, window)
            report = assess(
                samples,
                inlet.info.srate,
                inlet.info.labels,
                line_freq=line_freq,
                saturation_uv=saturation_uv,
            )
            elapsed = pylsl.local_clock() - start
            typer.echo(f"[{elapsed:5.0f} s]", nl=False)
            for channel in report:
                color = _STATUS_COLORS.get(channel.status, typer.colors.RED)
                typer.echo(f"  {channel.label} ", nl=False)
                typer.secho(channel.status, fg=color, nl=False)
                typer.echo(f" {channel.std_uv:.0f}µV", nl=False)
            typer.echo()
    except KeyboardInterrupt:
        pass


def _run_alpha_test(inlet: EEGInlet, phase: float) -> None:
    from openmind_plug.quality import (
        ALPHA_GAIN_THRESHOLD,
        ALPHA_MIN_RELIABLE_PHASE_S,
        alpha_test,
    )

    if phase < ALPHA_MIN_RELIABLE_PHASE_S:
        typer.secho(
            f"Warning: phases shorter than {ALPHA_MIN_RELIABLE_PHASE_S:g} s give unreliable "
            "results.",
            fg=typer.colors.YELLOW,
        )
    typer.secho(f"1/2  Keep your eyes OPEN, look at a fixed point ({phase:g} s)...", bold=True)
    eyes_open = _collect(inlet, phase)
    typer.secho(f"2/2  CLOSE your eyes and relax ({phase:g} s)...", bold=True)
    eyes_closed = _collect(inlet, phase)
    typer.echo("Done, you can open your eyes.")

    report = alpha_test(
        eyes_open,
        eyes_closed,
        inlet.info.srate,
        inlet.info.labels,
    )
    for label, gain in sorted(report.gains.items(), key=lambda item: -item[1]):
        typer.echo(f"  {label:<5} alpha x{gain:.2f}")
    if report.passed:
        typer.secho(
            f"PASSED: alpha rises x{report.best_gain:.1f} on {report.best_channel} "
            f"(threshold x{ALPHA_GAIN_THRESHOLD}).",
            fg=typer.colors.GREEN,
        )
    else:
        typer.secho(
            f"FAILED: best alpha increase x{report.best_gain:.2f} on {report.best_channel}, "
            f"below x{ALPHA_GAIN_THRESHOLD}. Check posterior electrodes and relax.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)


def _inside_git_repository(path: Path) -> bool:
    """Whether ``path`` would be written inside a Git working tree."""
    return any((parent / ".git").exists() for parent in path.resolve().parents)


def _build_source(
    source: SourceName,
    *,
    montage: str | None,
    srate: float,
    seed: int | None,
    blink_rate: float,
    muscle_rate: float,
    line_noise: float,
    line_freq: int,
    dataset: str | None,
    subject: int | None,
    session: str | None,
    file: Path | None,
    speed: float,
    loop: bool,
    relay: _RelayOptions | None = None,
    brainflow: _BrainFlowOptions | None = None,
    ironbci: _IronBCIOptions | None = None,
) -> EEGSource:
    from pydantic import ValidationError

    from openmind_plug.montage import DEFAULT_MONTAGE, get_montage

    resolved: Montage | None = None
    if montage is not None:
        try:
            resolved = get_montage(montage)
        except KeyError as error:
            _fail(str(error.args[0]))

    if source is SourceName.synthetic:
        from openmind_plug.sources.synthetic import SyntheticConfig, SyntheticSource

        try:
            config = SyntheticConfig(
                blink_rate_per_min=blink_rate,
                muscle_rate_per_min=muscle_rate,
                line_noise_uv=line_noise,
                line_freq_hz=line_freq,  # type: ignore[arg-type]  # validated by pydantic
            )
            session_number: int | None = None
            if session is not None:
                try:
                    session_number = int(session)
                except ValueError:
                    _fail(f"synthetic sessions are numbers, got {session!r}")
            if session_number is not None and session_number < 0:
                _fail("--session must be >= 0")
            if subject is not None:
                note = f", session {session_number}" if session_number is not None else ""
                typer.echo(f"Synthetic subject {subject}{note}")
            return SyntheticSource(
                montage=resolved or DEFAULT_MONTAGE,
                srate=srate,
                seed=seed,
                config=config,
                subject=subject,
                session=session_number,
            )
        except (ValidationError, ValueError) as error:
            _fail(str(error))

    if source is SourceName.lsl:
        return _build_relay(relay or _RelayOptions(), resolved)

    if source is SourceName.brainflow:
        return _build_brainflow(brainflow or _BrainFlowOptions(), resolved)

    if source is SourceName.ironbci:
        return _build_ironbci(ironbci or _IronBCIOptions(), resolved)

    from openmind_plug.sources.replay import MissingChannelsError, ReplaySource

    if (dataset is None) == (file is None):
        _fail("replay needs exactly one of --dataset or --file")
    try:
        if file is not None:
            return ReplaySource.from_file(
                file, montage=resolved, srate=srate, speed=speed, loop=loop
            )
        if not _datasets_available():
            _fail(f"replaying public datasets needs MOABB: {_REPLAY_INSTALL}")
        subject_number = 1 if subject is None else subject
        typer.echo(f"Loading {dataset} subject {subject_number} (downloaded on first use)...")
        return ReplaySource.from_dataset(
            str(dataset),
            subject=subject_number,
            session=session,
            montage=resolved,
            srate=srate,
            speed=speed,
            loop=loop,
        )
    except (MissingChannelsError, KeyError, ValueError, ImportError) as error:
        _fail(str(error.args[0]) if error.args else str(error))


@dataclass(frozen=True)
class _RelayOptions:
    name: str | None = None
    stream_type: str = "EEG"
    source_id: str | None = None
    unit: str | None = None
    rename: str | None = None


def _parse_renames(text: str | None) -> dict[str, str]:
    """Parse ``"old=new,old2=new2"`` into a mapping."""
    renames: dict[str, str] = {}
    for item in (text or "").split(","):
        if not item.strip():
            continue
        old, sep, new = item.partition("=")
        if not sep or not old.strip() or not new.strip():
            _fail(f"invalid rename {item.strip()!r}; expected old=new")
        renames[old.strip()] = new.strip()
    return renames


def _build_relay(options: _RelayOptions, montage: Montage | None) -> EEGSource:
    from openmind_plug.inlet import StreamNotFoundError
    from openmind_plug.sources.lsl_in import LSLSource, LSLSourceError

    if options.name is None and options.source_id is None:
        _fail("the lsl source needs --lsl-name or --lsl-source-id")
    typer.echo("Looking for the stream to relay...")
    try:
        return LSLSource(
            name=options.name,
            stream_type=options.stream_type or None,
            source_id=options.source_id,
            rename=_parse_renames(options.rename),
            unit=options.unit,
            montage=montage,
        )
    except (StreamNotFoundError, LSLSourceError) as error:
        _fail(str(error))


@dataclass(frozen=True)
class _BrainFlowOptions:
    board: str = "synthetic"
    params: tuple[str, ...] = ()
    labels: str | None = None


def _parse_board_params(items: tuple[str, ...]) -> dict[str, str]:
    """Parse ``KEY=VALUE`` items; the source converts values to the parameter type."""
    params: dict[str, str] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            _fail(f"invalid board parameter {item!r}; expected KEY=VALUE")
        params[key.strip()] = value.strip()
    return params


def _is_brainflow_error(error: BaseException) -> bool:
    try:
        from brainflow.board_shim import BrainFlowError
    except ImportError:
        return False
    return isinstance(error, BrainFlowError)


def _build_brainflow(options: _BrainFlowOptions, montage: Montage | None) -> EEGSource:
    from openmind_plug.sources.brainflow import (
        BRAINFLOW_INSTALL,
        BrainFlowSource,
        BrainFlowSourceError,
    )

    labels = [label.strip() for label in options.labels.split(",")] if options.labels else None
    if labels is not None and not all(labels):
        _fail(f"invalid --labels {options.labels!r}; expected a comma-separated list")
    params = _parse_board_params(options.params)
    try:
        return BrainFlowSource(options.board, params=params, labels=labels, montage=montage)
    except ImportError:
        _fail(f"the brainflow source needs the brainflow extra: {BRAINFLOW_INSTALL}")
    except BrainFlowSourceError as error:
        _fail(str(error))
    except Exception as error:
        if _is_brainflow_error(error):
            _fail(f"BrainFlow board error: {error}")
        raise


@dataclass(frozen=True)
class _IronBCIOptions:
    name: str | None = None
    address: str | None = None
    gain: float = 1.0
    counter: bool = False


def _is_ironbci_error(error: BaseException) -> bool:
    from openmind_plug.sources.ironbci import IronBCIError

    return isinstance(error, IronBCIError)


def _build_ironbci(options: _IronBCIOptions, montage: Montage | None) -> EEGSource:
    from openmind_plug.sources.ironbci import (
        IRONBCI_INSTALL,
        IronBCIConfig,
        IronBCISource,
    )

    typer.secho(
        "IronBCI-8 driver is experimental: not validated on hardware yet.", fg=typer.colors.YELLOW
    )
    try:
        config = IronBCIConfig(montage=montage or "C", gain=options.gain, counter=options.counter)
        if options.name:
            config = dataclasses.replace(config, ble_names=(options.name,))
        source = IronBCISource(address=options.address, config=config)
    except ImportError:
        _fail(f"the ironbci source needs the ironbci extra: {IRONBCI_INSTALL}")
    except ValueError as error:
        _fail(str(error))
    return source


def _start_keyboard(
    source: EEGSource, read: Callable[[float], str | None], stop: threading.Event
) -> None:
    """Read keys in a background thread and apply them to the synthetic source."""
    from openmind_plug.keyboard import run_key_loop
    from openmind_plug.sources.synthetic import KEY_BINDINGS, SyntheticSource

    assert isinstance(source, SyntheticSource)
    bindings = ", ".join(f"{key}: {action}" for key, action in KEY_BINDINGS.items())
    typer.echo(f"  Keys: {bindings}, q: quit.")

    def on_key(key: str) -> bool:
        if key.lower() == "q":
            return False
        if source.handle_key(key):
            typer.echo(f"  state: {source.state}, eyes: {source.eyes}")
        return True

    threading.Thread(target=run_key_loop, args=(read, on_key, stop), daemon=True).start()


def _fail(message: str) -> NoReturn:
    """Print an error and exit with code 2 (usage error)."""
    typer.secho(f"Error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=2)


@app.command("check-stream")
def check_stream(
    timeout: Annotated[
        float, typer.Option(help="Seconds to wait for the stream to appear.", min=0.1)
    ] = 5.0,
    duration: Annotated[
        float, typer.Option(help="Seconds of data to receive and check.", min=0.5)
    ] = 2.0,
    source_id: Annotated[
        str | None, typer.Option(help="Check the stream with this LSL source_id.")
    ] = None,
) -> None:
    """Check that a live EEG stream follows the OpenMind stream specification.

    Exits with code 1 if the stream breaks the specification, 2 if none is found.
    """
    from openmind_plug.inlet import StreamNotFoundError
    from openmind_plug.inlet import check_stream as run_check
    from openmind_plug.spec import SPEC_VERSION

    try:
        report = run_check(timeout=timeout, duration=duration, source_id=source_id)
    except StreamNotFoundError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from error

    typer.echo(f"Stream {report.source_id} (specification {SPEC_VERSION})")
    if report.info is not None:
        info = report.info
        typer.echo(f"  {info.n_channels} channels @ {info.srate:g} Hz: {', '.join(info.labels)}")
        typer.echo(
            f"  {info.manufacturer} / {info.model} / driver {info.driver}, "
            f"reference {info.reference}, montage {info.montage or '-'}"
        )
    if report.n_samples:
        typer.echo(
            f"  received {report.n_samples} samples ({report.effective_srate:.1f} Hz), "
            f"{report.n_markers} markers"
        )
    for warning in report.warnings:
        typer.secho(f"  warning: {warning}", fg=typer.colors.YELLOW)
    for problem in report.errors:
        typer.secho(f"  error: {problem}", fg=typer.colors.RED)
    if report.ok:
        typer.secho("OK: the stream follows the specification.", fg=typer.colors.GREEN)
    else:
        typer.secho(f"FAILED: {len(report.errors)} problem(s).", fg=typer.colors.RED)
        raise typer.Exit(code=1)
