# mcap-repair

Tools for recovering GenRobt DAS device `.mcap` recordings that are broken in ways the
recorder can't fix on its own, without losing any data that wasn't already
unrecoverable.

If a DAS-Gripper, DAS-Ego, or DAS-Finger recording throws a decode error in
Matrix Studio, Monitor, or your own MCAP tooling, or its timeline has a
multi-hour jump in it, these scripts will tell you exactly why, and fix what
can be fixed automatically.

| Script | Fixes | Input / output |
|---|---|---|
| `repair_mcap.py` | Schema corruption and corrupted chunks (file won't decode) | one `.mcap` in, one `.mcap` + `.report.json` out |
| `fix_timestamp_jump.py` | Clock steps that leave a multi-hour jump in `log_time` (forward or backward) | one `.mcap` in, one `.mcap` out (analysis-only mode if no output given) |

If a file has both problems, run `repair_mcap.py` first so the file decodes
cleanly, then run `fix_timestamp_jump.py` on the repaired output.

## Installation

```
git clone https://github.com/genrobot-ai/mcap-repair.git
cd mcap-repair
```

**Linux / WSL2 (Ubuntu/Debian):**

Recent Ubuntu blocks system-wide pip installs, so use a virtual environment
(or add `--break-system-packages`):

```
python3 -m venv ~/venv
source ~/venv/bin/activate
pip install -r requirements.txt
```

Run `source ~/venv/bin/activate` again in any new shell before running the scripts.

**Windows / macOS:**

```
pip install -r requirements.txt
```

Requires Python 3.9+. Both scripts use the same `requirements.txt`.

---

# repair_mcap.py: schema and chunk corruption

## What it fixes

| Symptom | Root cause | Recoverable? |
|---|---|---|
| **Schema corruption**: file won't decode in strict/schema-aware tools | A Schema record's embedded protobuf `FileDescriptorSet` has a few extra garbage bytes appended after an otherwise complete, valid descriptor. Strict protobuf parsers require the whole buffer to be consumed and reject the schema outright, even though nothing is actually missing or damaged. | **Yes, fully and losslessly.** No message data is touched. |
| **"Truncation" / mid-file chunk corruption**: decode fails partway through, sometimes reported as `unexpected EOF` | One or more `Chunk` records have a corrupted compressed payload (failed decompression or CRC), typically from a brief onboard-storage write interruption during recording. This is often *not* the file ending early. The footer/summary can be fully valid, and the bad chunk(s) can sit anywhere in the file, including the middle. | **Partially.** The corrupted chunk(s) are genuinely unrecoverable and are dropped; every other chunk is carried through unaffected. |

A single file can have both problems at once. The tool detects and repairs
both in one pass, and tells you exactly what it did.

## How it works

- Reads the file's own **Summary index** (schema/channel table + each
  chunk's file offset) instead of scanning the file byte-by-byte start to
  end. A corrupted chunk can desync a purely sequential parser for
  everything after it. Seeking chunk-by-chunk via the Summary means one bad
  region never takes the rest of a good file down with it.
- For each **Schema** record: attempts to parse it as a protobuf
  `FileDescriptorSet`. On failure, retries with 1-8 trailing bytes trimmed
  until it parses cleanly, and keeps that fixed version. Non-protobuf
  schemas, or schemas that still don't parse after trimming, are passed
  through unchanged and flagged in the report, never silently dropped.
- For each **Chunk**: attempts to decompress and CRC-validate it. A failing
  chunk is dropped and logged with its time range; every other chunk's
  messages are carried through into the output unchanged.
- Writes a new, valid `.mcap` file plus a `<output>.report.json` describing
  exactly what was fixed or dropped. Nothing happens silently.

## Usage

```
python3 repair_mcap.py <input.mcap> <output.mcap>
```

One input/output pair per run. It does not take a folder or wildcard
directly. To process a whole directory of recordings:

**Linux / WSL2 / macOS:**

```
mkdir -p output
for f in input/*.mcap; do
    python3 repair_mcap.py "$f" "output/fixed_$(basename "$f")"
done
```

**Windows PowerShell:**

```
mkdir output
Get-ChildItem input\*.mcap | ForEach-Object {
    python repair_mcap.py $_.FullName "output\fixed_$($_.Name)"
}
```

## Reading the output

Each run prints a summary and writes `<output>.report.json` next to the
repaired file:

```
Repair complete.
  Chunks total:             104
  Messages written:         22249
  Schemas repaired:         1
    - schema 3 (foxglove.IMUMeasurement): trimmed 3 trailing byte(s)
  Schemas unrepairable:     0
  Chunks dropped (data loss): 2
    - dropped ~0.66s of data at offset 27982340 (ZstdError: ...)
    - dropped ~0.73s of data at offset 30066774 (UnicodeDecodeError: ...)
  Full report written to: output/fixed_recording.mcap.report.json
```

- **Schemas repaired**: lossless fixes, nothing to worry about.
- **Schemas unrepairable**: a schema that stayed broken even after
  trimming; original bytes are passed through as-is, and the affected
  channel(s) may still fail to decode in strict downstream tools. Rare;
  worth reporting to GenRobot support if you see this.
- **Chunks dropped**: genuine, unrecoverable data loss for that time
  window. Everything else in the file is unaffected.

Always point downstream tools (Monitor, Matrix Studio, das-datakit, etc.) at
the **repaired output file**, not the original, when verifying a fix.

## Limitations

- If a file's Summary section itself is missing or unreadable, the tool
  raises an error rather than guessing. That's a more severe failure mode
  than what this tool currently targets, and needs full linear recovery
  instead.
- Repairing a schema does not change its `id` or any message data
  referencing it. Decoders keyed by topic/name won't see any difference
  beyond the fixed schema itself.
- This tool does not attempt to recover data inside a dropped chunk. If you
  need that, the raw (still-corrupted) chunk bytes remain in the original
  file for offline forensic recovery, but that isn't automated here.

---

# fix_timestamp_jump.py: multi-hour timestamp jumps

**Temporary workaround, not a root-cause fix.** Flag any file repaired with
this tool in your delivery/QC notes.

## The problem

Some recordings contain a large clock step partway through, usually several
hours (for example 6h51m), when the device clock gets corrected mid-recording.
Downstream tools then see a multi-hour hole in the timeline, or messages that
appear to go backward in time.

The step doesn't always hit every channel. In one DAS-Gripper recording the
IMU stayed on the correct clock while the encoder, cameras, and system_info
channels stepped forward by ~6h51m about 11 seconds in. In file order that
looks like a backward jump, but it is really a clock step on some channels.
The tool handles that case, plus plain forward and backward jumps.

## How it works

1. Splits the recording into **clusters** of time separated by gaps larger
   than a threshold (default 10 s) in the merged, time-sorted stream.
2. Works out the **true order** of the clusters from the order they appear
   in the file (file order is recording order).
3. For each boundary between consecutive clusters, estimates the shift that
   makes the timeline continuous (last sample + nominal interval = first
   sample of the next cluster), using every channel that spans the boundary.
   The channel with the smallest interval is used as the reference, and the
   spread across channels is reported as a sanity check.
4. Writes a **new** `.mcap` with the shifts applied. The original is never
   modified.

## What it changes

- Message `log_time`, and `publish_time` when it is on the same clock as `log_time`
- Attachment `log_time` / `create_time` (same rule)
- Adds a `timestamp_jump_fix` metadata record to the output (disable with `--no-marker`)

It does **not** touch message payloads. If the protobuf payloads carry their
own timestamps (for example `header.stamp` inside camera or IMU messages),
those are left as-is.

## Usage

Analyse only (safe, writes nothing):

```
python3 fix_timestamp_jump.py input/recording.mcap
```

Write a repaired copy:

```
mkdir -p output
python3 fix_timestamp_jump.py input/recording.mcap -o output/recording_fixed.mcap
```

Options:

| Flag | Meaning |
|---|---|
| `-o, --output` | Write the repaired file here. Omit for analysis only. |
| `--anchor first` | Keep the first cluster's clock and shift later data (default). |
| `--anchor last` | Keep the last cluster's clock and shift earlier data. |
| `--gap-threshold-s N` | Gap size in seconds treated as a jump (default 10). |
| `--offset-ns N` | Force the shift (ns, added to the later cluster) instead of estimating. Only valid with exactly 2 clusters. |
| `--no-marker` | Don't add the `timestamp_jump_fix` metadata record. |

Which clock to keep: the recording's start time is usually in the filename
(for example `DAS-Gripper_20260915132447_...` is 2026-09-15 13:24:47).
Compare it to the raw range the tool prints, and use `--anchor last` if the
first cluster turns out to be the wrong clock.

To process a whole folder:

```
mkdir -p output
for f in input/*.mcap; do
    python3 fix_timestamp_jump.py "$f" -o "output/fixed_$(basename "$f")"
done
```

## Reading the output

```
== Clusters in recording order (2) ==
  #1:    15681 msgs  raw 1789503887623920000 -> 1789503958082962000  (0h01m10.459s)
  #2:     8322 msgs  raw 1789528593065889000 -> 1789528652168350000  (0h00m59.102s)

== Boundary 1: cluster #1 -> #2 (FORWARD jump) ==
  /robot0/sensor/magnetic_encoder     nominal dt=   20.000 ms | boundary after fix=   20.000 ms  <- reference
  /robot0/sensor/camera2/compressed   nominal dt=   33.334 ms | boundary after fix=   33.349 ms
  offset estimate spread across channels: 0.614 ms
  => shift applied to later cluster: -6h51m34.097s
```

- **boundary after fix** should land close to each channel's nominal `dt`.
  A `!! check` flag means that channel lost frames across the jump or has an
  unusual rate. Look at it before trusting the output.
- **offset estimate spread** should be tiny (around a millisecond). A large
  spread means the channels disagree about the size of the step.
- When writing, the tool re-scans the output and prints a **Verification**
  block. You want `RESULT: PASS`, the same message count as the input, and no
  remaining gaps or backward jumps.

## Limitations

- Assumes no frames were lost across the step (the boundary interval should
  be about one normal interval).
- Assumes the clock step is seen at the same moment by every channel that
  jumps. Channels that never jump (like the IMU in the example above) are
  handled by the clustering.
- Payload timestamps are not rewritten.
- Output is written in file order, not re-sorted by `log_time`.

---

## Troubleshooting

**`ModuleNotFoundError: No module named 'mcap'`**
Dependencies aren't installed in the Python environment you're running from.
Re-run the install command for your OS above (and activate your venv if you
use one), then confirm with `python3 -c "import mcap; print(mcap.__file__)"`.

**`error: externally-managed-environment` (Ubuntu / WSL)**
Use a virtual environment as shown in Installation, or add
`--break-system-packages` to the pip command.

**`pip` / `python` not recognized (Windows)**
Python isn't installed, or isn't on PATH. Install it from
[python.org/downloads](https://www.python.org/downloads/) (check "Add
python.exe to PATH" during setup) or run `winget install Python.Python.3.12`,
then reopen your terminal.

**`Input/output error` on `/mnt/c` in WSL**
The Windows drive failed to mount. Run `wsl --shutdown` from PowerShell and
reopen WSL. If that doesn't help, run `sudo mount -t drvfs C: /mnt/c`.
Copying the project into your Linux home folder (`~/`) also avoids the issue.

**`FileNotFoundError` when running a script**
Double-check the path to your input file with `ls` (or `dir` on Windows).
The script needs the exact relative or absolute path, including any
subfolder like `input/`.