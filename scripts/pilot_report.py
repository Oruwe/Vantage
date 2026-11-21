"""Aggregate `vantage-ghost stats --json` files from a pilot into a results pack.

Reads every ``pilot-stats-*.json`` from an input directory, normalizes the
per-person counts, and writes:

- ``pilot-results.md``   - H1-H4 evaluation + per-person tables for the pitch.
- ``pilot-results.csv``  - one row per person per window, easy to open in Sheets.
- ``pilot-edit-rate.png`` (optional, if matplotlib is installed)
  - per-person edit-rate-per-window line chart that visualises H2.

Survey answers are optional: pass ``--survey survey.csv`` with columns
``person,minutes_saved,keep_using,felt_watched,draft_quality`` to merge them
into the overall table and to evaluate H4.

Filename convention: files should be named ``pilot-stats-<person>.json``
(e.g. ``pilot-stats-P1.json``). Anything else is handled too - the stem after
``pilot-stats-`` is used verbatim.

Usage:
    python scripts/pilot_report.py --input ./pilot-data --out ./pilot-results
    python scripts/pilot_report.py --input ./pilot-data --survey survey.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class WindowRow:
    week_end: str
    drafts_created: int
    approved: int
    rejected: int
    approved_with_edit: int

    @property
    def edit_rate(self) -> float | None:
        return (self.approved_with_edit / self.approved) if self.approved else None

    @property
    def approval_rate(self) -> float | None:
        reviewed = self.approved + self.rejected
        return (self.approved / reviewed) if reviewed else None


@dataclass
class PersonStats:
    person: str
    windows: list[WindowRow]
    pending: int
    totals_approved: int
    totals_rejected: int
    totals_with_edit: int
    scrubber_total: int
    scrubber_by_kind: dict[str, int]

    # Survey (optional)
    minutes_saved: float | None = None
    keep_using: str | None = None
    felt_watched: int | None = None
    draft_quality: int | None = None

    @property
    def total_reviewed(self) -> int:
        return self.totals_approved + self.totals_rejected

    @property
    def approval_rate(self) -> float | None:
        return (self.totals_approved / self.total_reviewed) if self.total_reviewed else None

    @property
    def edit_rate_first(self) -> float | None:
        for w in self.windows:
            if w.edit_rate is not None:
                return w.edit_rate
        return None

    @property
    def edit_rate_last(self) -> float | None:
        for w in reversed(self.windows):
            if w.edit_rate is not None:
                return w.edit_rate
        return None


@dataclass
class Report:
    people: list[PersonStats] = field(default_factory=list)

    @property
    def pooled_approved(self) -> int:
        return sum(p.totals_approved for p in self.people)

    @property
    def pooled_rejected(self) -> int:
        return sum(p.totals_rejected for p in self.people)

    @property
    def pooled_approval_rate(self) -> float | None:
        reviewed = self.pooled_approved + self.pooled_rejected
        return (self.pooled_approved / reviewed) if reviewed else None

    @property
    def pooled_scrubber_total(self) -> int:
        return sum(p.scrubber_total for p in self.people)


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def _person_from_filename(path: Path) -> str:
    stem = path.stem
    prefix = "pilot-stats-"
    return stem[len(prefix):] if stem.startswith(prefix) else stem


def load_stats_file(path: Path) -> PersonStats:
    data = json.loads(path.read_text(encoding="utf-8"))
    totals = data.get("totals", {})
    scrubber = data.get("scrubber_hits", {})
    windows = [
        WindowRow(
            week_end=w["week_end"],
            drafts_created=int(w.get("drafts_created", 0)),
            approved=int(w.get("approved", 0)),
            rejected=int(w.get("rejected", 0)),
            approved_with_edit=int(w.get("approved_with_edit", 0)),
        )
        for w in data.get("windows", [])
    ]
    return PersonStats(
        person=_person_from_filename(path),
        windows=windows,
        pending=int(data.get("pending", 0)),
        totals_approved=int(totals.get("approved", 0)),
        totals_rejected=int(totals.get("rejected", 0)),
        totals_with_edit=int(totals.get("approved_with_edit", 0)),
        scrubber_total=int(scrubber.get("total", 0)),
        scrubber_by_kind=dict(scrubber.get("by_kind", {})),
    )


def load_survey(path: Path, by_person: dict[str, PersonStats]) -> None:
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            person = (row.get("person") or "").strip()
            if not person or person not in by_person:
                continue
            p = by_person[person]
            p.minutes_saved = _float_or_none(row.get("minutes_saved"))
            p.keep_using = (row.get("keep_using") or "").strip() or None
            p.felt_watched = _int_or_none(row.get("felt_watched"))
            p.draft_quality = _int_or_none(row.get("draft_quality"))


def _float_or_none(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def load_report(input_dir: Path, survey_csv: Path | None) -> Report:
    files = sorted(input_dir.glob("pilot-stats-*.json"))
    if not files:
        raise SystemExit(
            f"No files matching pilot-stats-*.json in {input_dir}. "
            "Collect them with: uv run vantage-ghost stats --weeks 3 --json > "
            "pilot-stats-<P>.json"
        )
    people = [load_stats_file(f) for f in files]
    people.sort(key=lambda p: p.person)

    if survey_csv is not None:
        if not survey_csv.exists():
            raise SystemExit(f"Survey file not found: {survey_csv}")
        load_survey(survey_csv, {p.person: p for p in people})

    return Report(people=people)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _fmt_pct(x: float | None) -> str:
    return f"{x:.0%}" if x is not None else "-"


def _fmt_int(x: int | None) -> str:
    return str(x) if x is not None else "-"


def _fmt_num(x: float | None) -> str:
    if x is None:
        return "-"
    return f"{x:.1f}" if isinstance(x, float) and not x.is_integer() else f"{int(x)}"


def write_csv(report: Report, path: Path) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "person", "window_end", "drafts_created", "approved", "rejected",
            "approved_with_edit", "edit_rate", "approval_rate",
        ])
        for p in report.people:
            for w in p.windows:
                writer.writerow([
                    p.person, w.week_end, w.drafts_created, w.approved, w.rejected,
                    w.approved_with_edit,
                    "" if w.edit_rate is None else f"{w.edit_rate:.4f}",
                    "" if w.approval_rate is None else f"{w.approval_rate:.4f}",
                ])


def _evaluate_hypotheses(report: Report) -> list[tuple[str, str, str]]:
    # Returns (claim, measured_value, verdict) rows.
    rows: list[tuple[str, str, str]] = []

    # H1 - approval rate >= 70% pooled AND for a majority of people.
    pooled = report.pooled_approval_rate
    per_person_ok = sum(
        1 for p in report.people
        if p.approval_rate is not None and p.approval_rate >= 0.70
    )
    reviewing = sum(1 for p in report.people if p.approval_rate is not None)
    h1_detail = (
        f"pooled {_fmt_pct(pooled)}; "
        f"{per_person_ok}/{reviewing} people >= 70%"
    )
    h1_pass = (
        pooled is not None and pooled >= 0.70
        and reviewing > 0 and per_person_ok * 2 >= reviewing
    )
    rows.append(("H1 - drafts are useful (approval rate >= 70%)",
                 h1_detail, _verdict(h1_pass)))

    # H2 - edit rate decreases from first to last window for a majority.
    improved = 0
    compared = 0
    for p in report.people:
        f_, l_ = p.edit_rate_first, p.edit_rate_last
        if f_ is None or l_ is None:
            continue
        compared += 1
        if l_ < f_:
            improved += 1
    h2_detail = f"{improved}/{compared} people edited less in the last window than the first"
    h2_pass = compared > 0 and improved * 2 >= compared
    rows.append(("H2 - drafts learn your voice (edit rate drops)",
                 h2_detail, _verdict(h2_pass)))

    # H3 - scrubber caught >= 1 real redaction across the pilot.
    h3_detail = f"{report.pooled_scrubber_total} total redactions across {len(report.people)} people"
    h3_pass = report.pooled_scrubber_total >= 1
    rows.append(("H3 - scrubber matters on real work",
                 h3_detail, _verdict(h3_pass)))

    # H4 - median felt-watched <= 2 (only if survey data provided).
    watched = [p.felt_watched for p in report.people if p.felt_watched is not None]
    if watched:
        med = statistics.median(watched)
        h4_detail = f"median felt-watched = {med:g} ({len(watched)} responses)"
        h4_pass = med <= 2
        rows.append(("H4 - doesn't feel like surveillance",
                     h4_detail, _verdict(h4_pass)))
    else:
        rows.append(("H4 - doesn't feel like surveillance",
                     "no survey data provided", "n/a"))

    return rows


def _verdict(passed: bool) -> str:
    return "PASS" if passed else "MISS"


def write_markdown(report: Report, path: Path, chart_path: Path | None) -> None:
    lines: list[str] = []
    lines.append("# Vantage pilot - results")
    lines.append("")
    lines.append(
        f"_Generated from {len(report.people)} participant file(s). "
        "Counts only; no draft content is included._"
    )
    lines.append("")

    # Hypothesis evaluation.
    lines.append("## Hypothesis evaluation")
    lines.append("")
    lines.append("| Claim | Measured | Verdict |")
    lines.append("|---|---|---|")
    for claim, detail, verdict in _evaluate_hypotheses(report):
        lines.append(f"| {claim} | {detail} | {verdict} |")
    lines.append("")

    # Per-person overall table.
    lines.append("## Per-person overall")
    lines.append("")
    has_survey = any(p.felt_watched is not None or p.minutes_saved is not None
                     for p in report.people)
    header = [
        "person", "approval_rate", "edit_rate first", "edit_rate last",
        "scrubber_total", "pending",
    ]
    if has_survey:
        header += ["minutes_saved", "keep_using", "felt_watched", "draft_quality"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    for p in report.people:
        row = [
            p.person,
            _fmt_pct(p.approval_rate),
            _fmt_pct(p.edit_rate_first),
            _fmt_pct(p.edit_rate_last),
            str(p.scrubber_total),
            str(p.pending),
        ]
        if has_survey:
            row += [
                _fmt_num(p.minutes_saved),
                p.keep_using or "-",
                _fmt_int(p.felt_watched),
                _fmt_int(p.draft_quality),
            ]
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # Per-person per-window table.
    lines.append("## Per-person per-window")
    lines.append("")
    lines.append(
        "| person | window_end | drafts_created | approved | rejected | "
        "approved_with_edit | edit_rate | approval_rate |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for p in report.people:
        for w in p.windows:
            lines.append(
                f"| {p.person} | {w.week_end} | {w.drafts_created} | {w.approved} | "
                f"{w.rejected} | {w.approved_with_edit} | "
                f"{_fmt_pct(w.edit_rate)} | {_fmt_pct(w.approval_rate)} |"
            )
    lines.append("")

    # Scrubber breakdown.
    lines.append("## Scrubber hits by kind (pooled)")
    lines.append("")
    pooled_kinds: dict[str, int] = {}
    for p in report.people:
        for k, v in p.scrubber_by_kind.items():
            pooled_kinds[k] = pooled_kinds.get(k, 0) + v
    if not pooled_kinds:
        lines.append("_No redactions recorded across the pilot._")
    else:
        lines.append("| kind | count |")
        lines.append("|---|---|")
        for k, v in sorted(pooled_kinds.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"| `{k}` | {v} |")
    lines.append("")

    if chart_path is not None and chart_path.exists():
        lines.append("## Chart")
        lines.append("")
        lines.append(f"![edit rate per window]({chart_path.name})")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def try_write_chart(report: Report, path: Path) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:
        print(
            f"[skip] matplotlib not available ({exc.__class__.__name__}); "
            "chart not generated. Install with: uv pip install matplotlib",
            file=sys.stderr,
        )
        return False

    fig, ax = plt.subplots(figsize=(8, 4.5))
    plotted = 0
    for p in report.people:
        xs = [w.week_end for w in p.windows]
        ys = [w.edit_rate if w.edit_rate is not None else float("nan")
              for w in p.windows]
        if any(y == y for y in ys):  # at least one non-NaN
            ax.plot(xs, ys, marker="o", label=p.person)
            plotted += 1

    if plotted == 0:
        print("[skip] no approved drafts with edit-rate to plot.", file=sys.stderr)
        plt.close(fig)
        return False

    ax.set_title("Edit rate per weekly window (H2)")
    ax.set_xlabel("Window end")
    ax.set_ylabel("Edit rate")
    ax.set_ylim(0, 1.05)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _pos: f"{v:.0%}"))
    ax.grid(alpha=0.3)
    ax.legend(loc="best", fontsize=9)
    fig.autofmt_xdate(rotation=30)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return True


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Aggregate Vantage pilot stats files into a results pack.",
    )
    parser.add_argument(
        "--input", "-i", type=Path, default=Path("pilot-data"),
        help="Directory containing pilot-stats-*.json files (default: ./pilot-data)",
    )
    parser.add_argument(
        "--out", "-o", type=Path, default=Path("pilot-results"),
        help="Output directory (default: ./pilot-results)",
    )
    parser.add_argument(
        "--survey", type=Path, default=None,
        help="Optional CSV with columns person,minutes_saved,keep_using,"
             "felt_watched,draft_quality",
    )
    args = parser.parse_args(argv)

    if not args.input.exists() or not args.input.is_dir():
        raise SystemExit(f"Input directory not found: {args.input}")
    args.out.mkdir(parents=True, exist_ok=True)

    report = load_report(args.input, args.survey)

    csv_path = args.out / "pilot-results.csv"
    md_path = args.out / "pilot-results.md"
    chart_path = args.out / "pilot-edit-rate.png"

    write_csv(report, csv_path)
    chart_ok = try_write_chart(report, chart_path)
    write_markdown(report, md_path, chart_path if chart_ok else None)

    print(f"Wrote {csv_path}")
    print(f"Wrote {md_path}")
    if chart_ok:
        print(f"Wrote {chart_path}")
    print(f"{len(report.people)} participant file(s) processed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
