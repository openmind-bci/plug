# IronBCI-8 (experimental)

> **Experimental.** The `ironbci` driver was written from the public documentation
> only, without the headset. It stays experimental until the hardware validation
> task (PLUG-090) is done. Research and experimentation project, not a medical
> device. The headset must only run on its own battery, never while charging
> (vendor safety note).

## Protocol notes

What the vendor documents, and where. Everything is local Bluetooth LE between
the headset and this computer. Plug never uses PiEEG Cloud or browser streaming.

Sources (read on 2026-11-22):

- Repository README: <https://github.com/pieeg-club/ironbci> (also reachable as
  `Ildaron/ironbci`)
- Specification: <https://docs.pieeg.com/hardware/devices/ironbci/specification/>
- Python SDK scripts: `SDK/BLE_General/2.Read_data.py` and
  `3.Read_data_graph.py` in the repository above
- PiEEG Server: <https://github.com/pieeg-club/PiEEG-server>

### Licensing

- The IronBCI repository has **no license file** (the GitHub API reports none);
  the README only mentions an MIT licence for the Android APK. Without a license
  the SDK scripts cannot be assumed compatible with Apache-2.0, so **no code was
  copied**: `sources/ironbci.py` re-implements the documented format. Facts
  (UUID, byte layout, scale) are taken from the documentation.
- PiEEG Server is MIT licensed (its optional `pieeg-core` accelerator is
  AGPL-3.0-or-later and opt-in). Plug does not depend on it.

### Device and transport

| Item | Documented value |
|---|---|
| ADC | Texas Instruments ADS1299, 24 bits, 8 channels sampled simultaneously |
| Sampling rate | 250 SPS |
| Inputs | 8 biosignal inputs, plus a reference and a bias ear clip |
| Advertised name | `EAREEG` (some units `IronBCI` / `IRONBCI`); the SDK matches `EAREEG` exactly |
| Pairing | Not needed: connect and subscribe |
| Data characteristic | `0000fe42-8e22-4541-9d4c-21edae82ed19`, **notify** |
| Start command | None: the SDK only calls `start_notify`, it never writes to the device |
| Python client | `bleak` (`BleakScanner` by name, `BleakClient(address)`) |
| PiEEG Server options | `--device ironbci8`, `--ble-name` (default `EAREEG`), `--ble-address` |

### Packet format

- A notification holds one or more samples back to back; the number of samples is
  `len(data) / 24`.
- One sample is 8 channels x 3 bytes = 24 bytes, channel 1 first, no header, no
  trailer.
- Each value is a 24-bit **big-endian two's complement** integer.
- **No packet counter or timestamp is documented**, and the SDK reads none.

### Conversion to microvolts

Official formula (spec page and SDK):

```
raw = (b0 << 16) | (b1 << 8) | b2
if sign bit set: raw -= 16777214        # sic
uV  = raw * 4.5 / 16777215 * 1e6        # about 0.268 uV per count
```

- The constant `16777214` looks like a typo for `2**24 = 16777216` (it shifts
  negative values by 2 counts, about 0.5 uV). Plug uses the correct sign
  extension (`2**24`).
- No gain term appears: 4.5 V is divided by `2**24 - 1` counts. For comparison,
  the ADS1299 datasheet gives an LSB of `2 * Vref / (gain * 2**24)`. Plug applies
  `uV = counts * 1e6 * vref / (2**24 - 1) / gain` with `vref = 4.5` and
  `gain = 1` by default, which reproduces the official scale, and exposes `gain`
  and `vref` to correct it.
- Raw values carry a DC offset of thousands of microvolts from electrode
  contact; the vendor recommends a 1-40 Hz band-pass before viewing. Plug does not
  filter: filtering belongs to `core`.

### Assumptions to check with the headset (PLUG-090)

The driver reads every one of them from `IronBCIConfig`
(`openmind_plug.sources.ironbci`), so a measured value is changed in one place:

| Assumption | `IronBCIConfig` field | Default |
|---|---|---|
| 1. Gain and scale | `gain`, `vref` | `1.0`, `4.5` V |
| 2-3. Packet counter, samples per notification | `counter` | `False` |
| 4. Timestamps (device clock) | `srate` | `250.0` Hz |
| 5. Channel order | `montage` | `"C"` |
| 6. Reference and bias | `reference` | `"unknown"` |
| 7. Advertised name | `ble_names` | `("EAREEG", "IronBCI")` |
| GATT data characteristic | `data_characteristic` | `0000fe42-...` |

1. **Gain and scale.** The official scale (0.268 uV/count) may be wrong by a
   factor 2 and/or by the PGA gain. Check by injecting a known sine (function
   generator through a divider, or the ADS1299 internal test signal if exposed)
   and compare the measured amplitude; set `--gain` accordingly.
2. **No packet counter.** Lost packets cannot be detected from the documented
   format. The driver supports an *assumed* layout (one unsigned counter byte in
   front of the samples, +1 per notification, wrapping at 256) behind
   `--ble-counter`; whether the firmware sends one is unknown. Without a
   counter, check the effective rate on a long recording instead
   (`openmind-plug quality`).
3. **Samples per notification.** Unknown (one sample or a batch); the driver
   accepts any multiple of 24 bytes and drops other sizes (counted as malformed).
   With a counter, the lost samples are estimated as `lost_packets x` the size of
   the packet that revealed the loss.
4. **Timestamps.** Notifications arrive in bursts, so the driver anchors the LSL
   clock once on the first notification (`t0 = arrival - n / srate`) and stamps
   `t0 + index / srate`. This assumes the device clock is exactly 250 Hz and no
   sample is lost silently. Real clock drift (crystal tolerance, BLE stalls) is
   not corrected: measure it over 10 minutes; re-anchoring may be needed.
5. **Channel order.** Channel 1 is the first 3 bytes; which electrode pin each
   input corresponds to is not documented. The driver labels inputs from montage
   `C` (`FC3, FC4, C3, Cz, C4, CP3, CP4, CPz`) in that order. Confirm the wiring.
6. **Reference and bias.** The stream metadata says `reference = "unknown"`.
7. **Advertised name** may differ between units; use `--ble-name` or
   `--ble-address` otherwise. On macOS the address is a UUID, not a MAC address.
8. **Bleak backends.** Behaviour of `bleak` on Linux (BlueZ), macOS and Windows
   with this device, connection stability and any MTU effect on the payload size
   are untested.
9. **Connection loss.** The driver stops with an error when `bleak` reports a
   disconnection; automatic reconnection is not implemented.

## Using the driver

```bash
pip install "openmind-plug[ironbci]"
openmind-plug list-sources             # ironbci: experimental
openmind-plug stream --source ironbci --montage C
openmind-plug stream --source ironbci --ble-address <address> --montage C
```

Options: `--ble-name`, `--ble-address`, `--gain` (default 1), `--ble-counter`.
Without `--montage`, montage `C` is used; the montage must have 8 electrodes.
See `docs/sources.md` for details.

## Alternative: relay PiEEG Server (local)

If the BLE driver misbehaves, PiEEG Server can read the headset and publish an
LSL stream that plug relays with the existing `lsl` source. Keep it local:

```bash
pip install "pieeg-server[ironbci]"
pieeg-server --device ironbci8 --lsl --host 127.0.0.1     # no cloud, local only
openmind-plug stream --source lsl --lsl-name PiEEG --unit uV --montage C \
    --rename "<label 1>=FC3,<label 2>=FC4,<label 3>=C3,<label 4>=Cz,<label 5>=C4,<label 6>=CP3,<label 7>=CP4,<label 8>=CPz"
```

- Documented: the LSL outlet is enabled by `--lsl`, named `PiEEG` by default
  (`--lsl-name` changes it), type `EEG` (from the receive example); the server
  runs at 250 Hz. `--host` sets the bind address (default `0.0.0.0`, so pass
  `127.0.0.1`).
- **Not documented:** channel labels in the LSL description, units, nominal rate
  and `source_id`. Run `openmind-plug stream --source lsl --lsl-name PiEEG` once
  without `--rename` and read the labels it reports (otherwise `Ch1`...`Ch8`), then
  build the rename map. If the stream carries no unit, `--unit uV` is required
  (the WebSocket frames are documented in microvolts; the LSL outlet is assumed
  to match). No automated test exists for this path for the same reason.

## Validation checklist (PLUG-090)

Done by the user with the headset; `examples/validate_ironbci.py` guides the
steps and writes a Markdown report. Rehearse it first, without the headset:

```bash
uv run python examples/validate_ironbci.py --source synthetic --subject P01 --yes --duration 10
```

With the headset (battery only, never charging; Wi-Fi off):

```bash
uv sync --extra ironbci
uv run python examples/validate_ironbci.py --source ironbci --subject P01 \
    [--ble-name NAME | --ble-address ADDRESS] [--ble-counter]
```

Use `--steps a,b` to run only some steps and `--label ref-mastoid` to tag a run
(for instance a REF/BIAS placement). Reports go to
`$OPENMIND_DATA_DIR/validation/` (default `~/openmind-data/validation/`); the
script refuses to write inside a Git repository. The subject is a pseudonym
(`P01`), and the report holds figures only, never EEG samples. Stop any other
OpenMind stream on the network before step f.

| # | Item | Script step | Expected | If it fails |
|---|---|---|---|---|
| 1 | Bluetooth connection without internet | a | Samples arrive within a few seconds with Wi-Fi and Ethernet off; the script reports no internet route | No device found: try `--ble-name` / `--ble-address` (assumption 7), check the battery, unpair from the phone app. Internet still reachable: cut it and rerun. Connection drops: assumptions 8 and 9 |
| 2 | Effective rate and lost packets over 10 min | b | Rate within 2 % of 250 Hz, no gap above 1.5 samples, drift of the arrival-vs-timestamp lag small (a few tens of ms per 10 min), 0 lost and 0 malformed packets | Rate off: the device clock is not 250 Hz, use the measured rate (assumption 4). Drift: re-anchor timestamps periodically. Malformed packets: the notification size is not a multiple of 24 bytes (assumptions 2 and 3). Lost packets are only counted with `--ble-counter`, which assumes a counter byte; if the count is absurd the layout guess is wrong, drop the option |
| 3 | Channel order | c | Each tapped electrode is the strongest channel, in the order `FC3, FC4, C3, Cz, C4, CP3, CP4, CPz` | The script prints the observed order: update the labels in the driver (assumption 5) |
| 4 | REF/BIAS pin-out | manual | Ear clips match the vendor diagram; the placement is written in the report | Fix the wiring; set `reference` in the stream metadata (assumption 6) |
| 5 | Alpha test | d | Alpha rises at least x2 with eyes closed on the best channel | Weak on montage C (no posterior electrode): move one electrode to Pz/O1 and repeat, or judge on the other checks. A failure on a posterior electrode points to contact or scale problems (assumption 1) |
| 6 | Blink artifact | manual | Slow large peak on frontal electrodes (Fp1 if worn, otherwise FC3 and FC4) | Check contact; the blink fallback relies on FC3/FC4 |
| 7 | Double jaw clench on the 8 motor electrodes | e | Exactly two bursts in at least 80 % of the rounds, and visible on every channel | Few or no bursts: check contact, REF/BIAS placement (below), then compare with the reference detector in OpenMind Core. The detector in the script is a rough high-frequency envelope threshold, not the reference |
| 8 | No false jaw triggers | manual | Talking, eating, swallowing do not give a clean double burst | Record these situations with `openmind-plug record` (outside any repository) and tune the detector in Core, not here |
| 9 | Effect of REF/BIAS position | e, twice | Report the number of detected rounds for each placement | See below |
| 10 | Latency to LSL | f | p95 sample age at the LSL inlet below 250 ms | Large values point to driver buffering (BLE bursts) |

**REF and BIAS placement.** The ear clips set the common reference, and muscle
activity from the jaw is large near the mastoids and temples. A clip close to the
masseter or temporalis injects the clench into every channel at once (the jaw
check then looks easy but is partly common-mode), while a distant, quiet site
gives cleaner motor channels and a clench seen mainly on the lateral ones.
Run step e once per placement with `--label` (for example `ref-mastoid`,
`ref-earlobe`) and keep the placement that gives the most reliable double clench
without false triggers. Write the final placement in the stream's `reference`.

**What step f measures.** The age of each sample, on the LSL clock, when an inlet
receives it: driver buffering plus LSL transport (the marker transit is the
transport alone). The driver anchors its timestamps on the arrival of the first
notification, so the Bluetooth transit itself is not included; measuring it would
need an external trigger (for example a known signal injected at the input).

## After validation

1. Record the figures of each run in the PLUG-090 task and keep the reports
   outside the repositories.
2. Update the defaults of `IronBCIConfig` in `sources/ironbci.py` from the
   results: channel order (`montage`, assumption 5), `reference` (assumption 6),
   `gain` or `vref` (assumption 1), `srate` (assumption 4), `counter`
   (assumptions 2 and 3), `ble_names` (assumption 7). Timestamp re-anchoring
   (assumption 4) and reconnection (assumption 9) are code changes.
3. Update the tests with the real packet sizes and counter layout.
4. Rewrite the "Assumptions" section: confirmed facts move to the protocol notes,
   unresolved points stay listed.
5. If every item passes, remove the `experimental` status (the warning in the
   driver, `list-sources`, the CLI message, the README and `docs/sources.md`),
   and add a CHANGELOG entry.
