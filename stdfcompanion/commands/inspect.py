"""
stdfcompanion.commands.inspect
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Human-readable dump of every record in an STDF v4 file.

Output modes
------------
text  (default)
    One block per record, indented field-by-field.  Example::

        #0004  FAR  [offset=0  len=2]
          CPU_TYPE : 2
          STDF_VER : 4

        #0005  MIR  [offset=6  len=76]
          SETUP_T  : 08:30:00 05-Oct-2026
          START_T  : 08:30:00 05-Oct-2026
          LOT_ID   : LOT001
          PART_TYP : TEST_PART
          ...

        #0006  PIR  [offset=82  len=2]  HEAD=1 SITE=1
          HEAD_NUM : 1
          SITE_NUM : 1

summary (--summary)
    One line per record type, showing count and first/last offset::

        FAR  :   1 record
        MIR  :   1 record
        PIR  :  50 records   offset 82 – 214454
        PTR  : 200 records   offset 88 – 214470
        PRR  :  50 records   offset 122 – 214490
        ...

json (--json)
    Full dump as a JSON array, one object per record.

Filtering
---------
--record TYPE    Only show records of this type (e.g. PTR, MIR).
                 Can be given multiple times.
--part N         Only show records belonging to the Nth PIR/PRR block
                 (1-based, across all head/site combinations).
--head N         Filter by HEAD_NUM.
--site N         Filter by SITE_NUM.
--limit N        Stop after N matching records.
"""

from __future__ import annotations

import io
import json
import struct
from dataclasses import dataclass, field
from time import strftime, localtime
from typing import Any, Dict, List, Optional, Set, Tuple

from pystdf import V4
from pystdf.IO import Parser
from pystdf.Pipeline import DataSource

from stdfcompanion.commands.check import _get_field

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_TS_FIELDS = {"SETUP_T", "START_T", "FINISH_T", "MOD_TIM"}


def _fmt_value(field_name: str, stdf_type: str, value: Any) -> str:
    """Format a single field value as a human-readable string."""
    if value is None:
        return "(missing)"
    if field_name in _TS_FIELDS and isinstance(value, (int, float)) and value > 0:
        try:
            return strftime("%H:%M:%S %d-%b-%Y", localtime(value))
        except Exception:
            pass
    if stdf_type in ("B1",):
        return f"0x{value:02X}"
    if stdf_type.startswith("k") or isinstance(value, list):
        return "[" + ", ".join(str(v) for v in value) + "]"
    return str(value)


def _record_label(rec_type) -> str:
    return rec_type.__class__.__name__.upper()


def _head_site(rec_type, fields) -> Optional[str]:
    """Return 'HEAD=N SITE=N' tag if the record has those fields."""
    head = _get_field(fields, rec_type, "HEAD_NUM")
    site = _get_field(fields, rec_type, "SITE_NUM")
    if head is None:
        return None
    parts = [f"HEAD={head}"]
    if site is not None:
        parts.append(f"SITE={site}")
    return "  " + " ".join(parts)


# ---------------------------------------------------------------------------
# collector with offset tracking
# ---------------------------------------------------------------------------

class _OffsetParser(Parser):
    """Parser that records the byte offset of each record header."""
    def __init__(self, inp, endian):
        super().__init__(inp=inp, endian=endian)
        self._offsets: List[int] = []

    def header(self, header):
        try:
            pos = self.inp.tell() - 4
        except Exception:
            pos = -1
        self._offsets.append(pos)


class _Collector(DataSource):
    def __init__(self, parser: _OffsetParser):
        DataSource.__init__(self, [])
        self._parser = parser
        self.records: List[Tuple] = []  # (rec_type, fields, offset)
        self._idx = 0

    def after_send(self, data_source, data):
        rec_type, fields = data
        offsets = self._parser._offsets
        offset = offsets[self._idx] if self._idx < len(offsets) else -1
        self.records.append((rec_type, list(fields), offset))
        self._idx += 1


def _parse_file(path: str) -> List[Tuple]:
    """Return list of (rec_type, fields, offset)."""
    with open(path, "rb") as f:
        raw = f.read(6)
    endian = "<" if (len(raw) >= 5 and raw[4] == 2) else ">"

    with open(path, "rb") as f:
        parser = _OffsetParser(inp=f, endian=endian)
        collector = _Collector(parser)
        parser.addSink(collector)
        try:
            parser.parse()
        except Exception:
            pass
    return collector.records


def _get_rec_len(path: str, offset: int) -> int:
    """Read REC_LEN from the 2-byte header at *offset*."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            raw = f.read(2)
        endian = "<"
        val, = struct.unpack(endian + "H", raw)
        return val
    except Exception:
        return -1


# ---------------------------------------------------------------------------
# filter helpers
# ---------------------------------------------------------------------------

def _matches_filter(
    rec_type, fields, offset: int,
    record_types: Set[str],
    head_filter: Optional[int],
    site_filter: Optional[int],
) -> bool:
    label = _record_label(rec_type)
    if record_types and label not in record_types:
        return False
    if head_filter is not None:
        head = _get_field(fields, rec_type, "HEAD_NUM")
        if head is not None and head != head_filter:
            return False
    if site_filter is not None:
        site = _get_field(fields, rec_type, "SITE_NUM")
        if site is not None and site != site_filter:
            return False
    return True


# ---------------------------------------------------------------------------
# output formatters
# ---------------------------------------------------------------------------

def _format_text(
    path: str,
    records: List[Tuple],
    record_types: Set[str],
    head_filter: Optional[int],
    site_filter: Optional[int],
    part_filter: Optional[int],
    limit: Optional[int],
) -> str:
    out = io.StringIO()
    matched = 0
    part_idx = 0          # current PIR/PRR block counter (global, all head/sites)
    in_part = False

    REC_PIR = (_record_label(V4.Pir), )  # just the string
    REC_PRR = (_record_label(V4.Prr), )

    for seq, (rec_type, fields, offset) in enumerate(records, start=1):
        label = _record_label(rec_type)

        # track part blocks for --part filter
        if label == "PIR":
            part_idx += 1
            in_part = True
        elif label == "PRR":
            in_part = False

        if not _matches_filter(rec_type, fields, offset,
                                record_types, head_filter, site_filter):
            continue

        if part_filter is not None:
            # include only records inside the Nth block
            if label == "PIR" and part_idx != part_filter:
                continue
            if label != "PIR" and part_idx != part_filter:
                continue

        rec_len = _get_rec_len(path, offset)
        hs_tag = _head_site(rec_type, fields) or ""
        out.write(f"#{seq:06d}  {label:<6}  [offset={offset}  len={rec_len}]{hs_tag}\n")

        for i, fname in enumerate(rec_type.fieldNames):
            value = fields[i] if i < len(fields) else None
            stdf_type = rec_type.fieldStdfTypes[i] if i < len(rec_type.fieldStdfTypes) else "?"
            fmtval = _fmt_value(fname, stdf_type, value)
            out.write(f"  {fname:<12} : {fmtval}\n")

        out.write("\n")
        matched += 1
        if limit and matched >= limit:
            out.write(f"... output limited to {limit} records\n")
            break

    return out.getvalue()


def _format_summary(records: List[Tuple]) -> str:
    from collections import defaultdict, Counter
    counts: Counter = Counter()
    first_offset: Dict[str, int] = {}
    last_offset:  Dict[str, int] = {}

    for rec_type, fields, offset in records:
        label = _record_label(rec_type)
        counts[label] += 1
        if label not in first_offset:
            first_offset[label] = offset
        last_offset[label] = offset

    # preserve record order (first occurrence)
    seen = []
    for rec_type, _, _ in records:
        label = _record_label(rec_type)
        if label not in seen:
            seen.append(label)

    out = io.StringIO()
    out.write(f"{'Record':<8}  {'Count':>6}  {'Offsets'}\n")
    out.write("-" * 50 + "\n")
    for label in seen:
        cnt = counts[label]
        if first_offset[label] == last_offset[label]:
            span = f"offset {first_offset[label]}"
        else:
            span = f"offset {first_offset[label]} – {last_offset[label]}"
        out.write(f"{label:<8}  {cnt:>6}  {span}\n")
    out.write("-" * 50 + "\n")
    out.write(f"{'TOTAL':<8}  {sum(counts.values()):>6}\n")
    return out.getvalue()


def _format_json(
    records: List[Tuple],
    record_types: Set[str],
    head_filter: Optional[int],
    site_filter: Optional[int],
    part_filter: Optional[int],
    limit: Optional[int],
) -> str:
    part_idx = 0
    out = []
    for seq, (rec_type, fields, offset) in enumerate(records, start=1):
        label = _record_label(rec_type)
        if label == "PIR":
            part_idx += 1

        if not _matches_filter(rec_type, fields, offset,
                                record_types, head_filter, site_filter):
            continue
        if part_filter is not None:
            if label == "PIR" and part_idx != part_filter:
                continue
            if label != "PIR" and part_idx != part_filter:
                continue

        obj: Dict[str, Any] = {
            "seq":    seq,
            "record": label,
            "offset": offset,
            "fields": {},
        }
        for i, fname in enumerate(rec_type.fieldNames):
            value = fields[i] if i < len(fields) else None
            stdf_type = rec_type.fieldStdfTypes[i] if i < len(rec_type.fieldStdfTypes) else "?"
            if fname in _TS_FIELDS and isinstance(value, (int, float)) and value > 0:
                obj["fields"][fname] = strftime("%H:%M:%S %d-%b-%Y", localtime(value))
            elif isinstance(value, list):
                obj["fields"][fname] = value
            else:
                obj["fields"][fname] = value
        out.append(obj)

        if limit and len(out) >= limit:
            break

    return json.dumps(out, indent=2)


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def inspect_stdf(
    path: str,
    *,
    record_types: Optional[List[str]] = None,
    head_filter: Optional[int] = None,
    site_filter: Optional[int] = None,
    part_filter: Optional[int] = None,
    limit: Optional[int] = None,
    output_format: str = "text",   # "text" | "summary" | "json"
    verbose: bool = False,
) -> str:
    """
    Inspect an STDF file and return a formatted string.

    Parameters
    ----------
    path          : str – STDF file to inspect
    record_types  : list of record type names to include (e.g. ["PTR", "MIR"])
    head_filter   : only include records with HEAD_NUM == head_filter
    site_filter   : only include records with SITE_NUM == site_filter
    part_filter   : only include the Nth PIR/PRR block (1-based)
    limit         : stop after this many matching records
    output_format : "text", "summary", or "json"
    verbose       : print progress to stdout

    Returns
    -------
    str  – formatted inspection output
    """
    path = str(path)
    if verbose:
        print(f"  Inspecting: {path}")

    records = _parse_file(path)

    rt_set: Set[str] = {r.upper() for r in record_types} if record_types else set()

    if output_format == "summary":
        result = _format_summary(records)
    elif output_format == "json":
        result = _format_json(records, rt_set, head_filter, site_filter,
                              part_filter, limit)
    else:
        result = _format_text(path, records, rt_set, head_filter, site_filter,
                              part_filter, limit)

    if verbose:
        total = len(records)
        print(f"  {total} records parsed")

    return result
