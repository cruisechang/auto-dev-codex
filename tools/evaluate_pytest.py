#!/usr/bin/env python3
"""Simple evaluator for Python projects.

Outputs JSON with a scalar score:
- score = tests_passed * 10 - tests_failed * 20 - test_errors * 20 - lint_errors * 2

The script is intentionally simple and can be replaced by your own evaluator.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def run(command: str) -> CommandResult:
    proc = subprocess.run(command, shell=True, text=True, capture_output=True)
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def parse_pytest_counts(output: str) -> tuple[int, int, int]:
    # Examples:
    # "10 passed in 0.23s"
    # "2 failed, 8 passed in 1.23s"
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


def parse_ruff_errors(output: str, returncode: int) -> int:
    if returncode == 0:
        return 0
    # Best-effort count: each non-empty line likely corresponds to one issue.
    lines = [ln for ln in output.splitlines() if ln.strip()]
    return len(lines)


def main() -> int:
    pytest_cmd = "python3 -m pytest -q"
    ruff_cmd = "python3 -m ruff check ."

    pytest_available = shutil.which("python3") is not None
    ruff_available = shutil.which("python3") is not None

    tests_passed = tests_failed = test_errors = 0
    lint_errors = 0
    notes: list[str] = []

    if pytest_available:
        tr = run(pytest_cmd)
        combined = (tr.stdout or "") + "\n" + (tr.stderr or "")
        tests_passed, tests_failed, test_errors = parse_pytest_counts(combined)
        if tr.returncode not in (0, 1):
            # treat infra/tool failures as one hard error
            test_errors += 1
            notes.append("pytest command failed to run cleanly")
    else:
        test_errors += 1
        notes.append("python3 missing")

    if ruff_available:
        rr = run(ruff_cmd)
        combined = (rr.stdout or "") + "\n" + (rr.stderr or "")
        lint_errors = parse_ruff_errors(combined, rr.returncode)
    else:
        lint_errors += 1
        notes.append("python3 missing for ruff")

    score = tests_passed * 10 - tests_failed * 20 - test_errors * 20 - lint_errors * 2

    summary_bits = [
        f"tests_passed={tests_passed}",
        f"tests_failed={tests_failed}",
        f"test_errors={test_errors}",
        f"lint_errors={lint_errors}",
    ]
    if notes:
        summary_bits.append("notes=" + "; ".join(notes))

    out = {
        "score": float(score),
        "summary": ", ".join(summary_bits),
        "metrics": {
            "tests_passed": tests_passed,
            "tests_failed": tests_failed,
            "test_errors": test_errors,
            "lint_errors": lint_errors,
        },
    }
    print(json.dumps(out, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
