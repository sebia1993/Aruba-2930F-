"""Fail when public version declarations or runtime dependency pins drift."""

from __future__ import annotations

import argparse
import ast
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected", help="Expected semantic version without a leading v")
    parser.add_argument("--tag", help="Expected release tag, for example v0.1.1")
    return parser.parse_args()


def _package_version(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "__version__"
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    return node.value.value
    raise ValueError(f"__version__ string not found in {path}")


def _direct_pins(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("-r "):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^;\s]+)", line)
        if match is None:
            raise ValueError(f"{path.name} contains a non-exact requirement: {line}")
        pins[re.sub(r"[-_.]+", "-", match.group(1)).lower()] = match.group(2)
    return pins


def _project_pins(project: dict[str, object]) -> dict[str, str]:
    dependencies = project.get("dependencies")
    if not isinstance(dependencies, list):
        raise ValueError("project.dependencies is missing")
    pins: dict[str, str] = {}
    for value in dependencies:
        if not isinstance(value, str):
            raise ValueError("project.dependencies contains a non-string value")
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^;\s]+)", value)
        if match is None:
            raise ValueError(f"pyproject.toml contains a non-exact runtime dependency: {value}")
        pins[re.sub(r"[-_.]+", "-", match.group(1)).lower()] = match.group(2)
    return pins


def main() -> int:
    args = _arguments()
    document = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = document.get("project")
    if not isinstance(project, dict):
        raise ValueError("[project] is missing from pyproject.toml")

    pyproject_version = project.get("version")
    if not isinstance(pyproject_version, str):
        raise ValueError("project.version is missing")
    package_version = _package_version(ROOT / "src" / "aruba2930f_backup" / "__init__.py")

    expected = args.expected
    if args.tag:
        if not re.fullmatch(r"v\d+\.\d+\.\d+", args.tag):
            raise ValueError(f"Release tag must be vMAJOR.MINOR.PATCH: {args.tag}")
        tag_version = args.tag[1:]
        if expected is not None and expected != tag_version:
            raise ValueError("--expected and --tag disagree")
        expected = tag_version

    versions = {"pyproject.toml": pyproject_version, "package": package_version}
    if expected is not None:
        versions["expected"] = expected
    if len(set(versions.values())) != 1:
        details = ", ".join(f"{name}={value}" for name, value in versions.items())
        raise ValueError(f"Version declarations disagree: {details}")

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    if f"## [{pyproject_version}]" not in changelog:
        raise ValueError(f"CHANGELOG.md has no [{pyproject_version}] release section")

    synchronized_text = {
        "build_windows.ps1": f'[string]$Version = "{pyproject_version}"',
        ".github/workflows/ci.yml": f"-Version {pyproject_version}",
        "README.md": f"Aruba2930FConfigBackup_v{pyproject_version}_windows_x64.zip",
    }
    for relative_path, expected_text in synchronized_text.items():
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        if expected_text not in text:
            raise ValueError(
                f"{relative_path} does not declare current version {pyproject_version}"
            )

    project_pins = _project_pins(project)
    requirements_pins = _direct_pins(ROOT / "requirements.txt")
    if project_pins != requirements_pins:
        raise ValueError(
            "requirements.txt does not match pyproject.toml runtime pins: "
            f"pyproject={project_pins}, requirements={requirements_pins}"
        )

    lock_pins = _direct_pins(ROOT / "requirements-lock.txt")
    drift = {
        name: (version, lock_pins.get(name))
        for name, version in requirements_pins.items()
        if lock_pins.get(name) != version
    }
    if drift:
        raise ValueError(f"requirements-lock.txt direct pins drifted: {drift}")

    print(f"Version and runtime pins are synchronized at {pyproject_version}.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, tomllib.TOMLDecodeError) as error:
        print(f"version-check: {error}", file=sys.stderr)
        sys.exit(1)
