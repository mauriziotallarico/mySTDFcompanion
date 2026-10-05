"""
stdfcompanion.commands.check
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

STDF v4 file validator.

Error categories checked
------------------------
STRUCTURAL
  E001  File too small to contain a valid FAR record (< 6 bytes)
  E002  First record is not a FAR (typ=0, sub=10)
  E003  FAR has invalid STDF_VER (must be 4)
  E004  FAR has invalid CPU_TYPE (expected 1 or 2)
  E005  Truncated record: REC_LEN exceeds remaining bytes in file
  E006  Unknown record type/subtype encountered

SEQUENCE
  E010  MIR missing (required exactly once, immediately after FAR/ATRs)
  E011  MIR appears more than once
  E012  MRR missing (must be the last record)
  E013  MRR is not the last record in the file
  E014  PCR missing (at least one required)
  E015  PIR without matching PRR (unmatched open part)
  E016  PRR without preceding PIR (orphan part result)
  E017  WIR without matching WRR
  E018  WRR without preceding WIR
  E019  BPS without matching EPS
  E020  EPS without preceding BPS

CONTENT
  E030  MIR mandatory string field is empty (LOT_ID, PART_TYP, JOB_NAM)
  E031  PTR / FTR / MPR outside a PIR–PRR block
  E032  PRR HARD_BIN value 0 (reserved, usually indicates missing data)
  E033  PCR PART_CNT is 0
  E034  Duplicate PART_ID within the same lot (warning)
  E035  HBR / SBR bin number out of valid range (0–32767)
  E036  ATR CMD_LINE is empty

TIMESTAMPS
  E040  MIR START_T is zero (missing)
  E041  MRR FINISH_T < MIR START_T (time went backwards)
  E042  MRR FINISH_T is zero (missing)

Each issue is reported as an :class:`Issue` with a severity
(ERROR / WARNING / INFO) and the byte offset of the offending record.
"""

from __future__ import annotations

import io
import struct
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from pystdf import V4
from pystdf.IO import Parser
from pystdf.Pipeline import DataSource
from pystdf.Types import RecordHeader

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class Severity(Enum):
    ERROR   = "ERROR"
    WARNING = "WARNING"
    INFO    = "INFO"


@dataclass
class Issue:
    code:     str        # e.g. "E001"
    severity: Severity
    message:  str
    offset:   int = -1   # byte offset in file (-1 = not applicable)
    record:   str = ""   # human-readable record label, e.g. "PIR@offset 512"

    def __str__(self) -> str:
        loc = f" [offset={self.offset}]" if self.offset >= 0 else ""
        rec = f" in {self.record}" if self.record else ""
        return f"[{self.severity.value}] {self.code}: {self.message}{rec}{loc}"


@dataclass
class CheckResult:
    path:   str
    issues: List[Issue] = field(default_factory=list)

    @property
    def errors(self) -> List[Issue]:
        return [i for i in self.issues if i.severity == Severity.ERROR]

    @property
    def warnings(self) -> List[Issue]:
        return [i for i in self.issues if i.severity == Severity.WARNING]

    @property
    def infos(self) -> List[Issue]:
        return [i for i in self.issues if i.severity == Severity.INFO]

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0

    def summary(self) -> str:
        e = len(self.errors)
        w = len(self.warnings)
        i = len(self.infos)
        status = "OK" if self.ok else "FAIL"
        return f"{status}  {e} error(s)  {w} warning(s)  {i} info(s)"


# ---------------------------------------------------------------------------
# Low-level binary scanner
# (runs before pystdf to catch structural/truncation issues)
# ---------------------------------------------------------------------------

_PACK_FMT: dict[str, str] = {
    "C1": "c", "B1": "B",
    "U1": "B", "U2": "H", "U4": "I", "U8": "Q",
    "I1": "b", "I2": "h", "I4": "i", "I8": "q",
    "R4": "f", "R8": "d",
}

_KNOWN_RECORDS: Set[Tuple[int, int]] = {
    (rt.typ, rt.sub) for rt in V4.records
}


def _detect_endian(data: bytes) -> str:
    """Return '<' or '>' based on FAR CPU_TYPE byte (offset 4)."""
    if len(data) < 6:
        return "<"
    cpu_type = data[4]
    return "<" if cpu_type == 2 else ">"


def _scan_binary(data: bytes, issues: List[Issue]) -> List[Tuple[int, int, int, int, int]]:
    """
    Scan raw bytes and return a list of (offset, typ, sub, rec_len, data_start).
    Appends structural Issues to *issues*.
    """
    records_meta: List[Tuple[int, int, int, int, int]] = []
    file_size = len(data)

    if file_size < 6:
        issues.append(Issue("E001", Severity.ERROR,
            f"File is only {file_size} bytes – too small for a valid FAR record"))
        return records_meta

    endian = _detect_endian(data)
    offset = 0

    while offset < file_size:
        if offset + 4 > file_size:
            issues.append(Issue("E005", Severity.ERROR,
                f"Incomplete record header at end of file (only {file_size - offset} bytes remain)",
                offset=offset))
            break

        rec_len, typ, sub = struct.unpack_from(endian + "HBB", data, offset)
        data_start = offset + 4

        if data_start + rec_len > file_size:
            issues.append(Issue("E005", Severity.ERROR,
                f"Record {typ}/{sub} at offset {offset} claims REC_LEN={rec_len} but only "
                f"{file_size - data_start} bytes remain – file is truncated",
                offset=offset,
                record=f"REC_TYP={typ} REC_SUB={sub}"))
            # Still append what we have so downstream checks can run
            records_meta.append((offset, typ, sub, rec_len, data_start))
            break

        if (typ, sub) not in _KNOWN_RECORDS:
            issues.append(Issue("E006", Severity.WARNING,
                f"Unknown record type REC_TYP={typ} REC_SUB={sub} at offset {offset}",
                offset=offset,
                record=f"REC_TYP={typ} REC_SUB={sub}"))

        records_meta.append((offset, typ, sub, rec_len, data_start))
        offset = data_start + rec_len

    return records_meta


def _check_far(data: bytes, records_meta, issues: List[Issue], endian: str) -> None:
    """E002 / E003 / E004 – FAR must be first, version must be 4."""
    if not records_meta:
        return

    offset, typ, sub, rec_len, data_start = records_meta[0]
    if typ != V4.Far.typ or sub != V4.Far.sub:
        issues.append(Issue("E002", Severity.ERROR,
            f"First record is not FAR (got REC_TYP={typ} REC_SUB={sub})",
            offset=offset))
        return

    if rec_len >= 2 and data_start + 2 <= len(data):
        cpu_type = data[data_start]
        stdf_ver = data[data_start + 1]
        if cpu_type not in (1, 2):
            issues.append(Issue("E004", Severity.WARNING,
                f"FAR CPU_TYPE={cpu_type} is non-standard (expected 1=big-endian or 2=little-endian)",
                offset=offset, record="FAR"))
        if stdf_ver != 4:
            issues.append(Issue("E003", Severity.ERROR,
                f"FAR STDF_VER={stdf_ver} (only version 4 is supported)",
                offset=offset, record="FAR"))


# ---------------------------------------------------------------------------
# pystdf-based semantic scanner
# ---------------------------------------------------------------------------

class _OffsetTrackingParser(Parser):
    """Parser subclass that exposes the current file offset."""

    def __init__(self, inp, endian):
        super().__init__(inp=inp, endian=endian)
        self._offsets: List[int] = []

    def header(self, header):
        # Called right after each record header is read; record current pos
        # (pos is after the 4-byte header, so subtract 4 to get record start)
        try:
            pos = self.inp.tell() - 4
        except Exception:
            pos = -1
        self._offsets.append(pos)


class _SemanticCollector(DataSource):
    """Collect (rec_type, fields, offset) triples."""

    def __init__(self, parser: _OffsetTrackingParser):
        DataSource.__init__(self, [])
        self._parser = parser
        self.records: List[Tuple] = []
        self._rec_idx = 0

    def after_send(self, data_source, data):
        rec_type, fields = data
        offsets = self._parser._offsets
        offset = offsets[self._rec_idx] if self._rec_idx < len(offsets) else -1
        self.records.append((rec_type, list(fields), offset))
        self._rec_idx += 1


def _get_field(fields: list, rec_type, field_name: str):
    try:
        idx = rec_type.fieldNames.index(field_name)
        return fields[idx] if idx < len(fields) else None
    except ValueError:
        return None


def _run_semantic_checks(path: str, issues: List[Issue]) -> List[Tuple]:
    """
    Use pystdf to parse *path* and apply semantic/sequence/content rules.
    Returns the list of (rec_type, fields, offset) for use by repair.
    """
    with open(path, "rb") as f:
        raw = f.read(6)
        endian = "<" if (len(raw) >= 5 and raw[4] == 2) else ">"

    records: List[Tuple] = []
    try:
        with open(path, "rb") as f:
            parser = _OffsetTrackingParser(inp=f, endian=endian)
            collector = _SemanticCollector(parser)
            parser.addSink(collector)
            parser.parse()
        records = collector.records
    except Exception as exc:
        issues.append(Issue("E005", Severity.ERROR,
            f"pystdf parse error: {exc}", offset=0))
        return records

    # ---- helpers ---------------------------------------------------------
    def rname(rt) -> str:
        return rt.__class__.__name__

    def key(rt) -> Tuple[int, int]:
        return (rt.typ, rt.sub)

    REC_FAR  = (V4.Far.typ,  V4.Far.sub)
    REC_MIR  = (V4.Mir.typ,  V4.Mir.sub)
    REC_MRR  = (V4.Mrr.typ,  V4.Mrr.sub)
    REC_PCR  = (V4.Pcr.typ,  V4.Pcr.sub)
    REC_PIR  = (V4.Pir.typ,  V4.Pir.sub)
    REC_PRR  = (V4.Prr.typ,  V4.Prr.sub)
    REC_WIR  = (V4.Wir.typ,  V4.Wir.sub)
    REC_WRR  = (V4.Wrr.typ,  V4.Wrr.sub)
    REC_BPS  = (V4.Bps.typ,  V4.Bps.sub)
    REC_EPS  = (V4.Eps.typ,  V4.Eps.sub)
    REC_ATR  = (V4.Atr.typ,  V4.Atr.sub)
    REC_PTR  = (V4.Ptr.typ,  V4.Ptr.sub)
    REC_FTR  = (V4.Ftr.typ,  V4.Ftr.sub)
    REC_MPR  = (V4.Mpr.typ,  V4.Mpr.sub)
    REC_HBR  = (V4.Hbr.typ,  V4.Hbr.sub)
    REC_SBR  = (V4.Sbr.typ,  V4.Sbr.sub)

    # ---- E010/E011: MIR presence -----------------------------------------
    mirs = [(rt, flds, off) for rt, flds, off in records if key(rt) == REC_MIR]
    if not mirs:
        issues.append(Issue("E010", Severity.ERROR,
            "MIR (Master Information Record) is missing"))
    elif len(mirs) > 1:
        for _, _, off in mirs[1:]:
            issues.append(Issue("E011", Severity.ERROR,
                "Duplicate MIR record", offset=off, record="MIR"))

    # ---- E012/E013: MRR --------------------------------------------------
    mrrs = [(rt, flds, off) for rt, flds, off in records if key(rt) == REC_MRR]
    if not mrrs:
        issues.append(Issue("E012", Severity.ERROR,
            "MRR (Master Results Record) is missing"))
    else:
        last_rt, last_flds, last_off = records[-1]
        if key(last_rt) != REC_MRR:
            issues.append(Issue("E013", Severity.ERROR,
                "MRR is not the last record in the file",
                offset=mrrs[0][2], record="MRR"))

    # ---- E014: PCR presence ----------------------------------------------
    pcrs = [r for r in records if key(r[0]) == REC_PCR]
    if not pcrs:
        issues.append(Issue("E014", Severity.WARNING,
            "No PCR (Part Count Record) found"))

    # ---- E015/E016: PIR–PRR matching ------------------------------------
    # Stack keyed by (HEAD_NUM, SITE_NUM)
    open_parts: Dict[Tuple, int] = {}  # -> offset of PIR
    for rt, flds, off in records:
        k = key(rt)
        if k == REC_PIR:
            head = _get_field(flds, rt, "HEAD_NUM") or 1
            site = _get_field(flds, rt, "SITE_NUM") or 1
            hs = (head, site)
            if hs in open_parts:
                issues.append(Issue("E015", Severity.ERROR,
                    f"PIR for head={head} site={site} opened again before PRR closed the previous one",
                    offset=off, record="PIR"))
            open_parts[hs] = off
        elif k == REC_PRR:
            head = _get_field(flds, rt, "HEAD_NUM") or 1
            site = _get_field(flds, rt, "SITE_NUM") or 1
            hs = (head, site)
            if hs not in open_parts:
                issues.append(Issue("E016", Severity.ERROR,
                    f"PRR for head={head} site={site} has no matching PIR",
                    offset=off, record="PRR"))
            else:
                del open_parts[hs]
        elif k in (REC_PTR, REC_FTR, REC_MPR):
            head = _get_field(flds, rt, "HEAD_NUM") or 1
            site = _get_field(flds, rt, "SITE_NUM") or 1
            if (head, site) not in open_parts:
                issues.append(Issue("E031", Severity.WARNING,
                    f"{rname(rt)} for head={head} site={site} is outside a PIR–PRR block",
                    offset=off, record=rname(rt)))

    for (head, site), pir_off in open_parts.items():
        issues.append(Issue("E015", Severity.ERROR,
            f"PIR for head={head} site={site} at offset {pir_off} has no matching PRR",
            offset=pir_off, record="PIR"))

    # ---- E017/E018: WIR–WRR matching ------------------------------------
    open_wafers: Dict[Tuple, int] = {}
    for rt, flds, off in records:
        k = key(rt)
        if k == REC_WIR:
            head = _get_field(flds, rt, "HEAD_NUM") or 1
            site = _get_field(flds, rt, "SITE_GRP") or 255
            hs = (head, site)
            if hs in open_wafers:
                issues.append(Issue("E017", Severity.WARNING,
                    f"WIR for head={head} site_grp={site} opened again before WRR",
                    offset=off, record="WIR"))
            open_wafers[hs] = off
        elif k == REC_WRR:
            head = _get_field(flds, rt, "HEAD_NUM") or 1
            site = _get_field(flds, rt, "SITE_GRP") or 255
            hs = (head, site)
            if hs not in open_wafers:
                issues.append(Issue("E018", Severity.WARNING,
                    f"WRR for head={head} site_grp={site} has no matching WIR",
                    offset=off, record="WRR"))
            else:
                del open_wafers[hs]

    for (head, site), wir_off in open_wafers.items():
        issues.append(Issue("E017", Severity.WARNING,
            f"WIR for head={head} site_grp={site} at offset {wir_off} has no matching WRR",
            offset=wir_off, record="WIR"))

    # ---- E019/E020: BPS–EPS matching ------------------------------------
    open_bps: List[int] = []
    for rt, flds, off in records:
        k = key(rt)
        if k == REC_BPS:
            open_bps.append(off)
        elif k == REC_EPS:
            if not open_bps:
                issues.append(Issue("E020", Severity.WARNING,
                    "EPS has no matching BPS",
                    offset=off, record="EPS"))
            else:
                open_bps.pop()

    for bps_off in open_bps:
        issues.append(Issue("E019", Severity.WARNING,
            f"BPS at offset {bps_off} has no matching EPS",
            offset=bps_off, record="BPS"))

    # ---- Content checks --------------------------------------------------

    # E030: mandatory MIR string fields
    if mirs:
        mir_rt, mir_flds, mir_off = mirs[0]
        for fname in ("LOT_ID", "PART_TYP", "JOB_NAM"):
            val = _get_field(mir_flds, mir_rt, fname)
            if not val:
                issues.append(Issue("E030", Severity.WARNING,
                    f"MIR field {fname} is empty or missing",
                    offset=mir_off, record="MIR"))

    # E032: PRR HARD_BIN == 0
    for rt, flds, off in records:
        if key(rt) == REC_PRR:
            hbin = _get_field(flds, rt, "HARD_BIN")
            if hbin == 0:
                issues.append(Issue("E032", Severity.WARNING,
                    "PRR HARD_BIN=0 (bin 0 is typically reserved; may indicate missing bin assignment)",
                    offset=off, record="PRR"))

    # E033: PCR PART_CNT == 0
    for rt, flds, off in records:
        if key(rt) == REC_PCR:
            cnt = _get_field(flds, rt, "PART_CNT")
            if cnt == 0:
                issues.append(Issue("E033", Severity.WARNING,
                    "PCR PART_CNT=0",
                    offset=off, record="PCR"))

    # E034: duplicate PART_ID
    seen_part_ids: Dict[str, int] = {}
    for rt, flds, off in records:
        if key(rt) == REC_PRR:
            pid = _get_field(flds, rt, "PART_ID")
            if pid:
                if pid in seen_part_ids:
                    issues.append(Issue("E034", Severity.WARNING,
                        f"Duplicate PART_ID '{pid}' (first seen at offset {seen_part_ids[pid]})",
                        offset=off, record="PRR"))
                else:
                    seen_part_ids[pid] = off

    # E035: HBR / SBR bin number out of range
    for rt, flds, off in records:
        if key(rt) == REC_HBR:
            bn = _get_field(flds, rt, "HBIN_NUM")
            if bn is not None and not (0 <= bn <= 32767):
                issues.append(Issue("E035", Severity.ERROR,
                    f"HBR HBIN_NUM={bn} out of valid range [0–32767]",
                    offset=off, record="HBR"))
        elif key(rt) == REC_SBR:
            bn = _get_field(flds, rt, "SBIN_NUM")
            if bn is not None and not (0 <= bn <= 32767):
                issues.append(Issue("E035", Severity.ERROR,
                    f"SBR SBIN_NUM={bn} out of valid range [0–32767]",
                    offset=off, record="SBR"))

    # E036: empty ATR CMD_LINE
    for rt, flds, off in records:
        if key(rt) == REC_ATR:
            cmd = _get_field(flds, rt, "CMD_LINE")
            if not cmd:
                issues.append(Issue("E036", Severity.INFO,
                    "ATR CMD_LINE is empty",
                    offset=off, record="ATR"))

    # E040/E041/E042: timestamps
    if mirs and mrrs:
        mir_rt, mir_flds, mir_off = mirs[0]
        mrr_rt, mrr_flds, mrr_off = mrrs[0]
        start_t  = _get_field(mir_flds, mir_rt, "START_T")  or 0
        finish_t = _get_field(mrr_flds, mrr_rt, "FINISH_T") or 0
        if start_t == 0:
            issues.append(Issue("E040", Severity.WARNING,
                "MIR START_T is zero (timestamp missing)",
                offset=mir_off, record="MIR"))
        if finish_t == 0:
            issues.append(Issue("E042", Severity.WARNING,
                "MRR FINISH_T is zero (timestamp missing)",
                offset=mrr_off, record="MRR"))
        if start_t > 0 and finish_t > 0 and finish_t < start_t:
            issues.append(Issue("E041", Severity.ERROR,
                f"MRR FINISH_T ({finish_t}) < MIR START_T ({start_t}) – time went backwards",
                offset=mrr_off, record="MRR"))

    return records


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_stdf(path: str, verbose: bool = False) -> CheckResult:
    """
    Validate an STDF file and return a :class:`CheckResult`.

    Parameters
    ----------
    path    : str / Path – file to check
    verbose : bool       – print progress to stdout

    Returns
    -------
    CheckResult
        Contains a list of :class:`Issue` objects.  ``result.ok`` is True
        when there are no ERROR-level issues.
    """
    path = str(path)
    result = CheckResult(path=path)

    if verbose:
        print(f"  Checking: {path}")

    # ------------------------------------------------------------------
    # Phase 1 – raw binary scan
    # ------------------------------------------------------------------
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        result.issues.append(Issue("E000", Severity.ERROR,
            f"Cannot read file: {exc}"))
        return result

    endian = _detect_endian(data)
    records_meta = _scan_binary(data, result.issues)
    _check_far(data, records_meta, result.issues, endian)

    # ------------------------------------------------------------------
    # Phase 2 – semantic checks via pystdf
    # ------------------------------------------------------------------
    _run_semantic_checks(path, result.issues)

    # Sort issues by offset for readability
    result.issues.sort(key=lambda i: (i.offset if i.offset >= 0 else 10**18, i.code))

    if verbose:
        print(f"  {result.summary()}")

    return result
