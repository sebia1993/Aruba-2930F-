"""Verify a remote annotated release tag against checkout, event, and main."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


class ReleaseRefError(ValueError):
    """A release ref cannot be proven safe for publication."""


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True, help="Release tag in vMAJOR.MINOR.PATCH form")
    parser.add_argument("--event-sha", required=True, help="GitHub event commit SHA")
    return parser.parse_args()


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        operation = arguments[0] if arguments else "unknown"
        raise ReleaseRefError(f"Git {operation} operation failed")
    return result.stdout.strip()


def _commit(repository: Path, revision: str, label: str) -> str:
    try:
        return _git(repository, "rev-parse", "--verify", f"{revision}^{{commit}}")
    except ReleaseRefError as error:
        raise ReleaseRefError(f"Could not resolve the {label} commit") from error


def verify_release_ref(tag: str, event_sha: str, repository: Path) -> str:
    """Return the verified commit after enforcing all release ref invariants."""

    if re.fullmatch(r"v\d+\.\d+\.\d+", tag) is None:
        raise ReleaseRefError(f"Release tag must use vMAJOR.MINOR.PATCH: {tag}")
    if re.fullmatch(r"[0-9a-fA-F]{40}", event_sha) is None:
        raise ReleaseRefError("Event SHA must be a full 40-character hexadecimal commit")

    release_ref = f"refs/release-verify/tags/{tag}"
    main_ref = "refs/release-verify/heads/main"
    _git(
        repository,
        "fetch",
        "--force",
        "--no-tags",
        "origin",
        f"+refs/tags/{tag}:{release_ref}",
        f"+refs/heads/main:{main_ref}",
    )

    if _git(repository, "cat-file", "-t", release_ref) != "tag":
        raise ReleaseRefError(f"Release tag must be an annotated Git tag: {tag}")

    commits = {
        "remote tag": _commit(repository, release_ref, "remote tag"),
        "checkout HEAD": _commit(repository, "HEAD", "checkout HEAD"),
        "event SHA": _commit(repository, event_sha, "event SHA"),
        "remote main": _commit(repository, main_ref, "remote main"),
    }
    if len(set(commits.values())) != 1:
        details = ", ".join(f"{label}={commit[:12]}" for label, commit in commits.items())
        raise ReleaseRefError(f"Release refs do not resolve to one commit: {details}")

    return commits["remote tag"]


def main() -> int:
    args = _arguments()
    commit = verify_release_ref(args.tag, args.event_sha, Path.cwd())
    print(f"Release ref gate passed: {args.tag} -> {commit}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ReleaseRefError) as error:
        print(f"release-ref-check: {error}", file=sys.stderr)
        sys.exit(1)
