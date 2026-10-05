"""
stdfcompanion.commands.repair
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

STDF v4 file repair engine.

Repair actions applied (keyed to check error codes)
----------------------------------------------------
E003  FAR STDF_VER != 4        → force STDF_VER=4
E004  FAR CPU_TYPE invalid     → force CPU_TYPE=2 (little-endian)
E005  Truncated file           → drop incomplete record at end
E010  MIR missing              → insert synthetic MIR with placeholder values
E012  MRR missing              → append MRR at end of file
E013  MRR not last             → move MRR to end
E015  Unmatched PIR            → insert synthetic PRR (fail, bin 2) after last
                                  test record for that head/site
E016  Orphan PRR               → insert synthetic PIR before it
E017  Unmatched WIR            → insert synthetic WRR after all records for
                                  that head/site_grp
E018  Orphan WRR               → insert synthetic WIR before it
E034  Duplicate PART_ID        → rename duplicates with _retest suffix
                                  (e.g. '1' → '1', '1_retest', '1_retest2')
E040  MIR START_T=0            → set to MRR FINISH_T (or now)
E041  FINISH_T < START_T       → set FINISH_T = START_T
E042  MRR FINISH_T=0           → set to now

Repairs that are NOT attempted (data cannot be invented reliably):
  - E030 empty mandatory strings (no safe default)
  - E031 test record outside PIR/PRR (structural ambiguity)
  - E035 bin number out of range (cannot correct without knowing intent)

Each repair action is logged in a :class:`RepairLog` and a new ATR record
is added to the output file to document the repair.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from pystdf import V4

from stdfcompanion.writer import StdfWriter
from stdfcompanion.commands.check import (
    CheckResult, Issue, Severity,
    check_stdf, _run_semantic_checks, _get_field,
)

# ---------------------------------------------------------------------------
# Repair log
# ---------------------------------------------------------------------------

@dataclass
class RepairAction:
    code:    str    # E-code that triggered this action
    message: str
    offset:  int = -1

    def __str__(self) -> str:
        loc = f" [offset={self.offset}]" if self.offset >= 0 else ""
        return f"  [{self.code}] {self.message}{loc}"


@dataclass
class RepairResult:
    path_in:  str
    path_out: str
    actions:  List[RepairAction] = field(default_factory=list)
    errors_before: int = 0
    errors_after:  int = 0

    @property
    def repaired(self) -> bool:
        return len(self.actions) > 0

    def summary(self) -> str:
        return (
            f"{len(self.actions)} repair(s) applied  "
            f"errors: {self.errors_before} → {self.errors_after}"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_field(fields: list, rec_type, field_name: str, value) -> list:
    fields = list(fields)
    try:
        idx = rec_type.fieldNames.index(field_name)
        while len(fields) <= idx:
            fields.append(None)
        fields[idx] = value
    except ValueError:
        pass
    return fields


def _key(rt) -> Tuple[int, int]:
    return (rt.typ, rt.sub)


def _now() -> int:
    return int(time.time())


# ---------------------------------------------------------------------------
# Repair engine
# ---------------------------------------------------------------------------

def repair_stdf(
    input_path: str,
    output_path: str,
    *,
    verbose: bool = False,
) -> RepairResult:
    """
    Attempt to repair a malformed STDF file.

    Reads *input_path*, applies all applicable repairs, and writes the
    corrected file to *output_path*.  The original file is never modified.

    Parameters
    ----------
    input_path  : str / Path – source STDF file
    output_path : str / Path – destination for the repaired file
    verbose     : bool       – print progress to stdout

    Returns
    -------
    RepairResult
    """
    input_path  = str(input_path)
    output_path = str(output_path)
    result = RepairResult(path_in=input_path, path_out=output_path)

    # ---------------------------------------------------------------
    # Step 1 – check the file first
    # ---------------------------------------------------------------
    if verbose:
        print(f"  Analysing: {input_path}")

    check = check_stdf(input_path, verbose=False)
    result.errors_before = len(check.errors)
    error_codes: Set[str] = {i.code for i in check.issues}

    if verbose:
        print(f"  Issues found: {check.summary()}")

    # ---------------------------------------------------------------
    # Step 2 – parse all records (best-effort, tolerating truncation)
    # ---------------------------------------------------------------
    records: List[Tuple] = []  # (rec_type, fields, offset)
    try:
        records = _run_semantic_checks(input_path, [])
    except Exception:
        pass  # use whatever was collected

    # ---------------------------------------------------------------
    # Step 3 – apply record-level repairs (field fixups)
    # ---------------------------------------------------------------

    REC_FAR = (V4.Far.typ,  V4.Far.sub)
    REC_ATR = (V4.Atr.typ,  V4.Atr.sub)
    REC_MIR = (V4.Mir.typ,  V4.Mir.sub)
    REC_MRR = (V4.Mrr.typ,  V4.Mrr.sub)
    REC_PCR = (V4.Pcr.typ,  V4.Pcr.sub)
    REC_HBR = (V4.Hbr.typ,  V4.Hbr.sub)
    REC_SBR = (V4.Sbr.typ,  V4.Sbr.sub)
    REC_PIR = (V4.Pir.typ,  V4.Pir.sub)
    REC_PRR = (V4.Prr.typ,  V4.Prr.sub)
    REC_WIR = (V4.Wir.typ,  V4.Wir.sub)
    REC_WRR = (V4.Wrr.typ,  V4.Wrr.sub)

    repaired_records: List[Tuple] = []  # (rec_type, fields) – no offset

    for rec_type, fields, offset in records:
        k = _key(rec_type)
        fields = list(fields)

        # E003 / E004 – FAR field fixups
        if k == REC_FAR:
            cpu = _get_field(fields, rec_type, "CPU_TYPE")
            ver = _get_field(fields, rec_type, "STDF_VER")
            if cpu not in (1, 2):
                fields = _set_field(fields, rec_type, "CPU_TYPE", 2)
                result.actions.append(RepairAction("E004",
                    f"FAR CPU_TYPE={cpu} forced to 2 (little-endian)", offset))
            if ver != 4:
                fields = _set_field(fields, rec_type, "STDF_VER", 4)
                result.actions.append(RepairAction("E003",
                    f"FAR STDF_VER={ver} corrected to 4", offset))

        repaired_records.append((rec_type, fields))

    # ---------------------------------------------------------------
    # Step 4 – structural repairs (insert / remove / move records)
    # ---------------------------------------------------------------

    # --- E010: insert synthetic MIR if missing ----------------------
    has_far = any(_key(rt) == REC_FAR for rt, _ in repaired_records)
    has_mir = any(_key(rt) == REC_MIR for rt, _ in repaired_records)

    if not has_mir:
        ts = _now()
        synthetic_mir = [
            ts,             # SETUP_T
            ts,             # START_T
            1,              # STAT_NUM
            " ",            # MODE_COD
            " ",            # RTST_COD
            " ",            # PROT_COD
            65535,          # BURN_TIM (missing)
            " ",            # CMOD_COD
            "UNKNOWN",      # LOT_ID
            "UNKNOWN",      # PART_TYP
            "REPAIRED",     # NODE_NAM
            "UNKNOWN",      # TSTR_TYP
            "REPAIRED",     # JOB_NAM
        ]
        # Insert after FAR (and any ATRs)
        insert_pos = 0
        for i, (rt, _) in enumerate(repaired_records):
            k = _key(rt)
            if k in (REC_FAR, REC_ATR):
                insert_pos = i + 1
        repaired_records.insert(insert_pos, (V4.Mir, synthetic_mir))
        result.actions.append(RepairAction("E010",
            f"Synthetic MIR inserted at position {insert_pos}"))

    # --- E012/E013: MRR handling -------------------------------------
    mrr_entries = [(i, rt, flds) for i, (rt, flds) in enumerate(repaired_records)
                   if _key(rt) == REC_MRR]
    non_mrr = [(rt, flds) for rt, flds in repaired_records if _key(rt) != REC_MRR]

    if not mrr_entries:
        # E012: no MRR → create one
        ts = _now()
        synthetic_mrr = [ts, " ", None, None]
        repaired_records = non_mrr + [(V4.Mrr, synthetic_mrr)]
        result.actions.append(RepairAction("E012",
            "Synthetic MRR appended at end of file"))
    else:
        # Check if MRR is already last
        last_rt, _ = repaired_records[-1]
        if _key(last_rt) != REC_MRR:
            # E013: MRR exists but is not last → move it
            mrr_rt, mrr_flds = mrr_entries[-1][1], mrr_entries[-1][2]
            repaired_records = non_mrr + [(mrr_rt, mrr_flds)]
            result.actions.append(RepairAction("E013",
                "MRR moved to end of file"))

    # --- E040/E041/E042: timestamp fixups ----------------------------
    mir_entry  = next(((rt, flds) for rt, flds in repaired_records if _key(rt) == REC_MIR), None)
    mrr_entry  = next(((rt, flds) for rt, flds in reversed(repaired_records) if _key(rt) == REC_MRR), None)

    if mir_entry and mrr_entry:
        mir_rt, mir_flds = mir_entry
        mrr_rt, mrr_flds = mrr_entry

        start_t  = _get_field(mir_flds, mir_rt, "START_T")  or 0
        finish_t = _get_field(mrr_flds, mrr_rt, "FINISH_T") or 0
        ts_now = _now()

        if finish_t == 0:
            # E042
            new_ft = ts_now
            repaired_records = [
                (rt, _set_field(flds, rt, "FINISH_T", new_ft) if _key(rt) == REC_MRR else flds)
                for rt, flds in repaired_records
            ]
            result.actions.append(RepairAction("E042",
                f"MRR FINISH_T=0 set to current time ({new_ft})"))
            finish_t = new_ft

        if start_t == 0:
            # E040 – use finish_t as best guess
            new_st = finish_t
            repaired_records = [
                (rt, _set_field(flds, rt, "START_T", new_st) if _key(rt) == REC_MIR else flds)
                for rt, flds in repaired_records
            ]
            result.actions.append(RepairAction("E040",
                f"MIR START_T=0 set to FINISH_T ({new_st})"))
            start_t = new_st

        if start_t > 0 and finish_t > 0 and finish_t < start_t:
            # E041
            repaired_records = [
                (rt, _set_field(flds, rt, "FINISH_T", start_t) if _key(rt) == REC_MRR else flds)
                for rt, flds in repaired_records
            ]
            result.actions.append(RepairAction("E041",
                f"MRR FINISH_T ({finish_t}) < START_T ({start_t}); FINISH_T set to START_T"))

    # --- E015: unmatched PIR → insert synthetic PRR ------------------
    # Re-scan repaired_records to find still-open PIRs
    open_parts: Dict[Tuple, int] = {}
    fixed_records: List[Tuple] = []

    for rec_type, fields in repaired_records:
        k = _key(rec_type)

        if k == REC_PIR:
            head = _get_field(fields, rec_type, "HEAD_NUM") or 1
            site = _get_field(fields, rec_type, "SITE_NUM") or 1
            open_parts[(head, site)] = len(fixed_records)
            fixed_records.append((rec_type, fields))

        elif k == REC_PRR:
            head = _get_field(fields, rec_type, "HEAD_NUM") or 1
            site = _get_field(fields, rec_type, "SITE_NUM") or 1
            hs = (head, site)
            if hs not in open_parts:
                # E016: orphan PRR → insert synthetic PIR before it
                synth_pir = [head, site]
                fixed_records.append((V4.Pir, synth_pir))
                result.actions.append(RepairAction("E016",
                    f"Synthetic PIR inserted before orphan PRR (head={head} site={site})"))
            else:
                del open_parts[hs]
            fixed_records.append((rec_type, fields))

        else:
            fixed_records.append((rec_type, fields))

    # Close any still-open PIRs at the end (before MRR)
    if open_parts:
        # Insert before MRR
        insert_before_mrr = len(fixed_records) - 1  # MRR should be last now
        last_rt, _ = fixed_records[-1]
        if _key(last_rt) != REC_MRR:
            insert_before_mrr = len(fixed_records)

        for (head, site) in list(open_parts.keys()):
            synth_prr = [
                head, site,
                0x0A,   # PART_FLG: bit1=abnormal, bit3=fail
                0,      # NUM_TEST
                2,      # HARD_BIN (fail bin)
                2,      # SOFT_BIN (fail bin)
                -32768, -32768, 0, None, None, None
            ]
            fixed_records.insert(insert_before_mrr, (V4.Prr, synth_prr))
            result.actions.append(RepairAction("E015",
                f"Synthetic PRR inserted for unmatched PIR (head={head} site={site})"))
            insert_before_mrr += 1

    repaired_records = fixed_records

    # --- E017/E018: WIR–WRR matching --------------------------------
    open_wafers: Dict[Tuple, int] = {}
    fixed_records2: List[Tuple] = []

    for rec_type, fields in repaired_records:
        k = _key(rec_type)

        if k == REC_WIR:
            head = _get_field(fields, rec_type, "HEAD_NUM") or 1
            sg   = _get_field(fields, rec_type, "SITE_GRP") or 255
            open_wafers[(head, sg)] = len(fixed_records2)
            fixed_records2.append((rec_type, fields))

        elif k == REC_WRR:
            head = _get_field(fields, rec_type, "HEAD_NUM") or 1
            sg   = _get_field(fields, rec_type, "SITE_GRP") or 255
            hs   = (head, sg)
            if hs not in open_wafers:
                # E018: insert synthetic WIR before this WRR
                synth_wir = [head, sg, _now(), None]
                fixed_records2.append((V4.Wir, synth_wir))
                result.actions.append(RepairAction("E018",
                    f"Synthetic WIR inserted before orphan WRR (head={head} site_grp={sg})"))
            else:
                del open_wafers[hs]
            fixed_records2.append((rec_type, fields))

        else:
            fixed_records2.append((rec_type, fields))

    # Close open WIRs before MRR
    if open_wafers:
        last_rt, _ = fixed_records2[-1]
        ins = len(fixed_records2) - 1 if _key(last_rt) == REC_MRR else len(fixed_records2)
        for (head, sg) in list(open_wafers.keys()):
            synth_wrr = [head, sg, _now(), 0, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF,
                         None, None, None, None, None, None]
            fixed_records2.insert(ins, (V4.Wrr, synth_wrr))
            result.actions.append(RepairAction("E017",
                f"Synthetic WRR inserted for unmatched WIR (head={head} site_grp={sg})"))
            ins += 1

    repaired_records = fixed_records2

    # --- E034: duplicate PART_ID → rename with _retest, _retest2, ... --
    # Suffix scheme:
    #   1st occurrence  → unchanged  (e.g. '1')
    #   2nd occurrence  → _retest    (e.g. '1_retest')
    #   3rd occurrence  → _retest2   (e.g. '1_retest2')
    #   4th occurrence  → _retest3   (e.g. '1_retest3')  … and so on
    from collections import Counter
    part_id_counts: Counter = Counter()
    for rec_type, fields in repaired_records:
        if _key(rec_type) == REC_PRR:
            pid = _get_field(fields, rec_type, "PART_ID")
            if pid:
                part_id_counts[pid] += 1

    duplicates = {pid for pid, cnt in part_id_counts.items() if cnt > 1}

    if duplicates:
        seen_ids: Dict[str, int] = {}   # pid → occurrence count seen so far
        fixed_records3: List[Tuple] = []

        for rec_type, fields in repaired_records:
            if _key(rec_type) == REC_PRR:
                pid = _get_field(fields, rec_type, "PART_ID")
                if pid and pid in duplicates:
                    seen_ids[pid] = seen_ids.get(pid, 0) + 1
                    occurrence = seen_ids[pid]
                    if occurrence > 1:
                        # 2nd → _retest, 3rd → _retest2, 4th → _retest3, …
                        suffix = "_retest" if occurrence == 2 else f"_retest{occurrence - 1}"
                        new_pid = f"{pid}{suffix}"
                        fields = _set_field(list(fields), rec_type, "PART_ID", new_pid)
                        result.actions.append(RepairAction("E034",
                            f"Duplicate PART_ID '{pid}' (occurrence {occurrence}) "
                            f"renamed to '{new_pid}'"))
            fixed_records3.append((rec_type, fields))

        repaired_records = fixed_records3

    # ---------------------------------------------------------------
    # Step 5 – add repair ATR
    # ---------------------------------------------------------------
    if result.actions:
        repair_atr_flds = [
            _now(),
            f"stdfcompanion repair {input_path} -> {output_path} ({len(result.actions)} action(s))"
        ]
        # Insert ATR after FAR and any existing ATRs
        insert_pos = 0
        for i, (rt, _) in enumerate(repaired_records):
            k = _key(rt)
            if k in (REC_FAR, REC_ATR):
                insert_pos = i + 1
        repaired_records.insert(insert_pos, (V4.Atr, repair_atr_flds))

    # ---------------------------------------------------------------
    # Step 6 – write output
    # ---------------------------------------------------------------
    if verbose:
        print(f"  Writing:   {output_path}")
        for act in result.actions:
            print(f"   {act}")

    with StdfWriter(output_path) as writer:
        for rec_type, fields in repaired_records:
            writer.write_record(rec_type, fields)

    # ---------------------------------------------------------------
    # Step 7 – re-check the output to count remaining errors
    # ---------------------------------------------------------------
    post_check = check_stdf(output_path, verbose=False)
    result.errors_after = len(post_check.errors)

    if verbose:
        print(f"  {result.summary()}")

    return result
