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
        assert "stdfcompanion" in result.output
