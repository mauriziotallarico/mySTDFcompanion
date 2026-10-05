"""
stdfcompanion.commands.merge
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Merge two or more STDF v4 files into a single output file.

Merge strategy
--------------
1.  **FAR** – taken verbatim from the first input file (CPU_TYPE/STDF_VER).
2.  **ATR** (optional) – one ATR from each source file is forwarded, then a
    new ATR is appended that records this merge operation.
3.  **MIR** – taken from the first input file.  The START_T is kept as-is;
    the merged file inherits lot metadata from file 1.
4.  **SDR / RDR / WCR** – forwarded from each file in order (de-duplicated
    by key where sensible).
5.  **Body records** (WIR/WRR, PIR/PRR, PTR, FTR, MPR, PMR, PGR, PLR,
    DTR, BPS, EPS, GDR) – all records from every input file are forwarded
    in file order.
6.  **PCR / HBR / SBR summary** – per-file summary records (HEAD_NUM=255)
    are stripped from the body stream.  After all parts are written a new
    set of merged summary records is computed and written.
    Per-file: if a HEAD_NUM=255 summary PCR exists it is used exclusively
    (ignoring per-site PCRs for that file) to avoid double-counting.
7.  **TSR** – stripped from the body stream and re-emitted after summaries.
    De-duplicated by TEST_NUM: when the same TEST_NUM appears in multiple
    files its EXEC_CNT and FAIL_CNT are summed; other fields (limits, name)
    are taken from the first file.
8.  **MRR** – a single MRR is written last.  FINISH_T = max(FINISH_T) across
    all input files; DISP_COD kept from file 1.

The output file always uses little-endian byte order (CPU_TYPE=2).
"""

from __future__ import annotations

import struct
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from pystdf import V4
from pystdf.IO import Parser
from pystdf.Pipeline import DataSource

from stdfcompanion.writer import StdfWriter, pack_record

# ---------------------------------------------------------------------------
# Helper: collect all records from a single STDF file
# ---------------------------------------------------------------------------

class _RecordCollector(DataSource):
    """pystdf sink that stores every (rec_type, fields) pair in a list."""

    def __init__(self):
        DataSource.__init__(self, [])
        self.records: List[Tuple] = []

    def after_send(self, data_source, data):
        rec_type, fields = data
        self.records.append((rec_type, list(fields)))


def _read_stdf(path: str) -> List[Tuple]:
    """Parse *path* and return all records as a list of (rec_type, fields)."""
    collector = _RecordCollector()
    with open(path, "rb") as f:
        parser = Parser(inp=f)
        parser.addSink(collector)
        parser.parse()
    return collector.records


# ---------------------------------------------------------------------------
# Merge helpers
# ---------------------------------------------------------------------------

def _get_field(fields: list, rec_type, field_name: str):
    """Return the value of a named field, or None if not present."""
    try:
        idx = rec_type.fieldNames.index(field_name)
        return fields[idx] if idx < len(fields) else None
    except ValueError:
        return None


def _set_field(fields: list, rec_type, field_name: str, value) -> list:
    """Return a copy of *fields* with the named field replaced."""
    fields = list(fields)
    try:
        idx = rec_type.fieldNames.index(field_name)
        while len(fields) <= idx:
            fields.append(None)
        fields[idx] = value
    except ValueError:
        pass
    return fields


# ---------------------------------------------------------------------------
# Public merge function
# ---------------------------------------------------------------------------

def merge_stdf(
    input_paths: Sequence[str],
    output_path: str,
    *,
    verbose: bool = False,
) -> None:
    """
    Merge STDF files listed in *input_paths* into *output_path*.

    Parameters
    ----------
    input_paths : sequence of str/Path
        At least two STDF input files, processed in order.
    output_path : str/Path
        Destination STDF file.  Will be overwritten if it exists.
    verbose : bool
        Print progress messages to stdout.
    """
    if len(input_paths) < 2:
        raise ValueError("merge requires at least 2 input files")

    input_paths = [str(p) for p in input_paths]
    output_path = str(output_path)

    # ------------------------------------------------------------------
    # Step 1 – parse all input files
    # ------------------------------------------------------------------
    all_file_records: List[List[Tuple]] = []
    for path in input_paths:
        if verbose:
            print(f"  Reading: {path}")
        records = _read_stdf(path)
        all_file_records.append(records)
        if verbose:
            print(f"    {len(records)} records found")

    # ------------------------------------------------------------------
    # Step 2 – extract header records from each file
    # ------------------------------------------------------------------
    # We index records by (typ, sub) for quick lookup
    REC_FAR  = (V4.Far.typ,  V4.Far.sub)
    REC_ATR  = (V4.Atr.typ,  V4.Atr.sub)
    REC_MIR  = (V4.Mir.typ,  V4.Mir.sub)
    REC_MRR  = (V4.Mrr.typ,  V4.Mrr.sub)
    REC_PCR  = (V4.Pcr.typ,  V4.Pcr.sub)
    REC_HBR  = (V4.Hbr.typ,  V4.Hbr.sub)
    REC_SBR  = (V4.Sbr.typ,  V4.Sbr.sub)
    REC_SDR  = (V4.Sdr.typ,  V4.Sdr.sub)
    REC_RDR  = (V4.Rdr.typ,  V4.Rdr.sub)
    REC_WCR  = (V4.Wcr.typ,  V4.Wcr.sub)
    REC_PIR  = (V4.Pir.typ,  V4.Pir.sub)
    REC_PRR  = (V4.Prr.typ,  V4.Prr.sub)
    REC_WIR  = (V4.Wir.typ,  V4.Wir.sub)
    REC_WRR  = (V4.Wrr.typ,  V4.Wrr.sub)

    # Header records to skip when streaming body (will be written explicitly)
    HEADER_RECS = {REC_FAR, REC_ATR, REC_MIR}
    # Summary records to suppress from body stream (will be recomputed)
    REC_TSR  = (V4.Tsr.typ,  V4.Tsr.sub)
    SUMMARY_RECS = {REC_MRR, REC_PCR, REC_HBR, REC_SBR, REC_TSR}
    # Records to suppress altogether because they are per-lot singletons
    # that make no sense when merged (keep only from file 1)
    SINGLETON_RECS: set = set()

    def key(rec_type) -> Tuple[int, int]:
        return (rec_type.typ, rec_type.sub)

    def find_first(records, rec_key):
        for rt, flds in records:
            if key(rt) == rec_key:
                return rt, flds
        return None, None

    # FAR from file 1, force CPU_TYPE=2 (little-endian, Intel)
    far_rt, far_flds = find_first(all_file_records[0], REC_FAR)
    if far_rt is None:
        raise ValueError(f"No FAR record found in {input_paths[0]}")
    far_flds = _set_field(far_flds, far_rt, "CPU_TYPE", 2)
    far_flds = _set_field(far_flds, far_rt, "STDF_VER", 4)

    # MIR from file 1
    mir_rt, mir_flds = find_first(all_file_records[0], REC_MIR)
    if mir_rt is None:
        raise ValueError(f"No MIR record found in {input_paths[0]}")

    # MRR from each file – we need FINISH_T and DISP_COD
    mrr_data = []
    for i, records in enumerate(all_file_records):
        rt, flds = find_first(records, REC_MRR)
        mrr_data.append((rt, flds))

    # Compute merged FINISH_T = max across all files
    finish_times = []
    for rt, flds in mrr_data:
        if rt is not None:
            v = _get_field(flds, rt, "FINISH_T")
            if v:
                finish_times.append(v)
    merged_finish_t = max(finish_times) if finish_times else int(time.time())

    # DISP_COD from file 1 (or space)
    mrr1_rt, mrr1_flds = mrr_data[0]
    disp_cod = " "
    if mrr1_rt is not None:
        disp_cod = _get_field(mrr1_flds, mrr1_rt, "DISP_COD") or " "

    # ------------------------------------------------------------------
    # Step 3 – collect ATR records from all files
    # ------------------------------------------------------------------
    source_atrs: List[Tuple] = []
    for records in all_file_records:
        for rt, flds in records:
            if key(rt) == REC_ATR:
                source_atrs.append((rt, flds))

    # Build a new ATR for this merge operation
    merge_atr_flds = [
        int(time.time()),                          # MOD_TIM
        f"stdfcompanion merge {' '.join(input_paths)}"  # CMD_LINE
    ]

    # ------------------------------------------------------------------
    # Step 4 – collect per-file PCR / HBR / SBR for summary recomputation
    # ------------------------------------------------------------------
    # PCR strategy: for each source file, if a HEAD_NUM=255 summary PCR
    # exists use ONLY that (many testers write both summary + per-site PCRs
    # which would cause double-counting if we added them all).  Fall back
    # to per-site PCRs only when no summary exists in that file.
    pcr_acc: Dict[Tuple, Dict[str, int]] = defaultdict(lambda: dict(
        PART_CNT=0, RTST_CNT=0, ABRT_CNT=0, GOOD_CNT=0, FUNC_CNT=0
    ))
    hbr_map: Dict[int, dict] = {}
    sbr_map: Dict[int, dict] = {}

    MISSING_U4 = 4_294_967_295

    for records in all_file_records:
        # Determine whether this file has a summary (HEAD=255) PCR
        has_summary_pcr = any(
            key(rt) == REC_PCR and (_get_field(flds, rt, "HEAD_NUM") or 0) == 255
            for rt, flds in records
        )

        for rt, flds in records:
            k = key(rt)
            if k == REC_PCR:
                head = _get_field(flds, rt, "HEAD_NUM") or 0
                if has_summary_pcr:
                    # Only use the HEAD=255 summary record; skip per-site
                    if head != 255:
                        continue
                    acc = pcr_acc[(255, 0)]
                else:
                    # No summary – accumulate per-site entries
                    site = _get_field(flds, rt, "SITE_NUM") or 0
                    acc = pcr_acc[(head, site)]
                for fname in ("PART_CNT", "RTST_CNT", "ABRT_CNT", "GOOD_CNT", "FUNC_CNT"):
                    v = _get_field(flds, rt, fname)
                    if v is not None and v != MISSING_U4:
                        acc[fname] = acc.get(fname, 0) + v

            elif k == REC_HBR:
                head = _get_field(flds, rt, "HEAD_NUM") or 0
                if head == 255:
                    hbin = _get_field(flds, rt, "HBIN_NUM") or 0
                    cnt  = _get_field(flds, rt, "HBIN_CNT") or 0
                    pf   = _get_field(flds, rt, "HBIN_PF")  or " "
                    nam  = _get_field(flds, rt, "HBIN_NAM") or ""
                    if hbin not in hbr_map:
                        hbr_map[hbin] = {"cnt": 0, "pf": pf, "nam": nam}
                    hbr_map[hbin]["cnt"] += cnt

            elif k == REC_SBR:
                head = _get_field(flds, rt, "HEAD_NUM") or 0
                if head == 255:
                    sbin = _get_field(flds, rt, "SBIN_NUM") or 0
                    cnt  = _get_field(flds, rt, "SBIN_CNT") or 0
                    pf   = _get_field(flds, rt, "SBIN_PF")  or " "
                    nam  = _get_field(flds, rt, "SBIN_NAM") or ""
                    if sbin not in sbr_map:
                        sbr_map[sbin] = {"cnt": 0, "pf": pf, "nam": nam}
                    sbr_map[sbin]["cnt"] += cnt

    # If no PCR at all was found, build counts from PRR records
    if not pcr_acc:
        for records in all_file_records:
            for rt, flds in records:
                if key(rt) == REC_PRR:
                    head = _get_field(flds, rt, "HEAD_NUM") or 1
                    site = _get_field(flds, rt, "SITE_NUM") or 1
                    acc = pcr_acc[(head, site)]
                    acc["PART_CNT"] = acc.get("PART_CNT", 0) + 1
                    part_flg = _get_field(flds, rt, "PART_FLG") or 0
                    if not (part_flg & 0x08):
                        acc["GOOD_CNT"] = acc.get("GOOD_CNT", 0) + 1

    # ------------------------------------------------------------------
    # Step 4b – collect TSR records and merge by TEST_NUM
    # ------------------------------------------------------------------
    # Key: (HEAD_NUM, SITE_NUM, TEST_NUM).  Counts (EXEC_CNT, FAIL_CNT,
    # ALRM_CNT) are summed; all other fields taken from the first file.
    # TEST_TYP and TEST_NAM are kept from the first occurrence.
    tsr_map: Dict[Tuple, Tuple] = {}   # key → (rt, flds)

    for records in all_file_records:
        for rt, flds in records:
            if key(rt) != REC_TSR:
                continue
            head     = _get_field(flds, rt, "HEAD_NUM") or 1
            site     = _get_field(flds, rt, "SITE_NUM") or 1
            test_num = _get_field(flds, rt, "TEST_NUM")
            if test_num is None:
                continue
            tkey = (head, site, test_num)
            if tkey not in tsr_map:
                tsr_map[tkey] = (rt, list(flds))
            else:
                # Merge statistical and count fields into the stored entry
                _, stored = tsr_map[tkey]

                def _update(field_name: str, new_val, op: str) -> None:
                    """Apply op ('add'|'min'|'max') to stored field."""
                    if new_val is None or new_val == MISSING_U4:
                        return
                    try:
                        idx = rt.fieldNames.index(field_name)
                    except ValueError:
                        return
                    while len(stored) <= idx:
                        stored.append(None)
                    old_val = stored[idx]
                    if old_val is None or old_val == MISSING_U4:
                        stored[idx] = new_val
                    elif op == "add":
                        stored[idx] = old_val + new_val
                    elif op == "min":
                        stored[idx] = min(old_val, new_val)
                    elif op == "max":
                        stored[idx] = max(old_val, new_val)

                # Counts – add
                for fname in ("EXEC_CNT", "FAIL_CNT", "ALRM_CNT"):
                    _update(fname, _get_field(flds, rt, fname), "add")
                # Statistical accumulators – add
                for fname in ("TEST_TIM", "TST_SUMS", "TST_SQRS"):
                    _update(fname, _get_field(flds, rt, fname), "add")
                # Extremes – min / max
                _update("TEST_MIN", _get_field(flds, rt, "TEST_MIN"), "min")
                _update("TEST_MAX", _get_field(flds, rt, "TEST_MAX"), "max")

    # ------------------------------------------------------------------
    # Step 5 – write output file
    # ------------------------------------------------------------------
    if verbose:
        print(f"  Writing: {output_path}")

    with StdfWriter(output_path) as writer:

        # FAR
        writer.write_record(far_rt, far_flds)

        # ATR records (source files first, then merge ATR)
        for rt, flds in source_atrs:
            writer.write_record(rt, flds)
        writer.write_record(V4.Atr, merge_atr_flds)

        # MIR
        writer.write_record(mir_rt, mir_flds)

        # Per-file body records
        # Keep track of SDR/RDR/WCR so we don't write them more than once
        # from file 1 (they describe the tester, not parts)
        wrote_sdr = False
        wrote_rdr = False
        wrote_wcr = False

        for file_idx, records in enumerate(all_file_records):
            first_file = file_idx == 0

            for rt, flds in records:
                k = key(rt)

                # Skip header records already written
                if k in HEADER_RECS:
                    continue

                # Skip summary records – will recompute
                if k in SUMMARY_RECS:
                    continue

                # SDR/RDR/WCR – only from file 1 to avoid duplication
                if k == REC_SDR:
                    if first_file:
                        writer.write_record(rt, flds)
                        wrote_sdr = True
                    continue
                if k == REC_RDR:
                    if first_file:
                        writer.write_record(rt, flds)
                        wrote_rdr = True
                    continue
                if k == REC_WCR:
                    if first_file:
                        writer.write_record(rt, flds)
                        wrote_wcr = True
                    continue

                # All other body records – write verbatim
                writer.write_record(rt, flds)

        # ------------------------------------------------------------------
        # Step 6 – write merged summary records
        # ------------------------------------------------------------------

        # PCR summary (HEAD_NUM=255) – one record with totals
        if pcr_acc:
            # Compute overall totals from all per-site entries
            total = dict(PART_CNT=0, RTST_CNT=0, ABRT_CNT=0, GOOD_CNT=0, FUNC_CNT=0)
            for acc in pcr_acc.values():
                for k2, v in acc.items():
                    total[k2] = total.get(k2, 0) + v
            pcr_flds = [
                255,                     # HEAD_NUM (summary)
                0,                       # SITE_NUM
                total["PART_CNT"],
                total["RTST_CNT"] or MISSING_U4,
                total["ABRT_CNT"] or MISSING_U4,
                total["GOOD_CNT"] or MISSING_U4,
                total["FUNC_CNT"] or MISSING_U4,
            ]
            writer.write_record(V4.Pcr, pcr_flds)

        # HBR summary
        for hbin_num in sorted(hbr_map):
            info = hbr_map[hbin_num]
            hbr_flds = [
                255,         # HEAD_NUM (summary)
                0,           # SITE_NUM
                hbin_num,
                info["cnt"],
                info["pf"],
                info["nam"],
            ]
            writer.write_record(V4.Hbr, hbr_flds)

        # SBR summary
        for sbin_num in sorted(sbr_map):
            info = sbr_map[sbin_num]
            sbr_flds = [
                255,         # HEAD_NUM (summary)
                0,           # SITE_NUM
                sbin_num,
                info["cnt"],
                info["pf"],
                info["nam"],
            ]
            writer.write_record(V4.Sbr, sbr_flds)

        # TSR – merged de-duplicated records sorted by (head, site, test_num)
        for tkey in sorted(tsr_map.keys()):
            tsr_rt, tsr_flds = tsr_map[tkey]
            writer.write_record(tsr_rt, tsr_flds)

        # ------------------------------------------------------------------
        # Step 7 – MRR (must be last)
        # ------------------------------------------------------------------
        mrr_flds = [
            merged_finish_t,   # FINISH_T
            disp_cod,          # DISP_COD
            None,              # USR_DESC
            None,              # EXC_DESC
        ]
        writer.write_record(V4.Mrr, mrr_flds)

    if verbose:
        print("  Merge complete.")
