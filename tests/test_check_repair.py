"""
tests/test_check_repair.py
~~~~~~~~~~~~~~~~~~~~~~~~~~

Tests for `stdfcompanion check` and `stdfcompanion repair`.

We re-use the synthetic STDF generator from test_merge.py and also
craft deliberately malformed files to verify every error code and
repair path.
"""

from __future__ import annotations

import struct
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from stdfcompanion.writer import StdfWriter
from stdfcompanion.commands.check  import check_stdf, Severity, Issue
from stdfcompanion.commands.repair import repair_stdf
from stdfcompanion.commands.merge  import _read_stdf
from stdfcompanion.cli import main
from pystdf import V4

# ---------------------------------------------------------------------------
# Re-usable STDF builder helpers
# ---------------------------------------------------------------------------

MISSING_U4 = 4_294_967_295


def _ts():
    return int(time.time())


def _make_good_stdf(path: str, lot_id: str = "LOT001",
                    part_count: int = 4, good_count: int = 3) -> None:
    """Write a clean, fully valid STDF file."""
    ts = _ts()
    with StdfWriter(path) as w:
        w.write_record(V4.Far, [2, 4])
        w.write_record(V4.Mir, [
            ts, ts, 1, " ", " ", " ", 65535, " ",
            lot_id, "PART_A", "NODE1", "TSTR_X", "job.prg",
        ])
        w.write_record(V4.Sdr, [1, 1, 1, [1]])
        for i in range(part_count):
            passed = i < good_count
            w.write_record(V4.Pir, [1, 1])
            w.write_record(V4.Ptr, [100, 1, 1, 0, 0, 1.0, "VoltageTest"])
            w.write_record(V4.Prr, [
                1, 1,
                0x00 if passed else 0x08,
                1, 1 if passed else 2, 1 if passed else 2,
                -32768, -32768, 0, f"SN{i+1:04d}",
            ])
        w.write_record(V4.Pcr, [255, 0, part_count, MISSING_U4, MISSING_U4,
                                 good_count, MISSING_U4])
        w.write_record(V4.Hbr, [255, 0, 1, good_count, "P", "PASS"])
        if good_count < part_count:
            w.write_record(V4.Hbr, [255, 0, 2, part_count - good_count, "F", "FAIL"])
        w.write_record(V4.Mrr, [ts, " ", None, None])


# ---------------------------------------------------------------------------
# Tests: check – clean file
# ---------------------------------------------------------------------------

class TestCheckCleanFile:

    def test_clean_file_passes(self, tmp_path):
        p = str(tmp_path / "good.stdf")
        _make_good_stdf(p)
        result = check_stdf(p)
        assert result.ok, [str(i) for i in result.errors]

    def test_clean_file_no_errors(self, tmp_path):
        p = str(tmp_path / "good.stdf")
        _make_good_stdf(p)
        result = check_stdf(p)
        assert len(result.errors) == 0

    def test_result_path_attribute(self, tmp_path):
        p = str(tmp_path / "good.stdf")
        _make_good_stdf(p)
        result = check_stdf(p)
        assert result.path == p


# ---------------------------------------------------------------------------
# Tests: check – structural errors
# ---------------------------------------------------------------------------

class TestCheckStructural:

    def test_E001_empty_file(self, tmp_path):
        p = str(tmp_path / "empty.stdf")
        Path(p).write_bytes(b"")
        result = check_stdf(p)
        codes = {i.code for i in result.issues}
        assert "E001" in codes

    def test_E001_too_small(self, tmp_path):
        p = str(tmp_path / "tiny.stdf")
        Path(p).write_bytes(b"\x00\x01\x02")
        result = check_stdf(p)
        codes = {i.code for i in result.issues}
        assert "E001" in codes

    def test_E002_no_far_first(self, tmp_path):
        """File starts with MIR instead of FAR."""
        p = str(tmp_path / "nofar.stdf")
        with StdfWriter(p) as w:
            # Write MIR as first record (wrong)
            ts = _ts()
            w.write_record(V4.Mir, [
                ts, ts, 1, " ", " ", " ", 65535, " ",
                "LOT1", "PART", "NODE", "TST", "job",
            ])
            w.write_record(V4.Mrr, [ts, " ", None, None])
        result = check_stdf(p)
        codes = {i.code for i in result.issues}
        assert "E002" in codes

    def test_E003_wrong_stdf_ver(self, tmp_path):
        """FAR with STDF_VER=3."""
        p = str(tmp_path / "ver3.stdf")
        with StdfWriter(p) as w:
            w.write_record(V4.Far, [2, 3])   # version 3
            ts = _ts()
            w.write_record(V4.Mir, [ts, ts, 1, " ", " ", " ", 65535, " ",
                                    "L1", "P", "N", "T", "j"])
            w.write_record(V4.Mrr, [ts, " ", None, None])
        result = check_stdf(p)
        codes = {i.code for i in result.issues}
        assert "E003" in codes

    def test_E005_truncated_file(self, tmp_path):
        """Truncate a valid file mid-record."""
        good = str(tmp_path / "good.stdf")
        _make_good_stdf(good)
        data = Path(good).read_bytes()
        truncated = str(tmp_path / "truncated.stdf")
        Path(truncated).write_bytes(data[:-20])
        result = check_stdf(truncated)
        codes = {i.code for i in result.issues}
        assert "E005" in codes


# ---------------------------------------------------------------------------
# Tests: check – sequence errors
# ---------------------------------------------------------------------------

class TestCheckSequence:

    def test_E010_missing_mir(self, tmp_path):
        p = str(tmp_path / "no_mir.stdf")
        ts = _ts()
        with StdfWriter(p) as w:
            w.write_record(V4.Far, [2, 4])
            # Skip MIR deliberately
            w.write_record(V4.Pir, [1, 1])
            w.write_record(V4.Prr, [1, 1, 0x00, 1, 1, 1, -32768, -32768, 0, "SN001"])
            w.write_record(V4.Pcr, [255, 0, 1, MISSING_U4, MISSING_U4, 1, MISSING_U4])
            w.write_record(V4.Mrr, [ts, " ", None, None])
        result = check_stdf(p)
        codes = {i.code for i in result.issues}
        assert "E010" in codes

    def test_E012_missing_mrr(self, tmp_path):
        p = str(tmp_path / "no_mrr.stdf")
        ts = _ts()
        with StdfWriter(p) as w:
            w.write_record(V4.Far, [2, 4])
            w.write_record(V4.Mir, [ts, ts, 1, " ", " ", " ", 65535, " ",
                                    "L1", "P", "N", "T", "j"])
            w.write_record(V4.Pir, [1, 1])
            w.write_record(V4.Prr, [1, 1, 0x00, 1, 1, 1, -32768, -32768, 0, "SN001"])
            # MRR omitted
        result = check_stdf(p)
        codes = {i.code for i in result.issues}
        assert "E012" in codes

    def test_E013_mrr_not_last(self, tmp_path):
        p = str(tmp_path / "mrr_early.stdf")
        ts = _ts()
        with StdfWriter(p) as w:
            w.write_record(V4.Far, [2, 4])
            w.write_record(V4.Mir, [ts, ts, 1, " ", " ", " ", 65535, " ",
                                    "L1", "P", "N", "T", "j"])
            w.write_record(V4.Mrr, [ts, " ", None, None])  # MRR too early
            # More records after MRR
            w.write_record(V4.Pir, [1, 1])
            w.write_record(V4.Prr, [1, 1, 0x00, 1, 1, 1, -32768, -32768, 0, "SN001"])
        result = check_stdf(p)
        codes = {i.code for i in result.issues}
        assert "E013" in codes

    def test_E015_unmatched_pir(self, tmp_path):
        p = str(tmp_path / "open_pir.stdf")
        ts = _ts()
        with StdfWriter(p) as w:
            w.write_record(V4.Far, [2, 4])
            w.write_record(V4.Mir, [ts, ts, 1, " ", " ", " ", 65535, " ",
                                    "L1", "P", "N", "T", "j"])
            w.write_record(V4.Pir, [1, 1])
            # PRR missing – PIR left open
            w.write_record(V4.Mrr, [ts, " ", None, None])
        result = check_stdf(p)
        codes = {i.code for i in result.issues}
        assert "E015" in codes

    def test_E016_orphan_prr(self, tmp_path):
        p = str(tmp_path / "orphan_prr.stdf")
        ts = _ts()
        with StdfWriter(p) as w:
            w.write_record(V4.Far, [2, 4])
            w.write_record(V4.Mir, [ts, ts, 1, " ", " ", " ", 65535, " ",
                                    "L1", "P", "N", "T", "j"])
            # PRR without PIR
            w.write_record(V4.Prr, [1, 1, 0x00, 1, 1, 1, -32768, -32768, 0, "SN001"])
            w.write_record(V4.Mrr, [ts, " ", None, None])
        result = check_stdf(p)
        codes = {i.code for i in result.issues}
        assert "E016" in codes


# ---------------------------------------------------------------------------
# Tests: check – timestamp errors
# ---------------------------------------------------------------------------

class TestCheckTimestamps:

    def test_E040_zero_start_t(self, tmp_path):
        p = str(tmp_path / "zero_start.stdf")
        ts = _ts()
        with StdfWriter(p) as w:
            w.write_record(V4.Far, [2, 4])
            w.write_record(V4.Mir, [0, 0, 1, " ", " ", " ", 65535, " ",
                                    "L1", "P", "N", "T", "j"])  # START_T=0
            w.write_record(V4.Pcr, [255, 0, 0, MISSING_U4, MISSING_U4, 0, MISSING_U4])
            w.write_record(V4.Mrr, [ts, " ", None, None])
        result = check_stdf(p)
        codes = {i.code for i in result.issues}
        assert "E040" in codes

    def test_E041_finish_before_start(self, tmp_path):
        p = str(tmp_path / "backwards_time.stdf")
        ts = _ts()
        with StdfWriter(p) as w:
            w.write_record(V4.Far, [2, 4])
            w.write_record(V4.Mir, [ts, ts, 1, " ", " ", " ", 65535, " ",
                                    "L1", "P", "N", "T", "j"])
            w.write_record(V4.Pcr, [255, 0, 0, MISSING_U4, MISSING_U4, 0, MISSING_U4])
            w.write_record(V4.Mrr, [ts - 3600, " ", None, None])  # finish < start
        result = check_stdf(p)
        codes = {i.code for i in result.issues}
        assert "E041" in codes

    def test_E042_zero_finish_t(self, tmp_path):
        p = str(tmp_path / "zero_finish.stdf")
        ts = _ts()
        with StdfWriter(p) as w:
            w.write_record(V4.Far, [2, 4])
            w.write_record(V4.Mir, [ts, ts, 1, " ", " ", " ", 65535, " ",
                                    "L1", "P", "N", "T", "j"])
            w.write_record(V4.Pcr, [255, 0, 0, MISSING_U4, MISSING_U4, 0, MISSING_U4])
            w.write_record(V4.Mrr, [0, " ", None, None])  # FINISH_T=0
        result = check_stdf(p)
        codes = {i.code for i in result.issues}
        assert "E042" in codes


# ---------------------------------------------------------------------------
# Tests: check – content warnings
# ---------------------------------------------------------------------------

class TestCheckContent:

    def test_E032_prr_hard_bin_0(self, tmp_path):
        p = str(tmp_path / "bin0.stdf")
        ts = _ts()
        with StdfWriter(p) as w:
            w.write_record(V4.Far, [2, 4])
            w.write_record(V4.Mir, [ts, ts, 1, " ", " ", " ", 65535, " ",
                                    "L1", "P", "N", "T", "j"])
            w.write_record(V4.Pir, [1, 1])
            w.write_record(V4.Prr, [1, 1, 0x00, 1, 0, 0,   # HARD_BIN=0
                                    -32768, -32768, 0, "SN001"])
            w.write_record(V4.Pcr, [255, 0, 1, MISSING_U4, MISSING_U4, 1, MISSING_U4])
            w.write_record(V4.Mrr, [ts, " ", None, None])
        result = check_stdf(p)
        codes = {i.code for i in result.issues}
        assert "E032" in codes

    def test_E034_duplicate_part_id(self, tmp_path):
        p = str(tmp_path / "dup_id.stdf")
        ts = _ts()
        with StdfWriter(p) as w:
            w.write_record(V4.Far, [2, 4])
            w.write_record(V4.Mir, [ts, ts, 1, " ", " ", " ", 65535, " ",
                                    "L1", "P", "N", "T", "j"])
            for _ in range(2):
                w.write_record(V4.Pir, [1, 1])
                w.write_record(V4.Prr, [1, 1, 0x00, 1, 1, 1,
                                        -32768, -32768, 0, "DUPLICATE"])
            w.write_record(V4.Pcr, [255, 0, 2, MISSING_U4, MISSING_U4, 2, MISSING_U4])
            w.write_record(V4.Mrr, [ts, " ", None, None])
        result = check_stdf(p)
        codes = {i.code for i in result.issues}
        assert "E034" in codes


# ---------------------------------------------------------------------------
# Tests: check summary and severity helpers
# ---------------------------------------------------------------------------

class TestCheckResultHelpers:

    def test_summary_ok(self, tmp_path):
        p = str(tmp_path / "good.stdf")
        _make_good_stdf(p)
        result = check_stdf(p)
        assert "OK" in result.summary()

    def test_summary_fail(self, tmp_path):
        p = str(tmp_path / "empty.stdf")
        Path(p).write_bytes(b"")
        result = check_stdf(p)
        assert "FAIL" in result.summary()

    def test_warnings_list(self, tmp_path):
        p = str(tmp_path / "good.stdf")
        _make_good_stdf(p)
        result = check_stdf(p)
        for i in result.warnings:
            assert i.severity == Severity.WARNING


# ---------------------------------------------------------------------------
# Tests: repair
# ---------------------------------------------------------------------------

class TestRepair:

    def test_repair_good_file_is_noop(self, tmp_path):
        src = str(tmp_path / "good.stdf")
        dst = str(tmp_path / "repaired.stdf")
        _make_good_stdf(src)
        result = repair_stdf(src, dst)
        assert not result.repaired

    def test_repair_inserts_missing_mrr(self, tmp_path):
        src = str(tmp_path / "no_mrr.stdf")
        dst = str(tmp_path / "repaired.stdf")
        ts = _ts()
        with StdfWriter(src) as w:
            w.write_record(V4.Far, [2, 4])
            w.write_record(V4.Mir, [ts, ts, 1, " ", " ", " ", 65535, " ",
                                    "L1", "P", "N", "T", "j"])
            w.write_record(V4.Pir, [1, 1])
            w.write_record(V4.Prr, [1, 1, 0x00, 1, 1, 1, -32768, -32768, 0, "S1"])
        repair_stdf(src, dst)
        post = check_stdf(dst)
        codes = {i.code for i in post.errors}
        assert "E012" not in codes

    def test_repair_inserts_missing_mir(self, tmp_path):
        src = str(tmp_path / "no_mir.stdf")
        dst = str(tmp_path / "repaired.stdf")
        ts = _ts()
        with StdfWriter(src) as w:
            w.write_record(V4.Far, [2, 4])
            w.write_record(V4.Pir, [1, 1])
            w.write_record(V4.Prr, [1, 1, 0x00, 1, 1, 1, -32768, -32768, 0, "S1"])
            w.write_record(V4.Mrr, [ts, " ", None, None])
        result = repair_stdf(src, dst)
        codes = {a.code for a in result.actions}
        assert "E010" in codes
        post = check_stdf(dst)
        assert "E010" not in {i.code for i in post.errors}

    def test_repair_moves_mrr_to_end(self, tmp_path):
        src = str(tmp_path / "mrr_early.stdf")
        dst = str(tmp_path / "repaired.stdf")
        ts = _ts()
        with StdfWriter(src) as w:
            w.write_record(V4.Far, [2, 4])
            w.write_record(V4.Mir, [ts, ts, 1, " ", " ", " ", 65535, " ",
                                    "L1", "P", "N", "T", "j"])
            w.write_record(V4.Mrr, [ts, " ", None, None])
            w.write_record(V4.Pir, [1, 1])
            w.write_record(V4.Prr, [1, 1, 0x00, 1, 1, 1, -32768, -32768, 0, "S1"])
        result = repair_stdf(src, dst)
        codes = {a.code for a in result.actions}
        assert "E013" in codes
        records = _read_stdf(dst)
        assert records[-1][0].__class__.__name__ == "Mrr"

    def test_repair_closes_open_pir(self, tmp_path):
        src = str(tmp_path / "open_pir.stdf")
        dst = str(tmp_path / "repaired.stdf")
        ts = _ts()
        with StdfWriter(src) as w:
            w.write_record(V4.Far, [2, 4])
            w.write_record(V4.Mir, [ts, ts, 1, " ", " ", " ", 65535, " ",
                                    "L1", "P", "N", "T", "j"])
            w.write_record(V4.Pir, [1, 1])
            # No PRR
            w.write_record(V4.Mrr, [ts, " ", None, None])
        result = repair_stdf(src, dst)
        codes = {a.code for a in result.actions}
        assert "E015" in codes
        post = check_stdf(dst)
        assert "E015" not in {i.code for i in post.errors}

    def test_repair_fixes_orphan_prr(self, tmp_path):
        src = str(tmp_path / "orphan_prr.stdf")
        dst = str(tmp_path / "repaired.stdf")
        ts = _ts()
        with StdfWriter(src) as w:
            w.write_record(V4.Far, [2, 4])
            w.write_record(V4.Mir, [ts, ts, 1, " ", " ", " ", 65535, " ",
                                    "L1", "P", "N", "T", "j"])
            w.write_record(V4.Prr, [1, 1, 0x00, 1, 1, 1, -32768, -32768, 0, "S1"])
            w.write_record(V4.Mrr, [ts, " ", None, None])
        result = repair_stdf(src, dst)
        codes = {a.code for a in result.actions}
        assert "E016" in codes
        post = check_stdf(dst)
        assert "E016" not in {i.code for i in post.errors}

    def test_repair_fixes_zero_finish_t(self, tmp_path):
        src = str(tmp_path / "zero_finish.stdf")
        dst = str(tmp_path / "repaired.stdf")
        ts = _ts()
        with StdfWriter(src) as w:
            w.write_record(V4.Far, [2, 4])
            w.write_record(V4.Mir, [ts, ts, 1, " ", " ", " ", 65535, " ",
                                    "L1", "P", "N", "T", "j"])
            w.write_record(V4.Mrr, [0, " ", None, None])  # FINISH_T=0
        result = repair_stdf(src, dst)
        codes = {a.code for a in result.actions}
        assert "E042" in codes
        post = check_stdf(dst)
        assert "E042" not in {i.code for i in post.errors}

    def test_repair_fixes_backwards_time(self, tmp_path):
        src = str(tmp_path / "backwards.stdf")
        dst = str(tmp_path / "repaired.stdf")
        ts = _ts()
        with StdfWriter(src) as w:
            w.write_record(V4.Far, [2, 4])
            w.write_record(V4.Mir, [ts, ts, 1, " ", " ", " ", 65535, " ",
                                    "L1", "P", "N", "T", "j"])
            w.write_record(V4.Mrr, [ts - 3600, " ", None, None])
        result = repair_stdf(src, dst)
        codes = {a.code for a in result.actions}
        assert "E041" in codes
        post = check_stdf(dst)
        assert "E041" not in {i.code for i in post.errors}

    def test_repair_adds_atr(self, tmp_path):
        src = str(tmp_path / "no_mrr.stdf")
        dst = str(tmp_path / "repaired.stdf")
        ts = _ts()
        with StdfWriter(src) as w:
            w.write_record(V4.Far, [2, 4])
            w.write_record(V4.Mir, [ts, ts, 1, " ", " ", " ", 65535, " ",
                                    "L1", "P", "N", "T", "j"])
        repair_stdf(src, dst)
        records = _read_stdf(dst)
        atrs = [r for r in records if r[0].__class__.__name__ == "Atr"]
        assert len(atrs) >= 1

    def test_repair_output_is_readable(self, tmp_path):
        src = str(tmp_path / "bad.stdf")
        dst = str(tmp_path / "repaired.stdf")
        ts = _ts()
        with StdfWriter(src) as w:
            w.write_record(V4.Far, [2, 4])
            # Missing MIR, missing MRR, open PIR
            w.write_record(V4.Pir, [1, 1])
        repair_stdf(src, dst)
        records = _read_stdf(dst)
        assert len(records) > 0

    def test_repair_errors_reduced(self, tmp_path):
        src = str(tmp_path / "bad.stdf")
        dst = str(tmp_path / "repaired.stdf")
        ts = _ts()
        with StdfWriter(src) as w:
            w.write_record(V4.Far, [2, 4])
            w.write_record(V4.Pir, [1, 1])
        result = repair_stdf(src, dst)
        assert result.errors_after <= result.errors_before

    def test_repair_result_summary(self, tmp_path):
        src = str(tmp_path / "bad.stdf")
        dst = str(tmp_path / "repaired.stdf")
        ts = _ts()
        with StdfWriter(src) as w:
            w.write_record(V4.Far, [2, 4])
            w.write_record(V4.Pir, [1, 1])
        result = repair_stdf(src, dst)
        s = result.summary()
        assert "repair" in s.lower() or "action" in s.lower()


# ---------------------------------------------------------------------------
# Tests: CLI check command
# ---------------------------------------------------------------------------

class TestCheckCLI:

    def test_cli_check_clean(self, tmp_path):
        p = str(tmp_path / "good.stdf")
        _make_good_stdf(p)
        runner = CliRunner()
        r = runner.invoke(main, ["check", p])
        assert r.exit_code == 0, r.output

    def test_cli_check_bad_exits_nonzero(self, tmp_path):
        p = str(tmp_path / "empty.stdf")
        Path(p).write_bytes(b"")
        runner = CliRunner()
        r = runner.invoke(main, ["check", p])
        assert r.exit_code != 0

    def test_cli_check_json_output(self, tmp_path):
        import json as _json
        p = str(tmp_path / "good.stdf")
        _make_good_stdf(p)
        runner = CliRunner()
        r = runner.invoke(main, ["check", p, "--json"])
        assert r.exit_code == 0
        data = _json.loads(r.output)
        assert isinstance(data, list)
        assert data[0]["ok"] is True

    def test_cli_check_multiple_files(self, tmp_path):
        p1 = str(tmp_path / "f1.stdf")
        p2 = str(tmp_path / "f2.stdf")
        _make_good_stdf(p1)
        _make_good_stdf(p2)
        runner = CliRunner()
        r = runner.invoke(main, ["check", p1, p2])
        assert r.exit_code == 0


# ---------------------------------------------------------------------------
# Tests: CLI repair command
# ---------------------------------------------------------------------------

class TestRepairCLI:

    def test_cli_repair_basic(self, tmp_path):
        src = str(tmp_path / "bad.stdf")
        dst = str(tmp_path / "fixed.stdf")
        ts = _ts()
        with StdfWriter(src) as w:
            w.write_record(V4.Far, [2, 4])
            w.write_record(V4.Pir, [1, 1])
        runner = CliRunner()
        r = runner.invoke(main, ["repair", src, "-o", dst])
        assert r.exit_code == 0, r.output
        assert Path(dst).exists()

    def test_cli_repair_verbose(self, tmp_path):
        src = str(tmp_path / "bad.stdf")
        dst = str(tmp_path / "fixed.stdf")
        ts = _ts()
        with StdfWriter(src) as w:
            w.write_record(V4.Far, [2, 4])
            w.write_record(V4.Pir, [1, 1])
        runner = CliRunner()
        r = runner.invoke(main, ["repair", src, "-o", dst, "--verbose"])
        assert r.exit_code == 0, r.output

    def test_cli_repair_good_file_says_no_repairs(self, tmp_path):
        src = str(tmp_path / "good.stdf")
        dst = str(tmp_path / "out.stdf")
        _make_good_stdf(src)
        runner = CliRunner()
        r = runner.invoke(main, ["repair", src, "-o", dst])
        assert r.exit_code == 0
        assert "No repairs" in r.output
