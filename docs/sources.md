# Sources

A **source** is anything that produces EEG samples: a headset driver, a replayed
dataset, a synthetic generator or a relayed LSL stream. Every source implements
the same small interface, so the LSL output, the recorder and the quality checks
work with all of them.

## Stream metadata

### `ChannelInfo`

| Field | Default | Rule |
|---|---|---|
| `label` | (required) | Non-empty, no surrounding whitespace. Prefer 10-20/10-10 labels (`C3`, `Cz`). |
| `unit` | `"uV"` | One of `uV`, `mV`, `V`. The standard stream uses `uV`. |
| `type` | `"EEG"` | One of `EEG`, `EOG`, `EMG`, `ECG`, `MISC`. |

### `StreamInfo`

| Field | Default | Rule |
|---|---|---|
| `name` | `"openmind-eeg"` | Non-empty. |
| `srate` | (required) | Nominal sampling rate in Hz, finite and > 0. |
| `channels` | (required) | At least one; labels unique, case-insensitively (`Cz` and `CZ` clash). |
| `manufacturer` | `"unknown"` | Hardware maker, or `OpenMind` for software sources. |
| `model` | `"unknown"` | Device model or source kind (`IronBCI-8`, `synthetic`...). |
| `driver` | `"unknown"` | Plug driver that produced the stream. |
| `reference` | `"unknown"` | Reference electrode or scheme (`Cz`, `average`...). |
| `montage` | `None` | Montage name, if a named montage is used. |

Both are immutable [pydantic](https://docs.pydantic.dev) models: invalid values
raise `pydantic.ValidationError` at construction.

```python
from openmind_plug.montage import get_montage
from openmind_plug.sources import StreamInfo

montage = get_montage("A")
info = StreamInfo(
    srate=250.0,
    channels=montage.channels(),
    manufacturer="OpenMind",
    model="synthetic",
    driver="synthetic",
    reference="Cz",
    montage=montage.name,
)
info.labels  # ('FC3', 'FC4', 'C3', 'Cz', 'C4', 'CP3', 'CP4', 'Fp1')
info.n_channels  # 8
```

## The `EEGSource` interface

```python
with source:  # start() ... stop()
    samples, timestamps = source.read(max_samples=64)
```

- `info` → the `StreamInfo` of the source.
- `start()` / `stop()` → open and release the device; both are idempotent. The
  `with` statement calls them for you, and `stop()` runs even if an error occurs.
- `read(max_samples)` → the samples available now, without blocking for long:
  - `samples`: `float32` array of shape `(n_samples, n_channels)`, in the channels' units,
    with `0 <= n_samples <= max_samples`;
  - `timestamps`: `float64` array of shape `(n_samples,)`, in seconds on the
    **LSL clock** (`pylsl.local_clock()`), never `time.time()`.

Reading before `start()`, or a driver returning arrays of the wrong shape, raises
`SourceStateError`.

### Writing a new source

Subclass `EEGSource` and implement three hooks; the base class handles state
checks and validates what you return.

```python
import numpy as np
from openmind_plug.sources import EEGSource, StreamInfo


class MySource(EEGSource):
    def __init__(self, info: StreamInfo) -> None:
        super().__init__()
        self._info = info

    @property
    def info(self) -> StreamInfo:
        return self._info

    def _start(self) -> None:
        # Open the device.
        ...

    def _stop(self) -> None:
        # Close the device.
        ...

    def _read(self, max_samples: int) -> tuple[np.ndarray, np.ndarray]:
        # Return (samples, timestamps).
        ...
```

## Montages

A montage is the ordered list of electrodes wired to the amplifier inputs.

| Name | Electrodes (input order) | Use |
|---|---|---|
| `A` | `FC3, FC4, C3, Cz, C4, CP3, CP4, Fp1` | Motor imagery: sensorimotor cortex, plus `Fp1` for blink detection |
| `B` | `C3, Cz, C4, CP3, CP4, Fp1, O1, O2` | SSVEP variant: occipital `O1`/`O2` instead of `FC3`/`FC4` |
| **`C`** (default) | `FC3, FC4, C3, Cz, C4, CP3, CP4, CPz` | Motor imagery on eight motor electrodes (`CPz` for the feet area); the jaw clench of the `action` command is read on the same electrodes. Planned for the IronBCI-8 |

```python
from openmind_plug.montage import get_montage, position

montage = get_montage("b")  # case-insensitive; unknown names list the available ones
montage.channels()  # tuple of ChannelInfo, unit "uV", type "EEG"
montage.positions()  # {"C3": (x, y, z), ...}
position("Cz")  # (0.0004, -0.0092, 0.1002)
```

Positions are approximate coordinates in meters for 47 electrodes of the 10-10
system, in the MNE head frame (x towards the right ear, y towards the nose,
z towards the vertex), taken from the MNE `standard_1005` template. They are meant
for visualization and spatial filtering, not for source localization.

## Synthetic source

`SyntheticSource` generates realistic EEG without any hardware. It is the main
test tool of the OpenMind ecosystem: deterministic with a seed, with a
controllable motor-imagery state and optional artifacts.

```python
from openmind_plug.sources.synthetic import SyntheticConfig, SyntheticSource

source = SyntheticSource(srate=250, seed=42)  # montage "C" by default
with source:
    source.set_state("left")  # imagine a left-hand movement
    samples, timestamps = source.read(max_samples=64)
    markers = source.read_markers()  # [Marker(timestamp, {"type": "state", ...})]
```

### Signal model

Each channel, in microvolts, is the sum of:

| Component | Model | Default |
|---|---|---|
| Background | 1/f (pink) noise, independent per channel | 15 µV RMS |
| Alpha (8-12 Hz) | Posterior generator, weight 0 at the central line → 1 at O1/O2 | 4 µV eyes open, 20 µV eyes closed |
| Mu (8-12 Hz) | Three generators centred on C3, Cz and C4 (Gaussian spread, σ = 2.5 cm) | 10 µV |
| Beta (13-30 Hz) | Same three sites | 4 µV |

Rhythm amplitudes fluctuate slowly (±30 %, time constant 0.5 s), as real
rhythms wax and wane. Amplitudes are peak values at the centre of each generator.

### State and event-related desynchronization

Imagining a movement lowers mu and beta power over the opposite hemisphere:

| State | Generators lowered | Key |
|---|---|---|
| `rest` | none | `r` |
| `left` | C4 (right hemisphere) | `a` |
| `right` | C3 (left hemisphere) | `d` |
| `feet` | Cz | `s` |

The power drop is `erd_depth` (default 60 %), reached smoothly with time
constant `erd_tau_s` (default 0.3 s). `set_eyes("closed")` (key `e` toggles)
raises posterior alpha the same way.

### Artifacts (all off by default)

| Artifact | Model | Settings |
|---|---|---|
| Blink | Hann pulse of 0.25-0.4 s, strongest on Fp1/Fp2 (σ = 3 cm); barely visible with the default montage `C`, which has no `Fp1` (use montage `A` or `B`) | `blink_rate_per_min`, `blink_uv` (120), or `blink()` / key `b` |
| Muscle | Burst of 0.5-1.5 s of noise above 30 Hz, stronger on lateral channels | `muscle_rate_per_min`, `muscle_uv` (20 µV RMS) |
| Jaw clench | On demand only: burst of 0.2-0.3 s of noise above 30 Hz on every electrode (stronger laterally); a double clench is two bursts 0.6-0.75 s apart, with a release of at least 0.3 s | `clench(count=2)` / key `j`, `clench_uv` (120 µV RMS) |
| Power line | Sinusoid with a per-channel gain | `line_noise_uv`, `line_freq_hz` (50 or 60) |

```python
config = SyntheticConfig(blink_rate_per_min=15, line_noise_uv=5)
source = SyntheticSource(seed=0, config=config)
```

### Markers (ground truth)

With `emit_markers=True` (default), every state change, eyes change and artifact
produces a marker whose timestamp is that of the first EEG sample it applies to:

```json
{"type": "state", "label": "left", "source": "synthetic"}
{"type": "eyes", "label": "closed", "source": "synthetic"}
{"type": "artifact", "label": "blink", "source": "synthetic"}
{"type": "artifact", "label": "jaw_clench", "count": 2, "source": "synthetic"}
```

### Timing and reproducibility

- `read()` returns the samples due according to the LSL clock since `start()`;
  timestamps are perfectly regular (`t0 + index / srate`). After `stop()` and
  `start()`, the stream resumes with no gap in sample indices.
- `generate(n)` returns the next `n` samples immediately, ignoring the clock —
  handy for offline tests and datasets.
- For a given seed and sequence of state changes, the signal only depends on the
  sample index, not on how it is split into reads.
- `srate` must be at least 100 Hz.

### Synthetic subjects

By default the simulated user is a single neutral subject. To test calibration,
alignment and cold start on many different users, give the source a subject:

```python
from openmind_plug.sources.synthetic import SyntheticSource, SyntheticSubject

source = SyntheticSource(seed=42, subject=3)  # SyntheticSubject.sample(3, seed=42)
later = SyntheticSource(seed=42, subject=3, session=2)  # same user, another day
```

`SyntheticSubject.sample(subject_id, seed=0)` draws every field from
`numpy.random.default_rng((seed, subject_id))`, so the same id and seed always give
the same user. Ranges are the `*_RANGE` constants of `openmind_plug.sources.synthetic`.

| Field | Meaning | Sampled range |
|---|---|---|
| `alpha_hz`, `mu_hz`, `beta_hz` | Centre frequencies (each generator still adds ±0.5 Hz) | 8.5-12, alpha ±0.5, 18-24 |
| `erd_depth` | Power drop per class (`left`, `right`, `feet`) | 0.2-0.7; 15 % of subjects "BCI-illiterate": 0.05-0.15 |
| `channel_gain` | Electrode contact, per channel | 0.7-1.3 |
| `noise_uv` | RMS of the 1/f background | 10-20 µV |
| `rhythm_amplitude_uv` | Peak mu amplitude (beta keeps its ratio) | 6-14 µV |
| `spatial_mixing` | Matrix close to the identity (off-diagonal σ = 0.06): covariances differ between subjects, which is what alignment corrects | - |

`SyntheticSubject.default()` leaves every `None` field to the `SyntheticConfig`
value and reproduces the signal of a source without subject exactly. `session`
adds a small drift (gains ±5 %, slightly perturbed mixing) drawn from
`(seed, subject_id, session)`: what cross-session alignment corrects. Without
`seed`, the subject is drawn with seed 0. Gains and mixing cover up to 32 channels.

On the command line, `--subject` and `--session` are shared with the replay source:

```bash
openmind-plug stream --source synthetic --subject 3 --session 2
```

With `--source replay`, `--subject` stays the dataset subject number (default 1)
and `--session` the dataset session name. The subject is logged, not advertised: the
stream metadata (`desc`) is unchanged, as the stream specification requires.

## Replay source

`ReplaySource` plays back a recording as if it were live: a public dataset
downloaded with MOABB, or any file MNE can read (FIF, EDF, BDF, GDF,
BrainVision...). Install the extra first:

```bash
pip install "openmind-plug[replay]"
```

```python
from openmind_plug.sources.replay import ReplaySource

# Public dataset (downloaded on first use)
source = ReplaySource.from_dataset("physionetmi", subject=1, montage="A")

# Any file readable by MNE
source = ReplaySource.from_file("session_raw.fif", montage="A", loop=True)

with source:
    samples, timestamps = source.read(max_samples=64)
    markers = source.read_markers()
```

### Supported datasets

| Key | Dataset | Subjects | Classes | Channels | Rate | Download | Montage C |
|---|---|---|---|---|---|---|---|
| `bnci2014_001` | BCI Competition IV 2a | 9 | left, right, feet, tongue | 22 EEG (no Fp1, no occipital) | 250 Hz | ~0.4 GB | yes (not `A` or `B`: no Fp1, O1, O2) |
| `physionetmi` | PhysioNet EEGMMIDB | 109 | left, right, hands, feet, rest | 64 EEG (10-10) | 160 Hz | ~2 GB | yes (`A` and `B` too) |
| `lee2019_mi` | Lee 2019 (OpenBMI), 2 sessions | 54 | left, right | 62 EEG (10-10) | 1000 Hz | ~70 GB for all, ~1.3 GB per subject | yes |
| `cho2017` | Cho 2017 (GigaDB) | 52 | left, right | 64 EEG (10-10) | 512 Hz | ~0.2 GB per subject | yes |
| `schirrmeister2017` | Schirrmeister 2017 (high-gamma), executed movements | 14 | left, right, feet, rest | 128 EEG | 500 Hz | ~0.3 GB per subject | yes |

Sizes are approximate; `from_dataset` downloads one subject at a time, on first
use. Lee 2019 files are large (1000 Hz, two sessions): start with one subject.
Schirrmeister 2017 records *executed* movements, not imagery. All recordings are
resampled to 250 Hz by default.

Licenses and citations (check the dataset page before redistributing anything):

- `lee2019_mi`: CC BY 4.0 (GigaDB). Lee et al., "EEG dataset and OpenBMI
  toolbox for three BCI paradigms", GigaScience 8(5), 2019,
  doi:10.1093/gigascience/giz002.
- `cho2017`: CC BY 4.0 as listed by MOABB (check the GigaDB page before redistributing). Cho et al., "EEG
  datasets for motor imagery brain-computer interface", GigaScience 6(7), 2017,
  doi:10.1093/gigascience/gix034.
- `schirrmeister2017`: CC BY 4.0. Schirrmeister et al., "Deep learning with
  convolutional neural networks for EEG decoding and visualization", Human Brain
  Mapping 38(11), 2017, doi:10.1002/hbm.23730.

Datasets are downloaded by script into `$OPENMIND_DATA_DIR`, never committed.
Event names map to the same cue labels everywhere (`left_hand` → `left`,
`right_hand` → `right`, `feet` → `feet`, `rest` → `rest`). Montage C
(`FC3, FC4, C3, Cz, C4, CP3, CP4, CPz`) is present in the three new datasets
(checked against the MOABB channel lists); use `montage="C"`.

`from_dataset(dataset, subject=1, session=None, runs=None, ...)` loads the first
session by default and concatenates all its runs, in order.

### Processing

1. **Channel selection.** With a montage, its electrodes are picked in montage
   order; channel names are normalized (`"Fc3."` → `FC3`, `"EEG CZ"` → `Cz`).
   If an electrode is missing, `MissingChannelsError` names it and lists the
   montages that fit the recording (or suggests `montage=None`). Without a
   montage (the default), every EEG channel is kept; EOG, EMG and stim channels
   are dropped.
2. **Units.** Data are converted from volts (MNE convention) to microvolts.
3. **Resampling** to `srate` (250 Hz by default) when the recording differs.
4. **Events → markers.** Annotations (or stim-channel codes, for datasets that
   use them) become cue markers; `BAD_*` and `EDGE_*` annotations are ignored.
   Dataset names are translated (`left_hand` → `left`, `right_hand` → `right`,
   ...):

   ```json
   {"type": "cue", "label": "left", "trial": 12, "source": "replay"}
   ```

   `trial` counts cues from 1 and keeps counting across loops.

   Annotations written by the recorder (`state/left`, `artifact/blink`...) are
   replayed with their own type and label, without `trial`. Recordings also
   restore their model, reference and montage (see [recording](recording.md)).

### Playback

- `read()` releases samples at the pace of the LSL clock; `speed=10.0` plays ten
  times faster (timestamps follow the accelerated clock, so EEG and markers stay
  aligned).
- Without `loop`, the stream ends with the recording (`finished` becomes true and
  `read()` returns empty blocks); with `loop=True`, it restarts seamlessly.
- After `stop()` and `start()`, playback resumes where it stopped.

### Where data go

Downloads are stored in `$OPENMIND_DATA_DIR/mne_data` (default
`~/openmind-data/mne_data`), never in the repository. Plug sets MNE's
`MNE_DATASETS_*_PATH` variables only while loading, so your global MNE
configuration is left untouched.

## BrainFlow source

[BrainFlow](https://brainflow.org) drives many EEG boards (OpenBCI Cyton and
Ganglion, Muse, Neurosity, g.tec Unicorn...) behind one API, and includes a
**synthetic board** that needs no hardware. `BrainFlowSource` exposes the EEG
channels of a BrainFlow board as a standard source.

```bash
pip install "openmind-plug[brainflow]"

# BrainFlow's synthetic board, no hardware
openmind-plug stream --source brainflow --board synthetic

# A real board: connection parameters, wired electrodes, montage
openmind-plug stream --source brainflow --board cyton \
    --board-param serial_port=/dev/ttyUSB0 \
    --labels FC3,FC4,C3,Cz,C4,CP3,CP4,CPz --montage C
```

```python
from openmind_plug.sources.brainflow import BrainFlowSource

source = BrainFlowSource(
    "CYTON_BOARD",
    params={"serial_port": "/dev/ttyUSB0"},
    labels=["FC3", "FC4", "C3", "Cz", "C4", "CP3", "CP4", "CPz"],
    montage="C",
)
```

- `--board` is a `BoardIds` name (case-insensitive, the `_BOARD` suffix is
  optional) or a numeric id. Default: `synthetic`.
- `--board-param KEY=VALUE` (repeatable) sets an attribute of BrainFlow's
  `BrainFlowInputParams` (`serial_port`, `mac_address`, `ip_address`...). Numeric
  values are passed as integers, others as strings.
- Only the board's EEG rows are read; BrainFlow gives them in microvolts.
- **Labels.** `--labels` names the electrodes you wired, in board input order,
  and must give one label per EEG input of the board. Without it, plug uses the
  electrode names BrainFlow documents for the board (`Ch1`, `Ch2`... if there are
  none). Those defaults describe the board's standard cap, not necessarily
  yours: give `--labels` whenever the wiring differs.
- **Montage.** `--montage` selects and orders channels by label, so the labels
  must include every electrode of the montage; otherwise the error lists the
  missing ones.
- **Timestamps.** BrainFlow stamps samples with the Unix clock; they are moved
  to the LSL clock on each read, so the stream is consistent with the markers.
- `list-sources` shows `brainflow` as available only when the extra is installed.

## IronBCI-8 source (experimental)

`IronBCISource` reads the IronBCI-8 headset (ADS1299, 8 channels, 250 Hz) over
Bluetooth LE with `bleak`, entirely locally. **Experimental:** written from the
public documentation, not validated with the hardware until PLUG-090; plug logs a
warning at start. Protocol notes, assumptions and the PiEEG Server alternative
are in `docs/hardware/ironbci.md`.

```bash
pip install "openmind-plug[ironbci]"
openmind-plug stream --source ironbci --montage C
openmind-plug stream --source ironbci --ble-address <address>
```

- `--ble-name` (default: `EAREEG` or `IronBCI`) and `--ble-address` (skips the
  scan; a UUID on macOS).
- `--montage` names the 8 inputs in order (default `C`); other montages must
  have exactly 8 electrodes.
- `--gain` (default 1) divides the ADC counts; the default reproduces the
  vendor's scale (about 0.268 uV per count), which is unconfirmed.
- `--ble-counter` declares an assumed one-byte packet counter at the start of
  each notification and enables lost-packet detection (the vendor documents no
  counter).
- **Timestamps.** The first notification anchors the LSL clock once; samples are
  then stamped `t0 + index / srate`, skipping the samples of lost packets.
- `list-sources` shows `ironbci` as `experimental` once `bleak` is installed.

```python
from openmind_plug.sources.ironbci import IronBCIConfig, IronBCISource

source = IronBCISource(address="...", montage="C")
# Every hardware assumption (names, characteristic, rate, channel order,
# reference, scale, counter) is a field of IronBCIConfig:
source = IronBCISource(address="...", config=IronBCIConfig(gain=24.0, counter=True))
```

Tests use `tests/fake_ironbci.py`, a simulated headset injected through the
`scanner` and `client_factory` arguments, so they need no Bluetooth.

To relay PiEEG Server's local LSL stream instead, see the last section of
`docs/hardware/ironbci.md` (`--source lsl`).

## LSL relay

`LSLSource` takes a third-party LSL stream (OpenBCI GUI, BrainFlow, PiEEG Server,
vendor apps...) and exposes it as a source, so plug republishes it as the
standard `openmind-eeg` stream in microvolts.

```bash
openmind-plug stream --source lsl --lsl-name obci_eeg1 --unit uV \
    --rename "EXG Channel 0=Fp1,EXG Channel 1=FC3" --montage A
```

```python
from openmind_plug.sources.lsl_in import LSLSource

source = LSLSource(name="obci_eeg1", rename={"EXG Channel 0": "Fp1"}, unit="uV")
```

| Aspect | Behavior |
|---|---|
| Selection | By `name`, `stream_type` (default `EEG`) and/or `source_id`. The standard `openmind-eeg` stream is never picked, to avoid loops. |
| Labels | From `channels/channel/label`, renamed with `rename`, then normalized (`"Fc3."` → `FC3`). Missing labels become `Ch1`, `Ch2`... Duplicates are an error. |
| Units | `unit=` if given; else the description (`microvolts`, `uV`, `mV`, `volts`...); else microvolts are assumed, with a warning. The outlet converts to µV. |
| Types | From the description when known (`EEG`, `EOG`, `EMG`, `ECG`, `MISC`), `EEG` when absent, `MISC` otherwise. |
| Montage | Optional; selects and orders channels; missing labels are listed with a hint to use `rename`. |
| Timestamps | LSL clock synchronization and de-jittering (`proc_clocksync`, `proc_dejitter`). |
| Metadata | `model` = upstream stream name, `driver` = `lsl`, `manufacturer` and `reference` from the description when present. |

Only regular streams (nominal rate > 0) are relayed.
