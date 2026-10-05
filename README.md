# mySTDFcompanion

A command-line tool for parsing and manipulating **STDF v4** (Standard Test Data Format) files.  
Built on top of [pystdf](https://pypi.org/project/pystdf/).

---

## Features

| Command   | Description |
|-----------|-------------|
| `merge`   | Combine 2 or more STDF files into a single output file |
| `check`   | Validate an STDF file and report all structural, sequence, and content errors |
| `repair`  | Automatically repair a malformed STDF file and write a corrected copy |
| `inspect` | Dump all records with field values in human-readable text, summary table, or JSON |

---

## Requirements

- Python 3.8+
- Windows / Linux / macOS

---

## Installation

```bash
# From the repo root
pip install .
```

This installs the `stdfcompanion` command on your PATH.

### Development install

```bash
pip install -e ".[dev]"
```

---

## Usage

### `merge` — combine multiple STDF files

```
stdfcompanion merge [OPTIONS] INPUTS...

Options:
  -o, --output PATH   Output STDF file path.  [required]
  -v, --verbose       Print progress information.
```

```bash
stdfcompanion merge lot1.stdf lot2.stdf -o merged.stdf
stdfcompanion merge a.stdf b.stdf c.stdf -o combined.stdf --verbose
```

---

### `check` — validate an STDF file

```
stdfcompanion check [OPTIONS] INPUTS...

Options:
  --warnings / --no-warnings   Include WARNING issues (default: on)
  --info / --no-info           Include INFO issues (default: off)
  --json                       Output results as JSON
  -v, --verbose                Print progress information.
```

Exit code is **0** when no ERROR-level issues are found, **1** otherwise — suitable for CI pipelines.

```bash
# Check a single file
stdfcompanion check myfile.stdf

# Check multiple files, show all warnings, output as JSON
stdfcompanion check a.stdf b.stdf --warnings --json

# Suppress warnings (errors only)
stdfcompanion check myfile.stdf --no-warnings
```

**Example output**

```
============================================================
File: bad.stdf
============================================================
[ERROR] E012: MRR (Master Results Record) is missing
[ERROR] E015: PIR for head=1 site=1 at offset 32 has no matching PRR [offset=32]
[WARNING] E030: MIR field LOT_ID is empty or missing [offset=6] in MIR

  FAIL  2 error(s)  1 warning(s)  0 info(s)
```

#### Error codes

| Code | Severity | Description |
|------|----------|-------------|
| E001 | ERROR    | File too small to be a valid STDF |
| E002 | ERROR    | First record is not a FAR |
| E003 | ERROR    | FAR STDF_VER ≠ 4 |
| E004 | WARNING  | FAR CPU_TYPE is non-standard |
| E005 | ERROR    | Truncated record / file |
| E006 | WARNING  | Unknown record type/subtype |
| E010 | ERROR    | MIR missing |
| E011 | ERROR    | Duplicate MIR |
| E012 | ERROR    | MRR missing |
| E013 | ERROR    | MRR is not the last record |
| E014 | WARNING  | No PCR found |
| E015 | ERROR    | Unmatched PIR (no closing PRR) |
| E016 | ERROR    | Orphan PRR (no opening PIR) |
| E017 | WARNING  | Unmatched WIR |
| E018 | WARNING  | Orphan WRR |
| E019 | WARNING  | Unmatched BPS |
| E020 | WARNING  | Orphan EPS |
| E030 | WARNING  | Mandatory MIR string field is empty |
| E031 | WARNING  | PTR/FTR/MPR outside a PIR–PRR block |
| E032 | WARNING  | PRR HARD_BIN = 0 |
| E033 | WARNING  | PCR PART_CNT = 0 |
| E034 | WARNING  | Duplicate PART_ID within the lot |
| E035 | ERROR    | HBR/SBR bin number out of range |
| E036 | INFO     | ATR CMD_LINE is empty |
| E040 | WARNING  | MIR START_T = 0 |
| E041 | ERROR    | MRR FINISH_T < MIR START_T |
| E042 | WARNING  | MRR FINISH_T = 0 |

---

### `repair` — fix a malformed STDF file

```
stdfcompanion repair [OPTIONS] INPUT

Options:
  -o, --output PATH            Output path for the repaired file.  [required]
  --check-after / --no-check-after  Run check on repaired file (default: on)
  -v, --verbose                Print each repair action applied.
```

The original file is **never modified**.

```bash
stdfcompanion repair bad.stdf -o fixed.stdf
stdfcompanion repair bad.stdf -o fixed.stdf --verbose
```

**Example output**

```
2 repair(s) applied:
  [E012] Synthetic MRR appended at end of file
  [E015] Synthetic PRR inserted for unmatched PIR (head=1 site=1)

2 repair(s) applied  errors: 2 → 0
Repaired file written to: fixed.stdf

Post-repair check:
  No errors remain.
  OK  0 error(s)  1 warning(s)  0 info(s)
```

#### Automatic repairs

| Code  | Action |
|-------|--------|
| E003  | Force FAR STDF_VER = 4 |
| E004  | Force FAR CPU_TYPE = 2 (little-endian) |
| E010  | Insert synthetic MIR with placeholder values |
| E012  | Append synthetic MRR |
| E013  | Move MRR to end of file |
| E015  | Insert synthetic closing PRR for open PIR |
| E016  | Insert synthetic opening PIR before orphan PRR |
| E017  | Insert synthetic closing WRR for open WIR |
| E018  | Insert synthetic opening WIR before orphan WRR |
| E040  | Set MIR START_T from MRR FINISH_T |
| E041  | Set MRR FINISH_T = MIR START_T |
| E042  | Set MRR FINISH_T to current time |

A new **ATR** record is always added to the repaired file documenting the repair.

---

## `inspect` — dump record contents

```
stdfcompanion inspect [OPTIONS] INPUT

Options:
  -r, --record TYPE    Only show this record type (repeatable)
  --head N             Filter by HEAD_NUM
  --site N             Filter by SITE_NUM
  --part N             Only show the Nth PIR/PRR block (1-based)
  -n, --limit N        Stop after N matching records
  -o, --output FILE    Write to file instead of stdout
  --summary            Print one-line-per-type count table
  --json               Output as JSON array
  --text               Human-readable text (default)
  -v, --verbose        Print progress information
```

```bash
# Full text dump of every record
stdfcompanion inspect file.stdf

# Quick record-count summary
stdfcompanion inspect file.stdf --summary

# Only PTR records for head=1 site=1
stdfcompanion inspect file.stdf -r PTR --head 1 --site 1

# Inspect the 3rd part (PIR/PRR block) in detail
stdfcompanion inspect file.stdf --part 3

# First 20 records as JSON (pipe to jq, etc.)
stdfcompanion inspect file.stdf --json -n 20

# Write full dump to a text file for sharing
stdfcompanion inspect file.stdf -o dump.txt

# Show only MIR and MRR
stdfcompanion inspect file.stdf -r MIR -r MRR
```

**Example text output**

```
#000001  FAR     [offset=0  len=2]
  CPU_TYPE     : 2
  STDF_VER     : 4

#000002  MIR     [offset=6  len=76]
  SETUP_T      : 08:30:00 05-Oct-2026
  START_T      : 08:30:00 05-Oct-2026
  LOT_ID       : LOT001
  PART_TYP     : TEST_PART
  NODE_NAM     : NODE1
  TSTR_TYP     : TSTR_X
  JOB_NAM      : job.prg
  ...

#000004  PIR     [offset=88  len=2]  HEAD=1 SITE=1
  HEAD_NUM     : 1
  SITE_NUM     : 1

#000005  PTR     [offset=94  len=...]
  TEST_NUM     : 100
  RESULT       : 1.234
  TEST_TXT     : VoltageTest
  ...
```

**Example summary output**

```
Record    Count  Offsets
--------------------------------------------------
FAR           1  offset 0
MIR           1  offset 6
SDR           1  offset 88
PIR          50  offset 96 – 214454
PTR         200  offset 102 – 214460
PRR          50  offset 136 – 214490
PCR           1  offset 214516
MRR           1  offset 214540
--------------------------------------------------
TOTAL       305
```

---

## Merge strategy

| Record type | Behaviour |
|-------------|-----------|
| **FAR** | Taken from file 1; CPU_TYPE forced to `2` (little-endian / Intel) |
| **ATR** | All source ATRs forwarded; a new ATR recording this merge is appended |
| **MIR** | Taken from file 1 |
| **SDR / RDR / WCR** | Taken from file 1 only (tester-description singletons) |
| **WIR / WRR / PIR / PRR / PTR / FTR / MPR / TSR / BPS / EPS / GDR / DTR / PMR / PGR / PLR** | All records from all files, in file order |
| **PCR / HBR / SBR** | Per-file summary records suppressed; a new merged summary is written after all part data |
| **MRR** | Single MRR written last; `FINISH_T` = max across all input files |

---

## Project layout

```
mySTDFcompanion/
├── pyproject.toml
├── README.md
├── stdfcompanion/
│   ├── __init__.py
│   ├── cli.py              # Click entry point (merge / check / repair / inspect)
        │   ├── writer.py           # STDF binary serializer
        │   └── commands/
        │       ├── __init__.py
        │       ├── merge.py        # merge command logic
        │       ├── check.py        # validation engine
        │       ├── repair.py       # repair engine
        │       └── inspect.py      # record dump engine
        └── tests/
            ├── test_merge.py
            ├── test_check_repair.py
            └── test_inspect.py
```

---

## License

MIT
