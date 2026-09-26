# mcap-repair

Recover DAS device `.mcap` recordings that fail to decode due to schema
corruption or mid-file chunk corruption — without losing any data that
wasn't already unrecoverable.

If a DAS-Gripper, DAS-Ego, or DAS-Finger recording throws a decode error in
Matrix Studio, Monitor, or your own MCAP tooling, this tool will tell you
exactly why, and fix what can be fixed automatically.

## What it fixes

| Symptom | Root cause | Recoverable? |
|---|---|---|
| **Schema corruption** — file won't decode in strict/schema-aware tools | A Schema record's embedded protobuf `FileDescriptorSet` has a few extra garbage bytes appended after an otherwise complete, valid descriptor. Strict protobuf parsers require the whole buffer to be consumed and reject the schema outright — even though nothing is actually missing or damaged. | **Yes — fully, losslessly.** No message data is touched. |
| **"Truncation" / mid-file chunk corruption** — decode fails partway through, sometimes reported as `unexpected EOF` | One or more `Chunk` records have a corrupted compressed payload (failed decompression or CRC), typically from a brief onboard-storage write interruption during recording. This is often *not* the file ending early — the footer/summary can be fully valid, and the bad chunk(s) can sit anywhere in the file, including the middle. | **Partially.** The corrupted chunk(s) are genuinely unrecoverable and are dropped; every other chunk is carried through unaffected. |

A single file can have both problems at once — the tool detects and repairs
both in one pass, and tells you exactly what it did.

## How it works

- Reads the file's own **Summary index** (schema/channel table + each
  chunk's file offset) instead of scanning the file byte-by-byte start to
  end. A corrupted chunk can desync a purely sequential parser for
  everything after it — seeking chunk-by-chunk via the Summary means one bad
  region never takes the rest of a good file down with it.
- For each **Schema** record: attempts to parse it as a protobuf
  `FileDescriptorSet`. On failure, retries with 1–8 trailing bytes trimmed
  until it parses cleanly, and keeps that fixed version. Non-protobuf
  schemas, or schemas that still don't parse after trimming, are passed
  through unchanged and flagged in the report — never silently dropped.
- For each **Chunk**: attempts to decompress and CRC-validate it. A failing
  chunk is dropped and logged with its time range; every other chunk's
  messages are carried through into the output unchanged.
- Writes a new, valid `.mcap` file plus a `<output>.report.json` describing
  exactly what was fixed or dropped — nothing happens silently.

## Installation

```bash
git clone https://github.com/genrobot-ai/mcap-repair.git
cd mcap-repair
```

**Linux / WSL2 (Ubuntu/Debian):**
```bash
pip3 install -r requirements.txt --break-system-packages
```

**Windows / macOS:**
```bash
pip install -r requirements.txt
```

Requires Python 3.9+.

## Usage

```bash
python3 repair_mcap.py <input.mcap> <output.mcap>
```

One input/output pair per run — it does not take a folder or wildcard
directly. To process a whole directory of recordings:

**Linux / WSL2 / macOS:**
```bash
mkdir -p output
for f in input/*.mcap; do
    python3 repair_mcap.py "$f" "output/fixed_$(basename "$f")"
done
```

**Windows PowerShell:**
```powershell
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

- **Schemas repaired** — lossless fixes, nothing to worry about.
- **Schemas unrepairable** — a schema that stayed broken even after
  trimming; original bytes are passed through as-is, and the affected
  channel(s) may still fail to decode in strict downstream tools. Rare —
  worth reporting to GenRobot support if you see this.
- **Chunks dropped** — genuine, unrecoverable data loss for that time
  window. Everything else in the file is unaffected.

Always point downstream tools (Monitor, Matrix Studio, das-datakit, etc.) at
the **repaired output file**, not the original, when verifying a fix.

## Limitations

- If a file's Summary section itself is missing or unreadable, the tool
  raises an error rather than guessing — that's a more severe failure mode
  than what this tool currently targets, and needs full linear recovery
  instead.
- Repairing a schema does not change its `id` or any message data
  referencing it — decoders keyed by topic/name won't see any difference
  beyond the fixed schema itself.
- This tool does not attempt to recover data inside a dropped chunk. If you
  need that, the raw (still-corrupted) chunk bytes remain in the original
  file for offline forensic recovery, but that isn't automated here.

## Troubleshooting

**`ModuleNotFoundError: No module named 'mcap'`**
Dependencies aren't installed in the Python environment you're running from.
Re-run the install command for your OS above, then confirm with
`python3 -c "import mcap; print(mcap.__file__)"`.

**`pip` / `python` not recognized (Windows)**
Python isn't installed, or isn't on PATH. Install it from
[python.org/downloads](https://www.python.org/downloads/) (check "Add
python.exe to PATH" during setup) or via the Microsoft Store, then reopen
your terminal.

**`FileNotFoundError` when running the script**
Double-check the path to your input file with `ls` (or `dir` on Windows) —
the script needs the exact relative or absolute path, including any
subfolder like `input/`.
