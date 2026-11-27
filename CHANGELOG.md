# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Montage `C` is now the default montage (synthetic source, `DEFAULT_MONTAGE`,
  CLI help), since OpenMind Core reads the `action` jaw clench on the motor
  electrodes. Montage `A` stays available with `--montage A`. The synthetic
  source therefore has no `Fp1` by default: blinks are barely visible unless
  montage `A` or `B` is chosen.

### Added

- `examples/validate_ironbci.py`: guided hardware validation (connection, rate and
  packet loss, channel order, alpha test, jaw clench, latency to LSL) that runs
  today on the synthetic source and later on the headset, with a Markdown report
  under `$OPENMIND_DATA_DIR/validation/`; validation checklist and "After
  validation" procedure in `docs/hardware/ironbci.md`.
- `IronBCISource` (`ironbci` extra, **experimental**): IronBCI-8 over Bluetooth
  LE with `bleak`, with a pure `PacketDecoder` (24-bit two's complement to
  microvolts, configurable gain and Vref, optional packet counter with
  lost-packet detection) and timestamps on the LSL clock. Written from the
  public documentation and tested with a simulated headset; not validated on
  hardware until PLUG-090. Protocol notes and open assumptions in
  `docs/hardware/ironbci.md`.
- `stream --source ironbci` with `--ble-name`, `--ble-address`, `--gain` and
  `--ble-counter`; `list-sources` reports it as `experimental`. CI runs the tests
  with the `ironbci` extra. Documented relay of PiEEG Server's local LSL stream
  with `--source lsl`.
- `BrainFlowSource` (`brainflow` extra): the EEG channels of any BrainFlow
  board, in microvolts on the LSL clock, labelled from the wired electrodes or
  the board's defaults, optionally selected by montage. Tested with the
  BrainFlow synthetic board, without hardware.
- `stream --source brainflow` with `--board`, repeatable `--board-param
  KEY=VALUE` and `--labels`; `list-sources` reports it as available when the
  extra is installed. CI runs the tests with the `brainflow` extra.
- Synthetic subjects (`SyntheticSubject`, `SyntheticSource(subject=..., session=...)`,
  `openmind-plug stream --source synthetic --subject N [--session S]`): individual
  rhythm frequencies, ERD depth (including weak desynchronizers), electrode gains,
  noise and spatial mixing, drawn deterministically from the seed and id, plus a
  small session-to-session drift, to test calibration and alignment in Core.
  Without a subject the signal is unchanged.
- Replay of three more public datasets, all containing montage `C`:
  `lee2019_mi` (54 subjects), `cho2017` (52) and `schirrmeister2017` (14,
  with `feet` and `rest`). Listed in `stream --help` and `docs/sources.md`.
- LSL stream specification 1.2.0: marker types `trial_start`, `trial_end` and
  `event` (from the OpenMind protocol), and several `openmind-markers` streams
  at once (section 2.3). Additive: 1.x consumers keep working.
- `EEGInlet` reads every `openmind-markers` stream, including streams that
  appear later, and merges their markers by timestamp; `markers_from` restricts
  the accepted producers, `marker_sources` lists them. The recorder therefore
  keeps the calibration markers of OpenMind Sandbox.
- Montage `C` (`FC3, FC4, C3, Cz, C4, CP3, CP4, CPz`): eight motor electrodes,
  without Fp1, planned for the IronBCI-8.
- LSL stream specification 1.1.0: montage name `C` and the `jaw_clench`
  artifact marker (with `count`). Additive: 1.0 consumers keep working.
- Synthetic jaw clench, single or double (`clench()`, key `j`, `clench_uv`),
  to test the `action` gesture of OpenMind Core without a headset.
- Note on the alpha test with motor montages (no posterior electrode).
- Source interface: `ChannelInfo`, `StreamInfo` (validated, immutable) and the
  `EEGSource` base class with context-manager support and output checks.
- Guides: getting started without a headset, adding a driver; examples
  (scripted synthetic session, text band-power view, live plot, record and
  replay), checked by mypy and smoke tests.
- LSL relay source (`--source lsl`): republish a third-party LSL stream in
  standard form, with label renaming and normalization, unit detection or
  override, and montage selection.
- Signal quality and `openmind-plug quality`: per-channel flat, saturated,
  line-noise, noisy and weak detection on high-passed data, impedance reporting
  hook, and a guided eyes open / closed alpha test. See `docs/quality.md`.
- Recorder and `openmind-plug record`: EEG and markers saved to FIF with
  pseudonymous subject ids only, 10-10 positions, stream metadata and gap
  annotations, in `$OPENMIND_DATA_DIR/recordings/` by default. Recordings replay
  identically, markers included. See `docs/recording.md`.
- `openmind-plug stream` (synthetic with keyboard control, replay of datasets or
  files) and `openmind-plug list-sources`.
- LSL stream specification 1.0.0 (`docs/stream-spec.md`): `openmind-eeg`
  float32 stream in microvolts with XDF-style metadata (channels with 10-10
  locations, acquisition, reference, montage, `spec_version`) and the paired
  `openmind-markers` JSON stream.
- `EEGOutlet` and `stream()` to publish any source, `EEGInlet` to consume the
  standard stream, and `openmind-plug check-stream` to validate a live stream.
- Replay source: real-time playback (optionally accelerated or looped) of public
  datasets `bnci2014_001` and `physionetmi` via MOABB, or of any file MNE reads;
  montage-based channel selection with explicit errors, microvolt conversion,
  resampling, and dataset events as JSON cue markers. Downloads go to
  `$OPENMIND_DATA_DIR/mne_data`.
- Synthetic source: 1/f background, posterior alpha (eyes open/closed), mu and
  beta over C3/Cz/C4 with controllable desynchronization (`rest`, `left`,
  `right`, `feet`), optional blink, muscle and power-line artifacts, ground-truth
  markers, keyboard bindings; deterministic with a seed.
- `Marker` type and `EEGSource.read_markers()` for sources that produce events.
- Montages: 10-10 electrode positions and named montages `A` (motor imagery,
  default) and `B` (SSVEP variant). See `docs/sources.md`.
- Package skeleton: `src/` layout, `openmind-plug` command, optional extras
  `replay`, `brainflow` and `ironbci`.
- Tooling: ruff, strict mypy, pytest, pre-commit, GitHub Actions CI
  (Linux, macOS, Windows × Python 3.11, 3.12).

### Fixed

- `examples/validate_ironbci.py` checks that the machine is offline (step a)
  locally, from the routing table, instead of opening a TCP connection to
  1.1.1.1: the validation no longer sends a packet to a public server.
- `openmind-plug list-sources` sizes the status column to the longest status, so
  a `needs: pip install ...` hint no longer shifts the description column.
