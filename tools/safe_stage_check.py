#!/usr/bin/env python3
"""Portable safe-staging advisor for VoiceAI.

This command is intentionally read-only: it inspects Git state, pulls a small
amount of Engram context when the CLI is available, and prints conservative
staging recommendations. It never runs ``git add`` or modifies files.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


PROJECT_NAME = "voiceai"
DEFAULT_ENGRAM_EXE = Path(r"E:\Job\bin\engram.exe")

SENSITIVE_UNTRACKED_PREFIXES = (
    "data/",
    "logs/",
    "Grabaciones/",
    "modelos_f5/",
    "temp/",
    "Documents/",
    ".env",
)

SENSITIVE_NAME_MARKERS = (
    "token",
    "secret",
    "credential",
    "oauth",
    "apikey",
    "api_key",
    "password",
)

SAFE_UNTRACKED_SUFFIXES = (
    ".py",
    ".ts",
    ".js",
    ".json",
    ".jsonc",
    ".md",
    ".yml",
    ".yaml",
    ".toml",
    ".ini",
    ".txt",
)

GENERATED_OR_LOCAL_PREFIXES = (
    "__pycache__/",
    ".pytest_cache/",
    "dist/",
    "build/",
    "venv/",
    ".venv/",
)


@dataclass(frozen=True)
class StatusEntry:
    index_status: str
    worktree_status: str
    path: str
    original_path: str | None = None

    @property
    def is_untracked(self) -> bool:
        return self.index_status == "?" and self.worktree_status == "?"

    @property
    def is_staged(self) -> bool:
        return self.index_status not in (" ", "?")

    @property
    def is_tracked_worktree_change(self) -> bool:
        return not self.is_untracked and self.worktree_status != " "


def run(cmd: list[str], cwd: Path, timeout: int = 10) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def repo_root(start: Path) -> Path:
    proc = run(["git", "rev-parse", "--show-toplevel"], start)
    if proc.returncode != 0:
        raise SystemExit("ERROR: this command must be run inside a Git repository.")
    return Path(proc.stdout.strip()).resolve()


def parse_porcelain(output: str) -> list[StatusEntry]:
    entries: list[StatusEntry] = []
    for line in output.splitlines():
        if not line:
            continue
        index_status = line[0]
        worktree_status = line[1]
        raw_path = line[3:] if len(line) > 3 else ""

        original_path: str | None = None
        path = raw_path
        if " -> " in raw_path:
            original_path, path = raw_path.split(" -> ", 1)

        entries.append(StatusEntry(index_status, worktree_status, path, original_path))
    return entries


def is_sensitive_untracked(path: str) -> bool:
    normalized = path.replace("\\", "/")
    lowered = normalized.lower()
    return normalized.startswith(SENSITIVE_UNTRACKED_PREFIXES) or any(
        marker in lowered for marker in SENSITIVE_NAME_MARKERS
    )


def is_generated_or_local(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return normalized.startswith(GENERATED_OR_LOCAL_PREFIXES)


def is_safe_untracked_candidate(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return normalized.endswith(SAFE_UNTRACKED_SUFFIXES)


def is_conductor_review_artifact(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return normalized == "conductor/tracks.md" or normalized.startswith("conductor/tracks/")


def normalize_git_path(path: str) -> str:
    return path.replace("\\", "/").strip("/")


def parent_dir(path: str) -> str:
    normalized = normalize_git_path(path)
    if not normalized or "/" not in normalized:
        return "."
    return normalized.rsplit("/", 1)[0]


def review_artifact_scopes(entries: Iterable[StatusEntry], requested_paths: Sequence[str]) -> list[str]:
    if requested_paths:
        return [normalize_git_path(path) for path in requested_paths if normalize_git_path(path)]

    scopes: set[str] = set()
    for entry in entries:
        paths = [entry.path]
        if entry.original_path:
            paths.append(entry.original_path)
        for path in paths:
            normalized = normalize_git_path(path)
            if normalized.startswith("conductor/tracks/"):
                scopes.add(parent_dir(normalized))

    return sorted(scopes)


def quote_path(path: str) -> str:
    escaped = path.replace('"', '\\"')
    return f'"{escaped}"'


def find_engram_exe() -> str | None:
    env_value = os.environ.get("ENGRAM_EXE")
    if env_value and Path(env_value).exists():
        return env_value
    if DEFAULT_ENGRAM_EXE.exists():
        return str(DEFAULT_ENGRAM_EXE)
    return shutil.which("engram") or shutil.which("engram.exe")


def collect_engram_context(root: Path) -> list[str]:
    engram = find_engram_exe()
    if not engram:
        return ["Engram CLI: not found; ask the agent to use mem_context/mem_search if needed."]

    snippets: list[str] = [f"Engram CLI: {engram}"]
    commands = [
        [engram, "context", PROJECT_NAME],
        [engram, "search", "safe-stage guardrails Track B stress-first TDD", "--project", PROJECT_NAME],
    ]
    for cmd in commands:
        proc = run(cmd, root, timeout=15)
        label = " ".join(Path(part).name if part == engram else part for part in cmd)
        if proc.returncode != 0:
            snippets.append(f"{label}: unavailable ({proc.stderr.strip() or 'non-zero exit'}).")
            continue
        cleaned = "\n".join(line.rstrip() for line in proc.stdout.splitlines()[:20] if line.strip())
        snippets.append(f"{label}:\n{cleaned or '(no output)'}")
    return snippets


def classify(entries: Iterable[StatusEntry]) -> tuple[list[str], list[str], list[str]]:
    safe_to_stage: list[str] = []
    needs_decision: list[str] = []
    already_staged: list[str] = []

    for entry in entries:
        if entry.is_staged:
            already_staged.append(entry.path)

        if entry.is_untracked:
            if is_sensitive_untracked(entry.path) or is_generated_or_local(entry.path):
                needs_decision.append(f"{entry.path} (untracked/local data; do not stage blindly)")
            elif is_conductor_review_artifact(entry.path):
                needs_decision.append(f"{entry.path} (Conductor review artifact; verify current track before staging)")
            elif is_safe_untracked_candidate(entry.path):
                safe_to_stage.append(entry.path)
            else:
                needs_decision.append(f"{entry.path} (untracked; verify intent before staging)")
            continue

        if entry.is_tracked_worktree_change:
            if is_conductor_review_artifact(entry.path):
                needs_decision.append(f"{entry.path} (Conductor review artifact; verify current track before staging)")
            else:
                safe_to_stage.append(entry.path)

    return safe_to_stage, needs_decision, already_staged


def collect_ignored_review_artifacts(root: Path, scopes: Sequence[str]) -> list[str]:
    if not scopes:
        return []

    proc = run(
        ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "--", *scopes],
        root,
    )
    if proc.returncode != 0:
        return []
    return sorted(set(proc.stdout.splitlines()))


def build_report(root: Path, include_engram: bool, requested_paths: Sequence[str]) -> dict[str, object]:
    status_proc = run(["git", "status", "--short", "--untracked-files=all"], root)
    if status_proc.returncode != 0:
        raise SystemExit(status_proc.stderr.strip() or "ERROR: git status failed.")

    entries = parse_porcelain(status_proc.stdout)
    safe_to_stage, needs_decision, already_staged = classify(entries)
    ignored_review_scopes = review_artifact_scopes(entries, requested_paths)

    diff_name_proc = run(["git", "diff", "--name-only"], root)
    staged_name_proc = run(["git", "diff", "--cached", "--name-only"], root)
    ignored_review_artifacts = collect_ignored_review_artifacts(root, ignored_review_scopes)

    return {
        "project": PROJECT_NAME,
        "repo_root": str(root),
        "engram": collect_engram_context(root) if include_engram else ["Skipped by --no-engram."],
        "git_status_short": status_proc.stdout.splitlines(),
        "unstaged_diff_files": diff_name_proc.stdout.splitlines() if diff_name_proc.returncode == 0 else [],
        "staged_diff_files": staged_name_proc.stdout.splitlines() if staged_name_proc.returncode == 0 else [],
        "safe_to_stage": safe_to_stage,
        "needs_decision": needs_decision,
        "already_staged": already_staged,
        "ignored_review_scopes": ignored_review_scopes,
        "ignored_review_artifacts": ignored_review_artifacts,
        "recommended_git_add": [f"git add -- {quote_path(path)}" for path in safe_to_stage],
        "optional_force_add_review_artifacts": [
            f"git add -f -- {quote_path(path)}" for path in ignored_review_artifacts
        ],
    }


def print_report(report: dict[str, object]) -> None:
    print("Safe Stage Check — VoiceAI")
    print("=" * 31)
    print(f"Repo: {report['repo_root']}")
    print()

    print("Engram context")
    print("--------------")
    for snippet in report["engram"]:  # type: ignore[index]
        print(snippet)
        print()

    print("Git status --short")
    print("------------------")
    status_lines = report["git_status_short"]  # type: ignore[assignment]
    if status_lines:
        for line in status_lines:  # type: ignore[union-attr]
            print(line)
    else:
        print("Clean working tree.")
    print()

    print("Safe-to-stage candidates")
    print("------------------------")
    safe_to_stage = report["safe_to_stage"]  # type: ignore[assignment]
    if safe_to_stage:
        for path in safe_to_stage:  # type: ignore[union-attr]
            print(f"- {path}")
    else:
        print("None.")
    print()

    print("Needs human decision / do not stage blindly")
    print("-------------------------------------------")
    needs_decision = report["needs_decision"]  # type: ignore[assignment]
    if needs_decision:
        for path in needs_decision:  # type: ignore[union-attr]
            print(f"- {path}")
    else:
        print("None.")
    print()

    print("Recommended commands")
    print("--------------------")
    commands = report["recommended_git_add"]  # type: ignore[assignment]
    if commands:
        for command in commands:  # type: ignore[union-attr]
            print(command)
    else:
        print("No git add recommendation.")
    print()

    print("Ignored review artifacts")
    print("------------------------")
    ignored_review_scopes = report["ignored_review_scopes"]  # type: ignore[assignment]
    if ignored_review_scopes:
        print("Scope:")
        for path in ignored_review_scopes:  # type: ignore[union-attr]
            print(f"- {path}")
        print()
    ignored_review_artifacts = report["ignored_review_artifacts"]  # type: ignore[assignment]
    if ignored_review_artifacts:
        for path in ignored_review_artifacts:  # type: ignore[union-attr]
            print(f"- {path} (ignored by .gitignore; use git add -f only if this artifact should be versioned)")
        print()
        print("Optional force-add commands")
        print("---------------------------")
        for command in report["optional_force_add_review_artifacts"]:  # type: ignore[index]
            print(command)
    else:
        print("None.")
    print()

    if needs_decision or ignored_review_artifacts:
        print("Verdict: REVIEW REQUIRED before staging/commit.")
    else:
        print("Verdict: no obvious untracked/local-data blockers found.")
    print("Read-only check: no files were staged or modified.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Git/Engram safe-staging advisor.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON instead of text.")
    parser.add_argument("--no-engram", action="store_true", help="Skip optional Engram CLI context lookup.")
    parser.add_argument(
        "paths",
        nargs="*",
        help="Optional pathspecs to inspect for ignored review artifacts, for example conductor/tracks/<track>.",
    )
    args = parser.parse_args(argv)

    root = repo_root(Path.cwd())
    report = build_report(root, include_engram=not args.no_engram, requested_paths=args.paths)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
