#!/usr/bin/env python3
"""Autonomous code-improvement loop for software projects.

This script mirrors the autoresearch idea for general software development:
- Ask an agent to modify code.
- Run evaluation checks and compute a scalar score.
- Keep changes only when score improves.
- Otherwise discard and continue.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # Python 3.10 fallback


@dataclass
class RunConfig:
    name: str
    max_iterations: int
    score_epsilon: float
    keep_equal: bool
    patience: int
    branch: str | None


@dataclass
class AgentConfig:
    command: str
    timeout_seconds: int


@dataclass
class EvaluatorConfig:
    command: str
    timeout_seconds: int
    score_key: str
    higher_is_better: bool


@dataclass
class WorkspaceConfig:
    editable_paths: list[str]
    program_file: str
    artifacts_dir: str
    results_tsv: str


@dataclass
class Config:
    run: RunConfig
    agent: AgentConfig
    evaluator: EvaluatorConfig
    workspace: WorkspaceConfig


@dataclass
class CmdResult:
    returncode: int
    stdout: str
    stderr: str
    seconds: float


def load_config(path: Path) -> Config:
    with path.open("rb") as f:
        data = tomllib.load(f)

    run_data = data.get("run", {})
    agent_data = data.get("agent", {})
    evaluator_data = data.get("evaluator", {})
    workspace_data = data.get("workspace", {})

    cfg = Config(
        run=RunConfig(
            name=str(run_data.get("name", "autodev")),
            max_iterations=int(run_data.get("max_iterations", 40)),
            score_epsilon=float(run_data.get("score_epsilon", 1e-6)),
            keep_equal=bool(run_data.get("keep_equal", False)),
            patience=int(run_data.get("patience", 12)),
            branch=run_data.get("branch"),
        ),
        agent=AgentConfig(
            command=str(agent_data.get("command", "")),
            timeout_seconds=int(agent_data.get("timeout_seconds", 600)),
        ),
        evaluator=EvaluatorConfig(
            command=str(evaluator_data.get("command", "")),
            timeout_seconds=int(evaluator_data.get("timeout_seconds", 300)),
            score_key=str(evaluator_data.get("score_key", "score")),
            higher_is_better=bool(evaluator_data.get("higher_is_better", True)),
        ),
        workspace=WorkspaceConfig(
            editable_paths=[str(p) for p in workspace_data.get("editable_paths", ["."])],
            program_file=str(workspace_data.get("program_file", "program.md")),
            artifacts_dir=str(workspace_data.get("artifacts_dir", ".autodev")),
            results_tsv=str(workspace_data.get("results_tsv", "results.tsv")),
        ),
    )

    if not cfg.agent.command:
        raise ValueError("Missing [agent].command in config")
    if not cfg.evaluator.command:
        raise ValueError("Missing [evaluator].command in config")
    if cfg.run.max_iterations < 1:
        raise ValueError("run.max_iterations must be >= 1")
    if cfg.run.patience < 1:
        raise ValueError("run.patience must be >= 1")
    return cfg


def run_cmd(command: str, cwd: Path, timeout_seconds: int, output_file: Path | None = None) -> CmdResult:
    start = time.time()
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            shell=True,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
        result = CmdResult(
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            seconds=time.time() - start,
        )
    except subprocess.TimeoutExpired as exc:
        result = CmdResult(
            returncode=124,
            stdout=exc.stdout or "",
            stderr=(exc.stderr or "") + f"\n[autodev] timed out after {timeout_seconds}s",
            seconds=time.time() - start,
        )

    if output_file is not None:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        with output_file.open("w", encoding="utf-8") as f:
            f.write(result.stdout)
            if result.stderr:
                f.write("\n\n--- STDERR ---\n")
                f.write(result.stderr)

    return result


def git(cwd: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        capture_output=True,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def is_inside_git_repo(cwd: Path) -> bool:
    try:
        out = git(cwd, "rev-parse", "--is-inside-work-tree")
        return out == "true"
    except RuntimeError:
        return False


def ensure_clean_tracked(cwd: Path) -> None:
    staged = git(cwd, "diff", "--cached", "--name-only")
    unstaged = git(cwd, "diff", "--name-only")
    if staged or unstaged:
        raise RuntimeError(
            "Tracked changes detected. Please commit or stash them before running autodev."
        )


def checkout_branch(cwd: Path, branch: str) -> None:
    current = git(cwd, "branch", "--show-current")
    if current == branch:
        return

    exists_code = subprocess.run(
        ["git", "show-ref", "--verify", f"refs/heads/{branch}"],
        cwd=str(cwd),
        text=True,
        capture_output=True,
    ).returncode

    if exists_code == 0:
        git(cwd, "checkout", branch)
    else:
        git(cwd, "checkout", "-b", branch)


def list_untracked(cwd: Path) -> set[str]:
    out = git(cwd, "ls-files", "--others", "--exclude-standard")
    if not out:
        return set()
    return set(line.strip() for line in out.splitlines() if line.strip())


def list_changed(cwd: Path) -> list[str]:
    out = git(cwd, "status", "--porcelain")
    changed: list[str] = []
    for line in out.splitlines():
        if not line:
            continue
        # porcelain format: XY <path>
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        changed.append(path)
    return changed


def is_runtime_generated(path: str, artifacts_dir: str, results_tsv: str) -> bool:
    p = path.strip()
    p_no_slash = p.rstrip("/")
    art = artifacts_dir.strip("/").rstrip("/")
    res = results_tsv.strip("/").rstrip("/")

    if art and (p_no_slash == art or p_no_slash.startswith(art + "/")):
        return True
    if res and p_no_slash == res:
        return True
    if "__pycache__/" in p or p.endswith("__pycache__"):
        return True
    if p_no_slash.startswith(".pytest_cache") or p_no_slash.startswith(".ruff_cache"):
        return True
    if p_no_slash.endswith(".pyc"):
        return True
    return False


def is_allowed_path(path: str, editable_paths: list[str]) -> bool:
    normalized = path.strip("/")
    for allow in editable_paths:
        a = allow.strip("/")
        if a in {"", "."}:
            return True
        if normalized == a or normalized.startswith(a + "/"):
            return True
    return False


def restore_paths(cwd: Path, paths: list[str]) -> None:
    if not paths:
        return
    tracked: list[str] = []
    untracked: list[str] = []
    tracked_set = set(git(cwd, "ls-files").splitlines())
    for p in paths:
        if p in tracked_set:
            tracked.append(p)
        else:
            untracked.append(p)

    if tracked:
        subprocess.run(["git", "restore", "--staged", "--worktree", "--", *tracked], cwd=str(cwd), check=False)

    for p in untracked:
        target = cwd / p
        if target.is_file() or target.is_symlink():
            target.unlink(missing_ok=True)
        elif target.is_dir():
            shutil.rmtree(target, ignore_errors=True)


def sanitize_tsv(value: Any) -> str:
    return str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()


def append_result(path: Path, row: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with path.open("w", encoding="utf-8") as f:
            f.write("timestamp\titeration\tcommit\tscore\tstatus\tsummary\tchanged_files\n")
    with path.open("a", encoding="utf-8") as f:
        f.write("\t".join(sanitize_tsv(v) for v in row) + "\n")


def render_agent_prompt(program_text: str, best_score: float, iteration: int, history_tail: list[str]) -> str:
    history_block = "\n".join(history_tail[-10:]) if history_tail else "(no history yet)"
    return (
        f"{program_text}\n\n"
        "## Runtime Context (auto-generated)\n"
        f"- iteration: {iteration}\n"
        f"- best_score: {best_score:.8f}\n"
        "- instruction: modify code in-place in this repo, then exit.\n"
        "- instruction: focus on improving evaluator score with minimal complexity increase.\n"
        "- recent_results:\n"
        f"{history_block}\n"
    )


def parse_eval_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        raise ValueError("Evaluator produced empty output")

    # Common case: pure JSON output.
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    # Fallback: parse last line that looks like JSON object.
    for line in reversed(stripped.splitlines()):
        line = line.strip()
        if not line.startswith("{") or not line.endswith("}"):
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue

    raise ValueError("Cannot parse evaluator JSON output")


def improved(new_score: float, best_score: float, higher_is_better: bool, eps: float, keep_equal: bool) -> bool:
    if higher_is_better:
        delta = new_score - best_score
    else:
        delta = best_score - new_score

    if delta > eps:
        return True
    if keep_equal and math.fabs(delta) <= eps:
        return True
    return False


def maybe_commit_changes(cwd: Path, editable_paths: list[str], msg: str) -> str:
    args = ["add", "-A", "--", *editable_paths]
    git(cwd, *args)
    commit_proc = subprocess.run(
        ["git", "commit", "-m", msg],
        cwd=str(cwd),
        text=True,
        capture_output=True,
    )
    if commit_proc.returncode != 0:
        raise RuntimeError(f"git commit failed: {commit_proc.stderr.strip()}")
    return git(cwd, "rev-parse", "--short", "HEAD")


def main() -> int:
    parser = argparse.ArgumentParser(description="Autonomous code-improvement loop")
    parser.add_argument("--config", default="autodev.toml", help="Path to config TOML")
    parser.add_argument("--max-iterations", type=int, default=None, help="Override run.max_iterations")
    args = parser.parse_args()

    cwd = Path.cwd()
    config_path = cwd / args.config
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    cfg = load_config(config_path)
    if args.max_iterations is not None:
        cfg.run.max_iterations = args.max_iterations

    if not is_inside_git_repo(cwd):
        raise RuntimeError("autodev must run inside a git repository")

    ensure_clean_tracked(cwd)

    if cfg.run.branch:
        checkout_branch(cwd, cfg.run.branch)

    program_path = cwd / cfg.workspace.program_file
    if not program_path.exists():
        raise FileNotFoundError(f"program file not found: {program_path}")
    program_text = program_path.read_text(encoding="utf-8")

    artifacts_root = cwd / cfg.workspace.artifacts_dir
    runs_dir = artifacts_root / "runs"
    prompts_dir = artifacts_root / "prompts"
    results_path = cwd / cfg.workspace.results_tsv
    runs_dir.mkdir(parents=True, exist_ok=True)
    prompts_dir.mkdir(parents=True, exist_ok=True)

    history_tail: list[str] = []

    # Baseline from current HEAD.
    baseline_eval = run_cmd(
        cfg.evaluator.command,
        cwd=cwd,
        timeout_seconds=cfg.evaluator.timeout_seconds,
        output_file=runs_dir / "baseline_eval.log",
    )
    if baseline_eval.returncode != 0:
        raise RuntimeError(
            "Baseline evaluator failed. Fix evaluator before running autodev.\n"
            f"stderr: {baseline_eval.stderr.strip()}"
        )
    baseline_obj = parse_eval_json(baseline_eval.stdout)
    baseline_score = float(baseline_obj[cfg.evaluator.score_key])
    best_score = baseline_score
    best_commit = git(cwd, "rev-parse", "--short", "HEAD")

    append_result(
        results_path,
        [
            datetime.now().isoformat(timespec="seconds"),
            0,
            best_commit,
            f"{best_score:.8f}",
            "keep",
            baseline_obj.get("summary", "baseline"),
            "(baseline)",
        ],
    )
    history_tail.append(f"iter=0 score={best_score:.6f} status=keep summary=baseline")

    print(f"[autodev] baseline score={best_score:.8f} commit={best_commit}")

    stale_rounds = 0

    for iteration in range(1, cfg.run.max_iterations + 1):
        pre_untracked = list_untracked(cwd)
        iter_dir = runs_dir / f"iter_{iteration:04d}"
        iter_dir.mkdir(parents=True, exist_ok=True)

        prompt_text = render_agent_prompt(program_text, best_score, iteration, history_tail)
        prompt_path = prompts_dir / f"iter_{iteration:04d}.md"
        prompt_path.write_text(prompt_text, encoding="utf-8")

        cmd_ctx = {
            "prompt_file": shlex.quote(str(prompt_path)),
            "iteration": str(iteration),
            "best_score": f"{best_score:.8f}",
            "workspace": shlex.quote(str(cwd)),
            "program_file": shlex.quote(str(program_path)),
        }
        try:
            agent_cmd = cfg.agent.command.format_map(cmd_ctx)
        except KeyError as exc:
            raise RuntimeError(f"Unknown placeholder in agent command: {exc}") from exc

        print(f"[autodev] iteration={iteration} running agent")
        agent_result = run_cmd(
            agent_cmd,
            cwd=cwd,
            timeout_seconds=cfg.agent.timeout_seconds,
            output_file=iter_dir / "agent.log",
        )

        changed_all = list_changed(cwd)
        changed = [
            p for p in changed_all
            if not is_runtime_generated(p, cfg.workspace.artifacts_dir, cfg.workspace.results_tsv)
        ]
        if not changed:
            status = "discard"
            summary = "agent made no code changes"
            commit = git(cwd, "rev-parse", "--short", "HEAD")
            post_untracked = list_untracked(cwd)
            new_untracked = sorted(post_untracked - pre_untracked)
            runtime_untracked = [
                p for p in new_untracked
                if is_runtime_generated(p, cfg.workspace.artifacts_dir, cfg.workspace.results_tsv)
            ]
            restore_paths(cwd, runtime_untracked)
            append_result(
                results_path,
                [
                    datetime.now().isoformat(timespec="seconds"),
                    iteration,
                    commit,
                    f"{best_score:.8f}",
                    status,
                    summary,
                    "",
                ],
            )
            history_tail.append(f"iter={iteration} score={best_score:.6f} status={status} summary={summary}")
            stale_rounds += 1
            if stale_rounds >= cfg.run.patience:
                print(f"[autodev] stop: patience reached ({cfg.run.patience})")
                break
            continue

        disallowed = [p for p in changed if not is_allowed_path(p, cfg.workspace.editable_paths)]
        if disallowed:
            restore_paths(cwd, changed)
            post_untracked = list_untracked(cwd)
            new_untracked = sorted(post_untracked - pre_untracked)
            restore_paths(cwd, new_untracked)
            status = "discard"
            summary = f"changed disallowed paths: {', '.join(disallowed)}"
            commit = git(cwd, "rev-parse", "--short", "HEAD")
            append_result(
                results_path,
                [
                    datetime.now().isoformat(timespec="seconds"),
                    iteration,
                    commit,
                    f"{best_score:.8f}",
                    status,
                    summary,
                    ",".join(changed_all),
                ],
            )
            history_tail.append(f"iter={iteration} score={best_score:.6f} status={status} summary={summary}")
            stale_rounds += 1
            if stale_rounds >= cfg.run.patience:
                print(f"[autodev] stop: patience reached ({cfg.run.patience})")
                break
            continue

        print(f"[autodev] iteration={iteration} evaluating candidate")
        eval_result = run_cmd(
            cfg.evaluator.command,
            cwd=cwd,
            timeout_seconds=cfg.evaluator.timeout_seconds,
            output_file=iter_dir / "eval.log",
        )

        if agent_result.returncode != 0 or eval_result.returncode != 0:
            restore_paths(cwd, changed)
            post_untracked = list_untracked(cwd)
            new_untracked = sorted(post_untracked - pre_untracked)
            restore_paths(cwd, new_untracked)

            status = "crash"
            summary = (
                f"agent_rc={agent_result.returncode} eval_rc={eval_result.returncode}; "
                "candidate reverted"
            )
            commit = git(cwd, "rev-parse", "--short", "HEAD")
            append_result(
                results_path,
                [
                    datetime.now().isoformat(timespec="seconds"),
                    iteration,
                    commit,
                    f"{best_score:.8f}",
                    status,
                    summary,
                    ",".join(changed_all),
                ],
            )
            history_tail.append(f"iter={iteration} score={best_score:.6f} status={status} summary={summary}")
            stale_rounds += 1
            if stale_rounds >= cfg.run.patience:
                print(f"[autodev] stop: patience reached ({cfg.run.patience})")
                break
            continue

        try:
            obj = parse_eval_json(eval_result.stdout)
            new_score = float(obj[cfg.evaluator.score_key])
            eval_summary = str(obj.get("summary", ""))
        except Exception as exc:
            restore_paths(cwd, changed)
            status = "crash"
            summary = f"invalid evaluator output: {exc}"
            commit = git(cwd, "rev-parse", "--short", "HEAD")
            append_result(
                results_path,
                [
                    datetime.now().isoformat(timespec="seconds"),
                    iteration,
                    commit,
                    f"{best_score:.8f}",
                    status,
                    summary,
                    ",".join(changed_all),
                ],
            )
            history_tail.append(f"iter={iteration} score={best_score:.6f} status={status} summary={summary}")
            stale_rounds += 1
            if stale_rounds >= cfg.run.patience:
                print(f"[autodev] stop: patience reached ({cfg.run.patience})")
                break
            continue

        if improved(
            new_score,
            best_score,
            higher_is_better=cfg.evaluator.higher_is_better,
            eps=cfg.run.score_epsilon,
            keep_equal=cfg.run.keep_equal,
        ):
            msg = f"autodev iter {iteration}: score {new_score:.6f}"
            commit = maybe_commit_changes(cwd, cfg.workspace.editable_paths, msg)
            best_score = new_score
            best_commit = commit
            status = "keep"
            summary = eval_summary or "improved"
            stale_rounds = 0
            print(f"[autodev] keep iteration={iteration} score={new_score:.8f} commit={commit}")
        else:
            restore_paths(cwd, changed)
            post_untracked = list_untracked(cwd)
            new_untracked = sorted(post_untracked - pre_untracked)
            restore_paths(cwd, new_untracked)
            commit = git(cwd, "rev-parse", "--short", "HEAD")
            status = "discard"
            summary = eval_summary or "not improved"
            stale_rounds += 1
            print(f"[autodev] discard iteration={iteration} score={new_score:.8f} best={best_score:.8f}")

        post_untracked = list_untracked(cwd)
        new_untracked = sorted(post_untracked - pre_untracked)
        runtime_untracked = [
            p for p in new_untracked
            if is_runtime_generated(p, cfg.workspace.artifacts_dir, cfg.workspace.results_tsv)
        ]
        restore_paths(cwd, runtime_untracked)

        append_result(
            results_path,
            [
                datetime.now().isoformat(timespec="seconds"),
                iteration,
                commit,
                f"{new_score:.8f}",
                status,
                summary,
                ",".join(changed_all),
            ],
        )
        history_tail.append(f"iter={iteration} score={new_score:.6f} status={status} summary={summary}")

        if stale_rounds >= cfg.run.patience:
            print(f"[autodev] stop: patience reached ({cfg.run.patience})")
            break

    print(f"[autodev] done. best_score={best_score:.8f} best_commit={best_commit}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[autodev] fatal: {exc}", file=sys.stderr)
        raise SystemExit(1)
