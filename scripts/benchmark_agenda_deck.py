"""CLI entrypoint for the Offline Agenda Deck Benchmark Harness & Log Analyzer.

Usage examples:
    python scripts/benchmark_agenda_deck.py --log-dir logs --split-runs
    python scripts/benchmark_agenda_deck.py --run v1=logs/v1 --run v2=logs/v2 --md-out report.md --html-out report.html
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from opencohost.tools.agenda_benchmark import (  # noqa: E402
    RunAnalysis,
    analyze_log_dir,
    compare_runs,
    render_cli_table,
    render_html_report,
    render_json_report,
    render_markdown_report,
)


def _parse_run_spec(spec: str) -> tuple[str, Optional[str], Path]:
    """Parse ``label=path`` or ``label:variant=path`` into (label, variant, Path)."""
    if "=" not in spec:
        p = Path(spec)
        return p.name, None, p
    lhs, rhs = spec.split("=", 1)
    if ":" in lhs:
        label, variant = lhs.split(":", 1)
        return label.strip(), variant.strip() or None, Path(rhs.strip())
    label = lhs.strip()
    inferred_variant = label if label in {"off", "v1", "v2", "v3", "v4", "v5"} else None
    return label, inferred_variant, Path(rhs.strip())


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Offline Agenda Deck Benchmark Harness & Log Analyzer (tid-joined, zero cloud calls).",
    )
    p.add_argument(
        "--log-dir",
        type=Path,
        default=None,
        help="Analyze a single OpenCohost log directory (contains turn_events.jsonl, prompt_trace/, backend.log).",
    )
    p.add_argument(
        "--run",
        action="append",
        default=[],
        help="Add a labeled run as label=dir or label:variant=dir (repeatable for side-by-side comparison).",
    )
    p.add_argument(
        "--split-runs",
        action="store_true",
        help="Auto-split a single combined log directory into separate runs on model change or >180s gap.",
    )
    p.add_argument(
        "--date",
        action="append",
        default=None,
        help="Filter prompt_trace YYYYMMDD.jsonl files (repeatable).",
    )
    p.add_argument(
        "--format",
        choices=("table", "markdown", "json", "html"),
        default="table",
        help="Stdout output format (default: table).",
    )
    p.add_argument("--json-out", type=Path, default=None, help="Optional path to write JSON report.")
    p.add_argument("--md-out", type=Path, default=None, help="Optional path to write Markdown report.")
    p.add_argument("--html-out", type=Path, default=None, help="Optional path to write self-contained HTML report.")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    dates = set(args.date) if args.date else None

    runs: list[RunAnalysis] = []
    if args.run:
        for spec in args.run:
            label, variant, path = _parse_run_spec(spec)
            runs.extend(
                analyze_log_dir(
                    path,
                    label=label,
                    variant=variant,
                    dates=dates,
                    split_runs=args.split_runs,
                )
            )
    elif args.log_dir is not None:
        runs.extend(
            analyze_log_dir(
                args.log_dir,
                dates=dates,
                split_runs=args.split_runs,
            )
        )
    else:
        default_logs = _REPO_ROOT / "logs"
        runs.extend(
            analyze_log_dir(
                default_logs,
                dates=dates,
                split_runs=True,
            )
        )

    report = compare_runs(runs)

    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(render_json_report(report), encoding="utf-8")
    if args.md_out is not None:
        args.md_out.parent.mkdir(parents=True, exist_ok=True)
        args.md_out.write_text(render_markdown_report(report), encoding="utf-8")
    if args.html_out is not None:
        args.html_out.parent.mkdir(parents=True, exist_ok=True)
        args.html_out.write_text(render_html_report(report), encoding="utf-8")

    renderers = {
        "table": render_cli_table,
        "markdown": render_markdown_report,
        "json": render_json_report,
        "html": render_html_report,
    }
    output = renderers[args.format](report)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
