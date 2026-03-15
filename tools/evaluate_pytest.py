#!/usr/bin/env python3
"""Evaluator for Python projects with four fixed indicators.

Fixed indicators:
- Test Pass Rate (%)
- Coverage (%)
- Lint Errors (count)
- Type Errors (count)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass

METRIC_TEST_PASS_RATE = "test_pass_rate"
METRIC_COVERAGE = "coverage"
METRIC_LINT_ERRORS = "lint_errors"
METRIC_TYPE_ERRORS = "type_errors"

ALL_METRICS = [
    METRIC_TEST_PASS_RATE,
    METRIC_COVERAGE,
    METRIC_LINT_ERRORS,
    METRIC_TYPE_ERRORS,
]

METRIC_LABELS = {
    METRIC_TEST_PASS_RATE: "Test Pass Rate",
    METRIC_COVERAGE: "Coverage",
    METRIC_LINT_ERRORS: "Lint Errors",
    METRIC_TYPE_ERRORS: "Type Errors",
}

METRIC_WEIGHTS = {
    METRIC_TEST_PASS_RATE: 0.5,
    METRIC_COVERAGE: 0.3,
    METRIC_LINT_ERRORS: 2.0,
    METRIC_TYPE_ERRORS: 2.0,
}


def normalize_metric_name(raw: str) -> str:
    name = raw.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "test_pass_rate": METRIC_TEST_PASS_RATE,
        "testpassrate": METRIC_TEST_PASS_RATE,
        "pass_rate": METRIC_TEST_PASS_RATE,
        "coverage": METRIC_COVERAGE,
        "lint": METRIC_LINT_ERRORS,
        "lint_errors": METRIC_LINT_ERRORS,
        "type": METRIC_TYPE_ERRORS,
        "type_errors": METRIC_TYPE_ERRORS,
    }
    if name in aliases:
        return aliases[name]
    raise ValueError(
        f"Unknown metric: {raw}. Allowed: {', '.join(ALL_METRICS)}"
    )


def parse_selected_metrics(raw: str) -> list[str]:
    text = (raw or "").strip()
    if not text or text.lower() == "all":
        return list(ALL_METRICS)

    selected: list[str] = []
    seen: set[str] = set()
    for part in text.split(","):
        metric = normalize_metric_name(part)
        if metric in seen:
            continue
        selected.append(metric)
        seen.add(metric)

    if not selected:
        raise ValueError("No metrics selected")
    return selected


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def run(command: str) -> CommandResult:
    proc = subprocess.run(command, shell=True, text=True, capture_output=True)
    return CommandResult(proc.returncode, proc.stdout or "", proc.stderr or "")


def combine_output(result: CommandResult) -> str:
    return f"{result.stdout}\n{result.stderr}".strip()


def parse_pytest_counts(output: str) -> tuple[int, int, int]:
    passed = failed = errors = 0
    m = re.search(r"(\d+)\s+passed", output)
    if m:
        passed = int(m.group(1))
    m = re.search(r"(\d+)\s+failed", output)
    if m:
        failed = int(m.group(1))
    m = re.search(r"(\d+)\s+error", output)
    if m:
        errors = int(m.group(1))
    return passed, failed, errors


def parse_coverage_percent(output: str) -> float:
    for line in output.splitlines():
        if "TOTAL" not in line:
            continue
        m = re.search(r"(\d+(?:\.\d+)?)%", line)
        if m:
            return float(m.group(1))
    m = re.search(r"coverage[^0-9]*(\d+(?:\.\d+)?)%", output, flags=re.IGNORECASE)
    if m:
        return float(m.group(1))
    return 0.0


def parse_ruff_errors(output: str, returncode: int) -> int:
    if returncode == 0:
        return 0
    matches = [ln for ln in output.splitlines() if re.search(r":\d+:\d+:", ln)]
    if matches:
        return len(matches)
    return len([ln for ln in output.splitlines() if ln.strip()])


def parse_mypy_errors(output: str, returncode: int) -> int:
    if returncode == 0:
        return 0
    matches = [ln for ln in output.splitlines() if ": error:" in ln]
    if matches:
        return len(matches)
    return len([ln for ln in output.splitlines() if ln.strip()])


def to_pass_rate(passed: int, failed: int, errors: int) -> float:
    total = passed + failed + errors
    if total == 0:
        return 0.0
    return (passed / total) * 100.0


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate project health with selectable metrics")
    parser.add_argument(
        "--use-metrics",
        default=os.getenv("AUTODEV_METRICS", "all"),
        help=(
            "Comma-separated metric keys to use for score. "
            "Allowed: test_pass_rate, coverage, lint_errors, type_errors. "
            "Use 'all' for all metrics."
        ),
    )
    parser.add_argument(
        "--list-metrics",
        action="store_true",
        help="List available metrics and exit",
    )
    args = parser.parse_args()

    if args.list_metrics:
        print(json.dumps({"available_metrics": ALL_METRICS}, ensure_ascii=True))
        return 0

    selected_metrics = parse_selected_metrics(args.use_metrics)
    selected_set = set(selected_metrics)

    python_available = shutil.which("python3") is not None

    test_pass_rate = 0.0
    coverage = 0.0
    lint_errors = 0
    type_errors = 0
    notes: list[str] = []

    if not python_available:
        if METRIC_TEST_PASS_RATE in selected_set:
            notes.append("python3 missing for pytest")
        if METRIC_COVERAGE in selected_set:
            notes.append("python3 missing for coverage")
        if METRIC_LINT_ERRORS in selected_set:
            lint_errors = 1
            notes.append("python3 missing for ruff")
        if METRIC_TYPE_ERRORS in selected_set:
            type_errors = 1
            notes.append("python3 missing for mypy")
    else:
        if METRIC_TEST_PASS_RATE in selected_set:
            pytest_result = run("python3 -m pytest -q")
            pytest_output = combine_output(pytest_result)
            passed, failed, errors = parse_pytest_counts(pytest_output)
            test_pass_rate = to_pass_rate(passed, failed, errors)
            if pytest_result.returncode not in (0, 1):
                notes.append("pytest command failed to run cleanly")

        if METRIC_COVERAGE in selected_set:
            coverage_result = run("python3 -m pytest --cov=. --cov-report=term -q")
            coverage_output = combine_output(coverage_result)
            if coverage_result.returncode in (0, 1):
                coverage = parse_coverage_percent(coverage_output)
            else:
                notes.append("coverage command failed; fallback to 0")

        if METRIC_LINT_ERRORS in selected_set:
            ruff_result = run("python3 -m ruff check .")
            lint_errors = parse_ruff_errors(combine_output(ruff_result), ruff_result.returncode)

        if METRIC_TYPE_ERRORS in selected_set:
            mypy_result = run("python3 -m mypy .")
            type_errors = parse_mypy_errors(combine_output(mypy_result), mypy_result.returncode)

    metric_values = {
        METRIC_TEST_PASS_RATE: round(test_pass_rate, 4),
        METRIC_COVERAGE: round(coverage, 4),
        METRIC_LINT_ERRORS: int(lint_errors),
        METRIC_TYPE_ERRORS: int(type_errors),
    }

    # Weighted scalar score for keep/discard decision, only using selected metrics.
    score = 0.0
    for metric in selected_metrics:
        value = float(metric_values[metric])
        weight = METRIC_WEIGHTS[metric]
        if metric in (METRIC_LINT_ERRORS, METRIC_TYPE_ERRORS):
            score -= value * weight
        else:
            score += value * weight

    fixed_metrics = {
        METRIC_LABELS[METRIC_TEST_PASS_RATE]: metric_values[METRIC_TEST_PASS_RATE],
        METRIC_LABELS[METRIC_COVERAGE]: metric_values[METRIC_COVERAGE],
        METRIC_LABELS[METRIC_LINT_ERRORS]: metric_values[METRIC_LINT_ERRORS],
        METRIC_LABELS[METRIC_TYPE_ERRORS]: metric_values[METRIC_TYPE_ERRORS],
    }

    summary_bits = [f"selected_metrics={','.join(selected_metrics)}"]
    for metric in selected_metrics:
        label = METRIC_LABELS[metric]
        value = fixed_metrics[label]
        if metric in (METRIC_TEST_PASS_RATE, METRIC_COVERAGE):
            summary_bits.append(f"{label}={value}%")
        else:
            summary_bits.append(f"{label}={value}")
    if notes:
        summary_bits.append("notes=" + "; ".join(notes))

    out = {
        "score": round(float(score), 6),
        "summary": ", ".join(summary_bits),
        "metrics": {
            METRIC_TEST_PASS_RATE: metric_values[METRIC_TEST_PASS_RATE],
            METRIC_COVERAGE: metric_values[METRIC_COVERAGE],
            METRIC_LINT_ERRORS: metric_values[METRIC_LINT_ERRORS],
            METRIC_TYPE_ERRORS: metric_values[METRIC_TYPE_ERRORS],
        },
        "fixed_metrics": fixed_metrics,
        "selected_metrics": selected_metrics,
    }
    print(json.dumps(out, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
