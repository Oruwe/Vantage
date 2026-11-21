"""Regression test for scripts/pilot_report.py."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


_PILOT_REPORT = Path(__file__).resolve().parents[1] / "scripts" / "pilot_report.py"


def _load_module():
    if "pilot_report" in sys.modules:
        return sys.modules["pilot_report"]
    spec = importlib.util.spec_from_file_location("pilot_report", _PILOT_REPORT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclasses on 3.14 looks up the module in sys.modules; register first.
    sys.modules["pilot_report"] = module
    spec.loader.exec_module(module)
    return module


def _stats_json(approved, rejected, edited, scrubber_total, scrubber_by_kind):
    windows = []
    for i, (a, r, e) in enumerate(zip(approved, rejected, edited, strict=True)):
        windows.append({
            "week_start": f"2026-11-{9 + 7 * i:02d}",
            "week_end": f"2026-11-{16 + 7 * i:02d}",
            "drafts_created": a + r,
            "approved": a,
            "rejected": r,
            "approved_with_edit": e,
            "edit_rate": (e / a) if a else None,
            "approval_rate": (a / (a + r)) if (a + r) else None,
        })
    tot_a, tot_r, tot_e = sum(approved), sum(rejected), sum(edited)
    return {
        "generated_at": "2026-12-02T10:00:00",
        "weeks": len(approved),
        "pending": 0,
        "totals": {
            "drafts_created": tot_a + tot_r,
            "approved": tot_a,
            "rejected": tot_r,
            "approved_with_edit": tot_e,
            "approval_rate": tot_a / (tot_a + tot_r) if (tot_a + tot_r) else None,
            "edit_rate": tot_e / tot_a if tot_a else None,
        },
        "scrubber_hits": {"total": scrubber_total, "by_kind": scrubber_by_kind},
        "windows": windows,
    }


@pytest.fixture
def pilot_dir(tmp_path):
    in_dir = tmp_path / "pilot-data"
    in_dir.mkdir()

    # P1: strong approval, edit-rate drops across windows (H1+H2 pass for P1)
    (in_dir / "pilot-stats-P1.json").write_text(json.dumps(_stats_json(
        approved=[4, 5, 6], rejected=[1, 1, 0], edited=[4, 3, 2],
        scrubber_total=3, scrubber_by_kind={"aws_access_key": 2, "github_token": 1},
    )), encoding="utf-8")

    # P2: weak approval, edit-rate rises (both miss)
    (in_dir / "pilot-stats-P2.json").write_text(json.dumps(_stats_json(
        approved=[2, 2, 1], rejected=[1, 2, 2], edited=[0, 1, 1],
        scrubber_total=1, scrubber_by_kind={"anthropic_key": 1},
    )), encoding="utf-8")

    survey = in_dir / "survey.csv"
    survey.write_text(
        "person,minutes_saved,keep_using,felt_watched,draft_quality\n"
        "P1,8,yes,1,4\n"
        "P2,2,no,3,2\n",
        encoding="utf-8",
    )
    return in_dir, survey


def test_pilot_report_writes_csv_md_and_summary(tmp_path, pilot_dir, capsys):
    pilot_in, survey = pilot_dir
    out_dir = tmp_path / "pilot-results"

    mod = _load_module()
    rc = mod.main(["--input", str(pilot_in), "--out", str(out_dir),
                   "--survey", str(survey)])
    assert rc == 0

    csv_path = out_dir / "pilot-results.csv"
    md_path = out_dir / "pilot-results.md"
    assert csv_path.exists()
    assert md_path.exists()

    csv_text = csv_path.read_text(encoding="utf-8")
    # 1 header + 6 data rows (2 people x 3 windows)
    assert len(csv_text.strip().splitlines()) == 7
    assert "P1,2026-11-16" in csv_text
    assert "P2,2026-11-30" in csv_text

    md = md_path.read_text(encoding="utf-8")
    # H1: pooled 74% (16/22), 1/2 >= 70% -> PASS
    assert "H1" in md and "PASS" in md
    # H3: 4 total redactions -> PASS
    assert "4 total redactions" in md
    # Scrubber breakdown pools across people
    assert "`aws_access_key`" in md and "`github_token`" in md and "`anthropic_key`" in md
    # Survey columns rendered
    assert "minutes_saved" in md
    assert "felt_watched" in md


def test_pilot_report_errors_on_empty_input(tmp_path):
    mod = _load_module()
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SystemExit) as exc:
        mod.main(["--input", str(empty), "--out", str(tmp_path / "out")])
    assert "pilot-stats-*.json" in str(exc.value)


def test_pilot_report_handles_missing_survey_fields(tmp_path):
    in_dir = tmp_path / "pilot-data"
    in_dir.mkdir()
    (in_dir / "pilot-stats-P1.json").write_text(json.dumps(_stats_json(
        approved=[3], rejected=[0], edited=[1],
        scrubber_total=0, scrubber_by_kind={},
    )), encoding="utf-8")

    mod = _load_module()
    rc = mod.main(["--input", str(in_dir), "--out", str(tmp_path / "out")])
    assert rc == 0

    md = (tmp_path / "out" / "pilot-results.md").read_text(encoding="utf-8")
    # H4 falls back when no survey provided
    assert "no survey data provided" in md
    assert "H3" in md
    # Scrubber section renders empty case
    assert "No redactions recorded" in md
