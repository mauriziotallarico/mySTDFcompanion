# mySTDFcompanion

A command-line tool for parsing and manipulating **STDF v4** (Standard Test Data Format) files.  
Built on top of [pystdf](https://pypi.org/project/pystdf/).

---

## Features

| Command | Description |
|---------|-------------|
| `merge` | Combine 2 or more STDF files into a single output file |

More commands coming in future releases (split, filter, convert, inspect, …).

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

  Merge two or more STDF files into a single output file.

Options:
  -o, --output PATH   Output STDF file path.  [required]
  -v, --verbose       Print progress information.
  --version           Show version and exit.
  --help              Show this message and exit.
```

**Examples**

```bash
# Merge two lots
stdfcompanion merge lot1.stdf lot2.stdf -o merged.stdf

# Merge three files with verbose output
stdfcompanion merge a.stdf b.stdf c.stdf -o combined.stdf --verbose
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
│   ├── cli.py          # Click entry point
│   ├── writer.py       # STDF binary serializer
│   └── commands/
│       ├── __init__.py
│       └── merge.py    # merge command logic
└── tests/
    └── test_merge.py
```

---

## License

MIT
