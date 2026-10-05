"""
tests/test_merge.py
~~~~~~~~~~~~~~~~~~~

End-to-end tests for the `stdfcompanion merge` command.

The tests create minimal but valid STDF v4 binary files from scratch
(no real ATE data needed) and verify that merging them produces a
well-formed output that can be re-read by pystdf.
"""

from __future__ import annotations

import os
import struct
import time
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from stdfcompanion.writer import StdfWriter, pack_record
from stdfcompanion.commands.merge import merge_stdf, _read_stdf
from stdfcompanion.cli import main
from pystdf import V4

# ---------------------------------------------------------------------------
# Helpers – build minimal STDF files
# ---------------------------------------------------------------------------

MISSING_U4 = 4_294_967_295

def _ts():
    return int(time.time())


def _make_stdf(path: str, lot_id: str, part_count: int,
               good_count: int, hbin: int = 1) -> None:
    """
    Write a minimal but complete STDF v4 file with *part_count* parts.

    Structure:
        FAR  MIR  SDR
        [PIR  PTR  PRR] x part_count
        PCR(255)  HBR(255)  SBR(255)
        MRR
    """
    ts = _ts()

    with StdfWriter(path) as w:
        # FAR
        w.write_record(V4.Far, [2, 4])   # CPU_TYPE=2 (LE), STDF_VER=4

        # MIR
        mir_flds = [
            ts,            # SETUP_T
            ts,            # START_T
            1,             # STAT_NUM
            " ",           # MODE_COD
            " ",           # RTST_COD
            " ",           # PROT_COD
            65535,         # BURN_TIM (missing)
            " ",           # CMOD_COD
            lot_id,        # LOT_ID
            "TEST_PART",   # PART_TYP
            "NODE1",       # NODE_NAM
            "TSTR_A",      # TSTR_TYP
            "job.prg",     # JOB_NAM
        ]
        w.write_record(V4.Mir, mir_flds)

        # SDR
        sdr_flds = [
            1,    # HEAD_NUM
            1,    # SITE_GRP
            1,    # SITE_CNT
            [1],  # SITE_NUM array
        ]
        w.write_record(V4.Sdr, sdr_flds)

        # Part records
        for i in range(part_count):
            pir_flds = [1, 1]   # HEAD_NUM, SITE_NUM
            w.write_record(V4.Pir, pir_flds)

            # PTR: test 100, result=1.0 V, limits [0.5, 1.5]
            ptr_flds = [
                100,         # TEST_NUM
                1,           # HEAD_NUM
                1,           # SITE_NUM
                0b00000000,  # TEST_FLG (pass)
                0b00000000,  # PARM_FLG
                1.0,         # RESULT
                "VoltageTest",  # TEST_TXT
            ]
            w.write_record(V4.Ptr, ptr_flds)

            # PRR
            passed = i < good_count
            part_flg = 0x00 if passed else 0x08  # bit3=1 means fail
            prr_flds = [
                1,              # HEAD_NUM
                1,              # SITE_NUM
                part_flg,       # PART_FLG
                1,              # NUM_TEST
                hbin if passed else 2,   # HARD_BIN
                hbin if passed else 2,   # SOFT_BIN
                -32768,         # X_COORD (missing)
                -32768,         # Y_COORD (missing)
                0,              # TEST_T
                f"SN{i+1:04d}", # PART_ID
            ]
            w.write_record(V4.Prr, prr_flds)

        # PCR summary
        pcr_flds = [
            255,            # HEAD_NUM (summary)
            0,              # SITE_NUM
            part_count,     # PART_CNT
            MISSING_U4,     # RTST_CNT
            MISSING_U4,     # ABRT_CNT
            good_count,     # GOOD_CNT
            MISSING_U4,     # FUNC_CNT
        ]
        w.write_record(V4.Pcr, pcr_flds)

        # HBR summary – bin 1 = pass, bin 2 = fail
        for bin_num, cnt, pf, name in [
            (1, good_count, "P", "PASS"),
            (2, part_count - good_count, "F", "FAIL"),
        ]:
            if cnt > 0:
                w.write_record(V4.Hbr, [255, 0, bin_num, cnt, pf, name])

        # SBR summary (same bins)
        for bin_num, cnt, pf, name in [
            (1, good_count, "P", "PASS"),
            (2, part_count - good_count, "F", "FAIL"),
        ]:
            if cnt > 0:
                w.write_record(V4.Sbr, [255, 0, bin_num, cnt, pf, name])

        # MRR
        w.write_record(V4.Mrr, [ts, " ", None, None])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestStdfWriter:
    """Unit tests for the binary writer."""

    def test_far_roundtrip(self, tmp_path):
        out = str(tmp_path / "far.stdf")
        with StdfWriter(out) as w:
            w.write_record(V4.Far, [2, 4])
            # Need at least MIR + MRR for a valid file; skip here – just check bytes
        data = Path(out).read_bytes()
        # Header: REC_LEN=2, REC_TYP=0, REC_SUB=10
        assert data[2] == 0   # REC_TYP
        assert data[3] == 10  # REC_SUB
        # CPU_TYPE=2
        assert data[4] == 2
        # STDF_VER=4
        assert data[5] == 4

    def test_writer_context_manager(self, tmp_path):
        out = str(tmp_path / "ctx.stdf")
        with StdfWriter(out) as w:
            assert not w._file.closed
        assert w._file.closed

    def test_pack_cn_none(self):
        from stdfcompanion.writer import _pack_cn
        assert _pack_cn(None) == b"\x00"

    def test_pack_cn_value(self):
        from stdfcompanion.writer import _pack_cn
        result = _pack_cn("hello")
        assert result[0] == 5
        assert result[1:] == b"hello"


class TestMakeSyntheticStdf:
    """Verify that our synthetic STDF generator creates readable files."""

    def test_synthetic_readable(self, tmp_path):
        path = str(tmp_path / "synth.stdf")
        _make_stdf(path, "LOT001", part_count=5, good_count=4)
        records = _read_stdf(path)
        rec_types = [rt.__class__.__name__ for rt, _ in records]
        assert "Far" in rec_types
        assert "Mir" in rec_types
        assert "Pir" in rec_types
        assert "Prr" in rec_types
        assert "Mrr" in rec_types

    def test_synthetic_part_count(self, tmp_path):
        path = str(tmp_path / "synth.stdf")
        _make_stdf(path, "LOT001", part_count=10, good_count=8)
        records = _read_stdf(path)
        pirs = [r for r in records if r[0].__class__.__name__ == "Pir"]
        assert len(pirs) == 10


class TestMerge:
    """Integration tests for merge_stdf()."""

    def test_merge_basic(self, tmp_path):
        f1 = str(tmp_path / "f1.stdf")
        f2 = str(tmp_path / "f2.stdf")
        out = str(tmp_path / "merged.stdf")

        _make_stdf(f1, "LOT001", part_count=5, good_count=4)
        _make_stdf(f2, "LOT001", part_count=3, good_count=3)

        merge_stdf([f1, f2], out)

        assert Path(out).exists()
        assert Path(out).stat().st_size > 0

    def test_merged_is_readable_by_pystdf(self, tmp_path):
        f1 = str(tmp_path / "f1.stdf")
        f2 = str(tmp_path / "f2.stdf")
        out = str(tmp_path / "merged.stdf")

        _make_stdf(f1, "LOT001", part_count=4, good_count=3)
        _make_stdf(f2, "LOT001", part_count=6, good_count=5)

        merge_stdf([f1, f2], out)
        records = _read_stdf(out)
        assert len(records) > 0

    def test_merged_part_count(self, tmp_path):
        f1 = str(tmp_path / "f1.stdf")
        f2 = str(tmp_path / "f2.stdf")
        out = str(tmp_path / "merged.stdf")

        _make_stdf(f1, "LOT001", part_count=4, good_count=3)
        _make_stdf(f2, "LOT001", part_count=6, good_count=5)

        merge_stdf([f1, f2], out)
        records = _read_stdf(out)

        pirs = [r for r in records if r[0].__class__.__name__ == "Pir"]
        assert len(pirs) == 10  # 4 + 6

    def test_merged_has_single_far(self, tmp_path):
        f1 = str(tmp_path / "f1.stdf")
        f2 = str(tmp_path / "f2.stdf")
        out = str(tmp_path / "merged.stdf")

        _make_stdf(f1, "LOT001", part_count=2, good_count=2)
        _make_stdf(f2, "LOT001", part_count=2, good_count=1)

        merge_stdf([f1, f2], out)
        records = _read_stdf(out)

        fars = [r for r in records if r[0].__class__.__name__ == "Far"]
        assert len(fars) == 1

    def test_merged_has_single_mir(self, tmp_path):
        f1 = str(tmp_path / "f1.stdf")
        f2 = str(tmp_path / "f2.stdf")
        out = str(tmp_path / "merged.stdf")

        _make_stdf(f1, "LOT001", part_count=2, good_count=2)
        _make_stdf(f2, "LOT001", part_count=2, good_count=1)

        merge_stdf([f1, f2], out)
        records = _read_stdf(out)

        mirs = [r for r in records if r[0].__class__.__name__ == "Mir"]
        assert len(mirs) == 1

    def test_merged_has_single_mrr(self, tmp_path):
        f1 = str(tmp_path / "f1.stdf")
        f2 = str(tmp_path / "f2.stdf")
        out = str(tmp_path / "merged.stdf")

        _make_stdf(f1, "LOT001", part_count=2, good_count=2)
        _make_stdf(f2, "LOT001", part_count=2, good_count=1)

        merge_stdf([f1, f2], out)
        records = _read_stdf(out)

        mrrs = [r for r in records if r[0].__class__.__name__ == "Mrr"]
        assert len(mrrs) == 1

    def test_merged_mrr_is_last(self, tmp_path):
        f1 = str(tmp_path / "f1.stdf")
        f2 = str(tmp_path / "f2.stdf")
        out = str(tmp_path / "merged.stdf")

        _make_stdf(f1, "LOT001", part_count=2, good_count=2)
        _make_stdf(f2, "LOT001", part_count=2, good_count=1)

        merge_stdf([f1, f2], out)
        records = _read_stdf(out)

        assert records[-1][0].__class__.__name__ == "Mrr"

    def test_merged_pcr_totals(self, tmp_path):
        f1 = str(tmp_path / "f1.stdf")
        f2 = str(tmp_path / "f2.stdf")
        out = str(tmp_path / "merged.stdf")

        _make_stdf(f1, "LOT001", part_count=4, good_count=3)
        _make_stdf(f2, "LOT001", part_count=6, good_count=5)

        merge_stdf([f1, f2], out)
        records = _read_stdf(out)

        # Find summary PCR (HEAD_NUM=255)
        from stdfcompanion.commands.merge import _get_field
        summary_pcrs = [
            (rt, flds) for rt, flds in records
            if rt.__class__.__name__ == "Pcr"
            and _get_field(flds, rt, "HEAD_NUM") == 255
        ]
        assert len(summary_pcrs) >= 1
        rt, flds = summary_pcrs[0]
        total_parts = _get_field(flds, rt, "PART_CNT")
        assert total_parts == 10  # 4 + 6

    def test_merge_requires_two_files(self, tmp_path):
        f1 = str(tmp_path / "f1.stdf")
        _make_stdf(f1, "LOT001", part_count=2, good_count=2)

        with pytest.raises(ValueError, match="at least 2"):
            merge_stdf([f1], str(tmp_path / "out.stdf"))

    def test_merge_three_files(self, tmp_path):
        files = []
        for i in range(3):
            p = str(tmp_path / f"f{i}.stdf")
            _make_stdf(p, "LOT001", part_count=3, good_count=2)
            files.append(p)

        out = str(tmp_path / "merged3.stdf")
        merge_stdf(files, out)
        records = _read_stdf(out)
        pirs = [r for r in records if r[0].__class__.__name__ == "Pir"]
        assert len(pirs) == 9  # 3 * 3

    def test_atr_added(self, tmp_path):
        f1 = str(tmp_path / "f1.stdf")
        f2 = str(tmp_path / "f2.stdf")
        out = str(tmp_path / "merged.stdf")

        _make_stdf(f1, "LOT001", part_count=2, good_count=2)
        _make_stdf(f2, "LOT001", part_count=2, good_count=1)

        merge_stdf([f1, f2], out)
        records = _read_stdf(out)
        atrs = [r for r in records if r[0].__class__.__name__ == "Atr"]
        # At least 1 ATR (the merge record)
        assert len(atrs) >= 1


class TestCLI:
    """Tests for the Click CLI."""

    def test_merge_cli_basic(self, tmp_path):
        f1 = str(tmp_path / "f1.stdf")
        f2 = str(tmp_path / "f2.stdf")
        out = str(tmp_path / "merged.stdf")

        _make_stdf(f1, "LOT001", part_count=3, good_count=2)
        _make_stdf(f2, "LOT001", part_count=4, good_count=3)

        runner = CliRunner()
        result = runner.invoke(main, ["merge", f1, f2, "-o", out])

        assert result.exit_code == 0, result.output
        assert Path(out).exists()

    def test_merge_cli_verbose(self, tmp_path):
        f1 = str(tmp_path / "f1.stdf")
        f2 = str(tmp_path / "f2.stdf")
        out = str(tmp_path / "merged.stdf")

        _make_stdf(f1, "LOT001", part_count=2, good_count=2)
        _make_stdf(f2, "LOT001", part_count=2, good_count=1)

        runner = CliRunner()
        result = runner.invoke(main, ["merge", f1, f2, "-o", out, "--verbose"])

        assert result.exit_code == 0, result.output
        assert "Reading" in result.output

    def test_merge_cli_too_few_inputs(self, tmp_path):
        f1 = str(tmp_path / "f1.stdf")
        _make_stdf(f1, "LOT001", part_count=2, good_count=2)
        out = str(tmp_path / "out.stdf")

        runner = CliRunner()
        result = runner.invoke(main, ["merge", f1, "-o", out])
        # Should fail (exit code != 0 or error message)
        assert result.exit_code != 0 or "Error" in result.output

    def test_version(self):
        runner = CliRunner()
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Helpers for TSR / PCR merge tests
# ---------------------------------------------------------------------------

def _make_stdf_with_tsr(
    path: str,
    lot_id: str,
    part_count: int,
    good_count: int,
    test_nums,           # list of test numbers to include
    exec_counts=None,    # dict TEST_NUM -> EXEC_CNT (default = part_count)
    fail_counts=None,    # dict TEST_NUM -> FAIL_CNT (default = 0)
    tst_sums=None,       # dict TEST_NUM -> TST_SUMS (default = 0.0)
    tst_sqrs=None,       # dict TEST_NUM -> TST_SQRS (default = 0.0)
    test_min=None,       # dict TEST_NUM -> TEST_MIN
    test_max=None,       # dict TEST_NUM -> TEST_MAX
    pcr_per_site=False,  # if True write per-site PCR only (no HEAD=255 summary)
) -> None:
    """Write an STDF with TSR records for *test_nums*."""
    if exec_counts is None:
        exec_counts = {t: part_count for t in test_nums}
    if fail_counts is None:
        fail_counts = {t: 0 for t in test_nums}
    if tst_sums is None:
        tst_sums = {t: float(part_count) for t in test_nums}
    if tst_sqrs is None:
        tst_sqrs = {t: float(part_count) for t in test_nums}
    if test_min is None:
        test_min = {t: 0.0 for t in test_nums}
    if test_max is None:
        test_max = {t: 1.0 for t in test_nums}

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
            for t in test_nums:
                w.write_record(V4.Ptr, [t, 1, 1, 0, 0, 1.0, f"Test{t}"])
            w.write_record(V4.Prr, [
                1, 1, 0x00 if passed else 0x08, len(test_nums),
                1 if passed else 2, 1 if passed else 2,
                -32768, -32768, 0, f"SN{i+1:04d}",
            ])

        if pcr_per_site:
            # Write per-site PCR only (no summary HEAD=255)
            w.write_record(V4.Pcr, [1, 1, part_count, MISSING_U4, MISSING_U4,
                                    good_count, MISSING_U4])
        else:
            # Write summary PCR (HEAD=255)
            w.write_record(V4.Pcr, [255, 0, part_count, MISSING_U4, MISSING_U4,
                                    good_count, MISSING_U4])

        # TSR records
        for t in test_nums:
            # TSR fields: HEAD, SITE, TEST_TYP, TEST_NUM, EXEC_CNT, FAIL_CNT,
            # ALRM_CNT, TEST_NAM, SEQ_NAME, TEST_LBL, OPT_FLAG,
            # TEST_TIM, TEST_MIN, TEST_MAX, TST_SUMS, TST_SQRS
            w.write_record(V4.Tsr, [
                1, 1, "P", t,
                exec_counts[t], fail_counts[t], 0,
                f"Test{t}", None, None, None,
                0.0, test_min[t], test_max[t],
                tst_sums[t], tst_sqrs[t],
            ])

        w.write_record(V4.Mrr, [ts, " ", None, None])


# ---------------------------------------------------------------------------
# Tests: TSR merge
# ---------------------------------------------------------------------------

class TestTsrMerge:
    """Verify TSR de-duplication and statistical merging."""

    def test_same_test_program_tsr_count(self, tmp_path):
        """Two files with same TEST_NUMs → merged file has one TSR per test."""
        f1 = str(tmp_path / "f1.stdf")
        f2 = str(tmp_path / "f2.stdf")
        out = str(tmp_path / "merged.stdf")
        tests = [1, 2, 3]
        _make_stdf_with_tsr(f1, "LOT1", 200, 180, tests)
        _make_stdf_with_tsr(f2, "LOT1",  50,  45, tests)
        merge_stdf([f1, f2], out)
        records = _read_stdf(out)
        tsrs = [r for r in records if r[0].__class__.__name__ == "Tsr"]
        # 3 tests → exactly 3 TSRs (de-duplicated, not 6)
        assert len(tsrs) == 3

    def test_tsr_exec_cnt_summed(self, tmp_path):
        """EXEC_CNT must be 200+50=250 after merge."""
        f1 = str(tmp_path / "f1.stdf")
        f2 = str(tmp_path / "f2.stdf")
        out = str(tmp_path / "merged.stdf")
        tests = [1]
        _make_stdf_with_tsr(f1, "LOT1", 200, 200, tests,
                             exec_counts={1: 200})
        _make_stdf_with_tsr(f2, "LOT1",  50,  50, tests,
                             exec_counts={1: 50})
        merge_stdf([f1, f2], out)
        records = _read_stdf(out)
        tsrs = [(rt, flds) for rt, flds in records if rt.__class__.__name__ == "Tsr"]
        from stdfcompanion.commands.check import _get_field
        exec_cnt = _get_field(tsrs[0][1], tsrs[0][0], "EXEC_CNT")
        assert exec_cnt == 250

    def test_tsr_fail_cnt_summed(self, tmp_path):
        """FAIL_CNT must be summed across files."""
        f1 = str(tmp_path / "f1.stdf")
        f2 = str(tmp_path / "f2.stdf")
        out = str(tmp_path / "merged.stdf")
        tests = [1]
        _make_stdf_with_tsr(f1, "LOT1", 200, 190, tests,
                             exec_counts={1: 200}, fail_counts={1: 10})
        _make_stdf_with_tsr(f2, "LOT1",  50,  45, tests,
                             exec_counts={1: 50},  fail_counts={1: 5})
        merge_stdf([f1, f2], out)
        records = _read_stdf(out)
        tsrs = [(rt, flds) for rt, flds in records if rt.__class__.__name__ == "Tsr"]
        from stdfcompanion.commands.check import _get_field
        fail_cnt = _get_field(tsrs[0][1], tsrs[0][0], "FAIL_CNT")
        assert fail_cnt == 15

    def test_tsr_tst_sums_summed(self, tmp_path):
        """TST_SUMS must be summed (for mean calculation)."""
        f1 = str(tmp_path / "f1.stdf")
        f2 = str(tmp_path / "f2.stdf")
        out = str(tmp_path / "merged.stdf")
        tests = [1]
        _make_stdf_with_tsr(f1, "LOT1", 200, 200, tests,
                             tst_sums={1: 100.0})
        _make_stdf_with_tsr(f2, "LOT1",  50,  50, tests,
                             tst_sums={1: 25.0})
        merge_stdf([f1, f2], out)
        records = _read_stdf(out)
        tsrs = [(rt, flds) for rt, flds in records if rt.__class__.__name__ == "Tsr"]
        from stdfcompanion.commands.check import _get_field
        tst_sums = _get_field(tsrs[0][1], tsrs[0][0], "TST_SUMS")
        assert abs(tst_sums - 125.0) < 0.001

    def test_tsr_test_min_is_minimum(self, tmp_path):
        """TEST_MIN must be the minimum across files."""
        f1 = str(tmp_path / "f1.stdf")
        f2 = str(tmp_path / "f2.stdf")
        out = str(tmp_path / "merged.stdf")
        tests = [1]
        _make_stdf_with_tsr(f1, "LOT1", 200, 200, tests,
                             test_min={1: 0.5}, test_max={1: 1.5})
        _make_stdf_with_tsr(f2, "LOT1",  50,  50, tests,
                             test_min={1: 0.3}, test_max={1: 1.2})
        merge_stdf([f1, f2], out)
        records = _read_stdf(out)
        tsrs = [(rt, flds) for rt, flds in records if rt.__class__.__name__ == "Tsr"]
        from stdfcompanion.commands.check import _get_field
        test_min = _get_field(tsrs[0][1], tsrs[0][0], "TEST_MIN")
        assert abs(test_min - 0.3) < 0.001

    def test_tsr_test_max_is_maximum(self, tmp_path):
        """TEST_MAX must be the maximum across files."""
        f1 = str(tmp_path / "f1.stdf")
        f2 = str(tmp_path / "f2.stdf")
        out = str(tmp_path / "merged.stdf")
        tests = [1]
        _make_stdf_with_tsr(f1, "LOT1", 200, 200, tests,
                             test_min={1: 0.5}, test_max={1: 1.5})
        _make_stdf_with_tsr(f2, "LOT1",  50,  50, tests,
                             test_min={1: 0.3}, test_max={1: 1.8})
        merge_stdf([f1, f2], out)
        records = _read_stdf(out)
        tsrs = [(rt, flds) for rt, flds in records if rt.__class__.__name__ == "Tsr"]
        from stdfcompanion.commands.check import _get_field
        test_max = _get_field(tsrs[0][1], tsrs[0][0], "TEST_MAX")
        assert abs(test_max - 1.8) < 0.001

    def test_different_test_programs_tsr_count(self, tmp_path):
        """Two files with different TEST_NUMs → all TSRs preserved (no merge)."""
        f1 = str(tmp_path / "f1.stdf")
        f2 = str(tmp_path / "f2.stdf")
        out = str(tmp_path / "merged.stdf")
        _make_stdf_with_tsr(f1, "LOT1", 100, 90, [1, 2, 3])
        _make_stdf_with_tsr(f2, "LOT1", 100, 90, [4, 5, 6])
        merge_stdf([f1, f2], out)
        records = _read_stdf(out)
        tsrs = [r for r in records if r[0].__class__.__name__ == "Tsr"]
        # 6 unique test numbers → 6 TSRs
        assert len(tsrs) == 6

    def test_tsr_after_summary_records_before_mrr(self, tmp_path):
        """TSRs must appear after PCR and before MRR."""
        f1 = str(tmp_path / "f1.stdf")
        f2 = str(tmp_path / "f2.stdf")
        out = str(tmp_path / "merged.stdf")
        _make_stdf_with_tsr(f1, "LOT1", 5, 5, [1, 2])
        _make_stdf_with_tsr(f2, "LOT1", 5, 5, [1, 2])
        merge_stdf([f1, f2], out)
        records = _read_stdf(out)
        names = [r[0].__class__.__name__ for r in records]
        tsr_idx = names.index("Tsr")
        pcr_idx = names.index("Pcr")
        mrr_idx = names.index("Mrr")
        assert pcr_idx < tsr_idx < mrr_idx

    def test_no_tsr_in_source_no_tsr_in_output(self, tmp_path):
        """Files without TSRs produce a merged file without TSRs."""
        f1 = str(tmp_path / "f1.stdf")
        f2 = str(tmp_path / "f2.stdf")
        out = str(tmp_path / "merged.stdf")
        _make_stdf(f1, "LOT1", 3, 3)
        _make_stdf(f2, "LOT1", 3, 3)
        merge_stdf([f1, f2], out)
        records = _read_stdf(out)
        tsrs = [r for r in records if r[0].__class__.__name__ == "Tsr"]
        assert len(tsrs) == 0


# ---------------------------------------------------------------------------
# Tests: PCR double-count fix
# ---------------------------------------------------------------------------

class TestPcrMerge:

    def test_pcr_no_double_count_with_summary(self, tmp_path):
        """File with both HEAD=255 summary + per-site PCR must not double-count."""
        src = str(tmp_path / "both_pcr.stdf")
        out = str(tmp_path / "merged.stdf")
        ts = _ts()
        with StdfWriter(src) as w:
            w.write_record(V4.Far, [2, 4])
            w.write_record(V4.Mir, [
                ts, ts, 1, " ", " ", " ", 65535, " ",
                "LOT1", "P", "N", "T", "j",
            ])
            for i in range(10):
                w.write_record(V4.Pir, [1, 1])
                w.write_record(V4.Ptr, [1, 1, 1, 0, 0, 1.0, "T1"])
                w.write_record(V4.Prr, [1, 1, 0x00, 1, 1, 1,
                                        -32768, -32768, 0, f"SN{i+1:04d}"])
            # Both per-site AND summary PCR (common tester output)
            w.write_record(V4.Pcr, [1, 1, 10, MISSING_U4, MISSING_U4, 10, MISSING_U4])
            w.write_record(V4.Pcr, [255, 0, 10, MISSING_U4, MISSING_U4, 10, MISSING_U4])
            w.write_record(V4.Mrr, [ts, " ", None, None])

        f2 = str(tmp_path / "f2.stdf")
        _make_stdf(f2, "LOT1", 5, 5)
        merge_stdf([src, f2], out)

        records = _read_stdf(out)
        from stdfcompanion.commands.check import _get_field
        pcrs = [(rt, flds) for rt, flds in records
                if rt.__class__.__name__ == "Pcr"
                and (_get_field(flds, rt, "HEAD_NUM") or 0) == 255]
        assert len(pcrs) == 1
        # Should be 10 (from summary, not 20) + 5 = 15, not 25
        assert _get_field(pcrs[0][1], pcrs[0][0], "PART_CNT") == 15

    def test_pcr_per_site_only_accumulates_correctly(self, tmp_path):
        """Files with only per-site PCRs (no HEAD=255) still count correctly."""
        f1 = str(tmp_path / "f1.stdf")
        f2 = str(tmp_path / "f2.stdf")
        out = str(tmp_path / "merged.stdf")
        _make_stdf_with_tsr(f1, "LOT1", 10, 9, [1], pcr_per_site=True)
        _make_stdf_with_tsr(f2, "LOT1", 15, 14, [1], pcr_per_site=True)
        merge_stdf([f1, f2], out)
        records = _read_stdf(out)
        from stdfcompanion.commands.check import _get_field
        pcrs = [(rt, flds) for rt, flds in records
                if rt.__class__.__name__ == "Pcr"
                and (_get_field(flds, rt, "HEAD_NUM") or 0) == 255]
        assert len(pcrs) == 1
        assert _get_field(pcrs[0][1], pcrs[0][0], "PART_CNT") == 25
