"""Verify the portable ZIP, checksum, SBOM, PE architecture, and optional smoke."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

PRODUCT = "Aruba2930FConfigBackup"
EXECUTABLE = f"{PRODUCT}.exe"
REQUIRED_DOCUMENTS = {"README.md", "LICENSE", "CHANGELOG.md", "SECURITY.md"}
FORBIDDEN_NAMES = {
    ".env",
    "known_hosts.json",
    "result.xlsx",
}
MAX_ARCHIVE_BYTES = 1_500_000_000
TEXT_SUFFIXES = {".json", ".md", ".txt", ".xml", ".yml", ".yaml"}
HIGH_CONFIDENCE_SECRET = re.compile(
    rb"(?:github_pat_[A-Za-z0-9_]{50,}|(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|"
    rb"(?:AKIA|ASIA)[A-Z0-9]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)"
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--zip", required=True, type=Path, dest="zip_path")
    parser.add_argument("--sha256", required=True, type=Path)
    parser.add_argument("--sbom", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--smoke-timeout", type=int, default=30)
    parser.add_argument("--allow-dirty", action="store_true")
    return parser.parse_args()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_sidecar(zip_path: Path, sidecar: Path) -> str:
    digest = _sha256(zip_path)
    line = sidecar.read_text(encoding="utf-8-sig").strip()
    match = re.fullmatch(r"([0-9a-fA-F]{64})\s+(.+)", line)
    if match is None:
        raise ValueError("SHA-256 sidecar must contain '<64 hex>  <zip filename>'")
    if match.group(1).lower() != digest:
        raise ValueError("ZIP SHA-256 does not match its sidecar")
    if match.group(2) != zip_path.name:
        raise ValueError("SHA-256 sidecar names a different archive")
    return digest


def _safe_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    members: dict[str, zipfile.ZipInfo] = {}
    folded: set[str] = set()
    total_size = 0
    root_prefix = f"{PRODUCT}/"
    for info in archive.infolist():
        normalized = info.filename.replace("\\", "/")
        path = PurePosixPath(normalized)
        if normalized.startswith("/") or path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Unsafe ZIP member path: {info.filename}")
        if not (normalized == PRODUCT or normalized.startswith(root_prefix)):
            raise ValueError(f"ZIP member is outside the single product root: {info.filename}")
        if normalized.casefold() in folded:
            raise ValueError(f"Duplicate case-insensitive ZIP member: {info.filename}")
        folded.add(normalized.casefold())
        unix_mode = info.external_attr >> 16
        if unix_mode & 0o170000 == 0o120000:
            raise ValueError(f"Symlink is not allowed in the portable ZIP: {info.filename}")
        total_size += info.file_size
        if total_size > MAX_ARCHIVE_BYTES:
            raise ValueError("Uncompressed ZIP contents exceed the release safety limit")
        members[normalized] = info
    return members


def _verify_pe_x64(data: bytes) -> None:
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise ValueError("Packaged executable does not have a DOS/PE header")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if pe_offset + 6 > len(data) or data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise ValueError("Packaged executable has an invalid PE signature")
    machine = struct.unpack_from("<H", data, pe_offset + 4)[0]
    if machine != 0x8664:
        raise ValueError(f"Packaged executable is not Windows x64 (machine=0x{machine:04x})")


def _verify_sbom(path: Path, version: str) -> bytes:
    raw = path.read_bytes()
    document = json.loads(raw.decode("utf-8"))
    if document.get("bomFormat") != "CycloneDX":
        raise ValueError("SBOM is not a CycloneDX document")
    if document.get("specVersion") not in {"1.5", "1.6", "1.7"}:
        raise ValueError("SBOM uses an unexpected CycloneDX specification version")
    metadata = document.get("metadata")
    component = metadata.get("component") if isinstance(metadata, dict) else None
    if not isinstance(component, dict) or component.get("version") != version:
        raise ValueError("SBOM root component version does not match the release")
    components = document.get("components")
    if not isinstance(components, list):
        raise ValueError("SBOM has no dependency components")
    names = {str(item.get("name", "")).casefold() for item in components if isinstance(item, dict)}
    expected = {"netmiko", "openpyxl", "pyside6"}
    if not expected.issubset(names):
        raise ValueError(f"SBOM is missing runtime dependencies: {sorted(expected - names)}")
    return raw


def _verify_archive(
    zip_path: Path, version: str, external_sbom: bytes, *, allow_dirty: bool = False
) -> tuple[zipfile.ZipFile, dict[str, zipfile.ZipInfo]]:
    expected_name = f"{PRODUCT}_v{version}_windows_x64.zip"
    if zip_path.name != expected_name:
        raise ValueError(f"Release ZIP must be named {expected_name}")
    archive = zipfile.ZipFile(zip_path)
    try:
        if archive.testzip() is not None:
            raise ValueError("ZIP CRC verification failed")
        members = _safe_members(archive)
        required = {
            f"{PRODUCT}/{EXECUTABLE}",
            f"{PRODUCT}/BUILD_INFO.json",
            f"{PRODUCT}/sbom.cdx.json",
            *(f"{PRODUCT}/{name}" for name in REQUIRED_DOCUMENTS),
        }
        missing = required - members.keys()
        if missing:
            raise ValueError(f"Portable ZIP is missing required files: {sorted(missing)}")

        for member_name in members:
            path = PurePosixPath(member_name)
            if path.name.casefold() in {name.casefold() for name in FORBIDDEN_NAMES}:
                raise ValueError(f"Forbidden runtime/user-data file in ZIP: {member_name}")
            if "backup" in {part.casefold() for part in path.parts} or path.suffix == ".part":
                raise ValueError(f"Backup or partial user data found in ZIP: {member_name}")

        executable_data = archive.read(f"{PRODUCT}/{EXECUTABLE}")
        if len(executable_data) < 100_000:
            raise ValueError("Packaged executable is unexpectedly small")
        _verify_pe_x64(executable_data)

        build_info = json.loads(archive.read(f"{PRODUCT}/BUILD_INFO.json").decode("utf-8"))
        if build_info.get("version") != version:
            raise ValueError("BUILD_INFO.json version does not match the release")
        if build_info.get("architecture") != "windows-x64":
            raise ValueError("BUILD_INFO.json architecture is not windows-x64")
        if build_info.get("commit") in {None, "", "unknown"} and not allow_dirty:
            raise ValueError("BUILD_INFO.json does not identify a Git commit")
        if build_info.get("dirtyTree") is not False and not allow_dirty:
            raise ValueError("Official release assets must come from a clean source tree")
        if archive.read(f"{PRODUCT}/sbom.cdx.json") != external_sbom:
            raise ValueError("SBOM in the ZIP differs from the published SBOM asset")

        for member_name, info in members.items():
            if info.is_dir() or PurePosixPath(member_name).suffix.casefold() not in TEXT_SUFFIXES:
                continue
            if info.file_size > 2 * 1024 * 1024:
                continue
            if HIGH_CONFIDENCE_SECRET.search(archive.read(info)):
                raise ValueError(f"High-confidence secret pattern found in ZIP: {member_name}")
        return archive, members
    except Exception:
        archive.close()
        raise


def _smoke(archive: zipfile.ZipFile, timeout: int) -> None:
    with tempfile.TemporaryDirectory(prefix="aruba2930f-release-") as temp_directory:
        archive.extractall(temp_directory)
        executable = Path(temp_directory) / PRODUCT / EXECUTABLE
        environment = os.environ.copy()
        environment["QT_QPA_PLATFORM"] = "offscreen"
        environment["PYTHONUTF8"] = "1"
        result = subprocess.run(
            [str(executable), "--smoke-test"],
            env=environment,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            raise ValueError(f"Packaged EXE smoke failed with exit code {result.returncode}")


def main() -> int:
    args = _arguments()
    for path in (args.zip_path, args.sha256, args.sbom):
        if not path.is_file():
            raise ValueError(f"Release asset does not exist: {path}")
    digest = _verify_sidecar(args.zip_path, args.sha256)
    sbom = _verify_sbom(args.sbom, args.version)
    archive, _ = _verify_archive(
        args.zip_path,
        args.version,
        sbom,
        allow_dirty=args.allow_dirty,
    )
    try:
        if args.smoke:
            _smoke(archive, args.smoke_timeout)
    finally:
        archive.close()
    print(f"Release verification passed: {args.zip_path.name} sha256={digest}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as error:
        print(f"release-verification: {error}", file=sys.stderr)
        sys.exit(1)
