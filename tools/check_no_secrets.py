"""Scan repository text files for high-confidence secrets and private IPv4 data."""

from __future__ import annotations

import ipaddress
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
MAX_TEXT_BYTES = 2 * 1024 * 1024
ALLOWED_CREDENTIAL_WORDS = {
    "changeme",
    "dummy",
    "example",
    "not-a-secret",
    "password",
    "redacted",
    "secret-password",
    "synthetic",
    "test",
    "test-password",
}
TOKEN_PATTERNS = {
    "GitHub token": re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b"),
    "GitHub fine-grained token": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "PEM private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}
CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)\b(?:password|passwd|enable_secret|api[_-]?token|secret[_-]?key)\b"
    r"\s*[:=]\s*['\"]([^'\"]{6,})['\"]"
)
IPV4_CANDIDATE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
RFC1918 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def _repository_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [ROOT / item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def _is_private_fixture(address: ipaddress.IPv4Address) -> bool:
    return any(address in network for network in RFC1918)


def main() -> int:
    findings: list[str] = []
    for path in _repository_files():
        if path.resolve() == SELF or not path.is_file() or path.stat().st_size > MAX_TEXT_BYTES:
            continue
        raw = path.read_bytes()
        if b"\0" in raw:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        relative = path.relative_to(ROOT)

        for label, pattern in TOKEN_PATTERNS.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                findings.append(f"{relative}:{line}: possible {label}")

        for match in CREDENTIAL_ASSIGNMENT.finditer(text):
            value = match.group(1).strip().lower()
            if value in ALLOWED_CREDENTIAL_WORDS or "${{" in value or value.startswith("<"):
                continue
            line = text.count("\n", 0, match.start()) + 1
            findings.append(f"{relative}:{line}: possible hard-coded credential")

        for match in IPV4_CANDIDATE.finditer(text):
            try:
                address = ipaddress.ip_address(match.group(0))
            except ValueError:
                continue
            if isinstance(address, ipaddress.IPv4Address) and _is_private_fixture(address):
                line = text.count("\n", 0, match.start()) + 1
                findings.append(f"{relative}:{line}: RFC1918 address must not be committed")

    if findings:
        print("Repository safety scan failed:", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        return 1
    print("Repository safety scan passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
