"""
tests/test_inspect.py
~~~~~~~~~~~~~~~~~~~~~

Tests for `stdfcompanion inspect`.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from stdfcompanion.writer import StdfWriter
from stdfcompanion.commands.inspect import inspect_stdf
from stdfcompanion.cli import main
from pystdf import V4

MISSING_U4 = 4_294_967_295


def _ts():
    return int(time.time())


def _make_stdf(path: str, part_ids=None, lot_id="LOT001"):
    """Write a small but complete STDF with optional PART_IDs list."""
    if part_ids is None:
        part_ids = ["SN001", "SN002", "SN003"]
    ts = _ts()
    with StdfWriter(path) as w:
        w.write_record(V4.Far, [2, 4])
        w.write_record(V4.Mir, [
            ts, ts, 1, " ", " ", " ", 65535, " ",
            lot_id, "PART_A", "NODE1", "TSTR_X", "job.prg",
        ])
        w.write_record(V4.Sdr, [1, 1, 1, [1]])
        for i, pid in enumerate(part_ids):
            w.write_record(V4.Pir, [1, 1])
            w.write_record(V4.Ptr, [100 + i, 1, 1, 0, 0, float(i), f"Test{i}"])
            w.write_record(V4.Prr, [
                1, 1, 0x00, 1, 1, 1, -32768, -32768, 0, pid,
            ])
        w.write_record(V4.Pcr, [255, 0, len(part_ids),
                                 MISSING_U4, MISSING_U4, len(part_ids), MISSING_U4])
        w.write_record(V4.Mrr, [ts, " ", None, None])


# ---------------------------------------------------------------------------
# text output
# ---------------------------------------------------------------------------

class TestInspectText:

    def test_returns_string(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p)
        result = inspect_stdf(p, output_format="text")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_contains_far(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p)
        result = inspect_stdf(p, output_format="text")
        assert "FAR" in result

    def test_contains_mir(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p)
        result = inspect_stdf(p, output_format="text")
        assert "MIR" in result

    def test_contains_lot_id(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p, lot_id="TESTLOT99")
        result = inspect_stdf(p, output_format="text")
        assert "TESTLOT99" in result

    def test_contains_part_id(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p, part_ids=["MYPART001"])
        result = inspect_stdf(p, output_format="text")
        assert "MYPART001" in result

    def test_contains_pir_and_prr(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p)
        result = inspect_stdf(p, output_format="text")
        assert "PIR" in result
        assert "PRR" in result

    def test_contains_ptr(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p)
        result = inspect_stdf(p, output_format="text")
        assert "PTR" in result

    def test_contains_mrr(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p)
        result = inspect_stdf(p, output_format="text")
        assert "MRR" in result

    def test_offset_shown(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p)
        result = inspect_stdf(p, output_format="text")
        assert "offset=0" in result  # FAR is always at offset 0

    def test_timestamp_human_readable(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p)
        result = inspect_stdf(p, output_format="text")
        # timestamps are rendered as HH:MM:SS DD-Mon-YYYY not as raw integers
        import re
        assert re.search(r"\d{2}:\d{2}:\d{2} \d{2}-\w{3}-\d{4}", result)

    def test_head_site_tag(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p)
        result = inspect_stdf(p, output_format="text")
        assert "HEAD=1" in result
        assert "SITE=1" in result


# ---------------------------------------------------------------------------
# filtering
# ---------------------------------------------------------------------------

class TestInspectFilter:

    def test_record_type_filter_includes(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p)
        result = inspect_stdf(p, record_types=["PTR"], output_format="text")
        assert "PTR" in result

    def test_record_type_filter_excludes(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p)
        result = inspect_stdf(p, record_types=["PTR"], output_format="text")
        assert "MIR" not in result
        assert "PRR" not in result

    def test_record_type_filter_multiple(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p)
        result = inspect_stdf(p, record_types=["MIR", "MRR"], output_format="text")
        assert "MIR" in result
        assert "MRR" in result
        assert "PTR" not in result
        assert "PIR" not in result

    def test_limit(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p, part_ids=["A", "B", "C", "D", "E"])
        result = inspect_stdf(p, limit=3, output_format="text")
        assert "limited to 3" in result

    def test_head_filter(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p)
        result = inspect_stdf(p, head_filter=1, output_format="text")
        # Records with HEAD_NUM=1 shown
        assert "HEAD=1" in result

    def test_part_filter_first_block(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p, part_ids=["SN001", "SN002", "SN003"])
        result = inspect_stdf(p, part_filter=1, output_format="text")
        assert "SN001" in result
        # SN002 belongs to block 2, should not appear
        assert "SN002" not in result

    def test_part_filter_second_block(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p, part_ids=["SN001", "SN002", "SN003"])
        result = inspect_stdf(p, part_filter=2, output_format="text")
        assert "SN002" in result
        assert "SN001" not in result
        assert "SN003" not in result


# ---------------------------------------------------------------------------
# summary output
# ---------------------------------------------------------------------------

class TestInspectSummary:

    def test_returns_string(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p)
        result = inspect_stdf(p, output_format="summary")
        assert isinstance(result, str)

    def test_contains_record_names(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p)
        result = inspect_stdf(p, output_format="summary")
        for name in ("FAR", "MIR", "PIR", "PTR", "PRR", "PCR", "MRR"):
            assert name in result

    def test_contains_counts(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p, part_ids=["A", "B", "C"])
        result = inspect_stdf(p, output_format="summary")
        # 3 PIRs, 3 PRRs, 3 PTRs
        assert "3" in result

    def test_contains_total(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p)
        result = inspect_stdf(p, output_format="summary")
        assert "TOTAL" in result

    def test_contains_offsets(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p)
        result = inspect_stdf(p, output_format="summary")
        assert "offset" in result


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------

class TestInspectJson:

    def test_returns_valid_json(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p)
        result = inspect_stdf(p, output_format="json")
        data = json.loads(result)
        assert isinstance(data, list)

    def test_json_has_record_field(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p)
        data = json.loads(inspect_stdf(p, output_format="json"))
        assert data[0]["record"] == "FAR"

    def test_json_has_offset(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p)
        data = json.loads(inspect_stdf(p, output_format="json"))
        assert data[0]["offset"] == 0  # FAR at start

    def test_json_has_fields(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p)
        data = json.loads(inspect_stdf(p, output_format="json"))
        far = next(r for r in data if r["record"] == "FAR")
        assert "CPU_TYPE" in far["fields"]
        assert far["fields"]["CPU_TYPE"] == 2

    def test_json_lot_id(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p, lot_id="MYJSON_LOT")
        data = json.loads(inspect_stdf(p, output_format="json"))
        mir = next(r for r in data if r["record"] == "MIR")
        assert mir["fields"]["LOT_ID"] == "MYJSON_LOT"

    def test_json_limit(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p)
        data = json.loads(inspect_stdf(p, output_format="json", limit=2))
        assert len(data) == 2

    def test_json_record_type_filter(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p)
        data = json.loads(inspect_stdf(p, record_types=["PTR"], output_format="json"))
        assert all(r["record"] == "PTR" for r in data)

    def test_json_seq_numbers(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p)
        data = json.loads(inspect_stdf(p, output_format="json"))
        seqs = [r["seq"] for r in data]
        assert seqs == sorted(seqs)
        assert seqs[0] == 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestInspectCLI:

    def test_cli_default_text(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p)
        runner = CliRunner()
        r = runner.invoke(main, ["inspect", p])
        assert r.exit_code == 0, r.output
        assert "FAR" in r.output
        assert "MIR" in r.output

    def test_cli_summary(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p)
        runner = CliRunner()
        r = runner.invoke(main, ["inspect", p, "--summary"])
        assert r.exit_code == 0, r.output
        assert "TOTAL" in r.output

    def test_cli_json(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p)
        runner = CliRunner()
        r = runner.invoke(main, ["inspect", p, "--json"])
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)
        assert isinstance(data, list)

    def test_cli_record_filter(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p)
        runner = CliRunner()
        r = runner.invoke(main, ["inspect", p, "-r", "MIR"])
        assert r.exit_code == 0, r.output
        assert "MIR" in r.output
        assert "PIR" not in r.output

    def test_cli_limit(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p)
        runner = CliRunner()
        r = runner.invoke(main, ["inspect", p, "-n", "2"])
        assert r.exit_code == 0, r.output
        assert "limited to 2" in r.output

    def test_cli_output_file(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        out = str(tmp_path / "dump.txt")
        _make_stdf(p)
        runner = CliRunner()
        r = runner.invoke(main, ["inspect", p, "-o", out])
        assert r.exit_code == 0, r.output
        assert Path(out).exists()
        content = Path(out).read_text(encoding="utf-8")
        assert "FAR" in content
        assert "MIR" in content

    def test_cli_verbose(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p)
        runner = CliRunner()
        r = runner.invoke(main, ["inspect", p, "--verbose", "--summary"])
        assert r.exit_code == 0, r.output

    def test_cli_part_filter(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p, part_ids=["AAA", "BBB", "CCC"])
        runner = CliRunner()
        r = runner.invoke(main, ["inspect", p, "--part", "1"])
        assert r.exit_code == 0, r.output
        assert "AAA" in r.output
        assert "BBB" not in r.output

    def test_cli_multiple_record_types(self, tmp_path):
        p = str(tmp_path / "f.stdf")
        _make_stdf(p)
        runner = CliRunner()
        r = runner.invoke(main, ["inspect", p, "-r", "MIR", "-r", "MRR"])
        assert r.exit_code == 0, r.output
        assert "MIR" in r.output
        assert "MRR" in r.output
        assert "PIR" not in r.output
