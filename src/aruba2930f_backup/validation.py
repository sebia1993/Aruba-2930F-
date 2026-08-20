"""Input, device identity, prompt, and command-output validation."""

from __future__ import annotations

import re
from ipaddress import IPv4Address

from .models import CollectionFailure, DeviceIdentity, DeviceTarget, ErrorCode


class InputValidationError(ValueError):
    """Raised after all IP input lines have been checked."""

    def __init__(self, issues: list[str]) -> None:
        self.issues = tuple(issues)
        super().__init__("\n".join(issues))


# Official AOS-S 16.10 applicable-product list for the Aruba 2930F series:
# https://arubanetworking.hpe.com/techdocs/AOS-S/16.10/FCI/content/
# fci/applicable%20products%20all%2016.10.htm
# Accessories (for example JL311A/JL312A) are intentionally not accepted.
ARUBA_2930F_MODELS: dict[str, str] = {
    "JL253A": "Aruba 2930F 24G 4SFP+",
    "JL254A": "Aruba 2930F 48G 4SFP+",
    "JL255A": "Aruba 2930F 24G PoE+ 4SFP+",
    "JL256A": "Aruba 2930F 48G PoE+ 4SFP+",
    "JL258A": "Aruba 2930F 8G PoE+ 2SFP+",
    "JL259A": "Aruba 2930F 24G 4SFP",
    "JL260A": "Aruba 2930F 48G 4SFP",
    "JL261A": "Aruba 2930F 24G PoE+ 4SFP",
    "JL262A": "Aruba 2930F 48G PoE+ 4SFP",
    "JL263A": "Aruba 2930F 24G PoE+ 4SFP+ TAA",
    "JL264A": "Aruba 2930F 48G PoE+ 4SFP+ TAA",
    "JL557A": "Aruba 2930F 48G PoE+ 4SFP 740W",
    "JL558A": "Aruba 2930F 48G PoE+ 4SFP+ 740W",
    "JL559A": "Aruba 2930F 48G PoE+ 4SFP+ 740W TAA",
    "JL692A": "Aruba 2930F 8G PoE+ 2SFP+ TAA",
    "JL693A": "Aruba 2930F 12G PoE+ 2G/2SFP+",
}

_SKU_RE = re.compile(r"\b(JL\d{3}A)\b", re.IGNORECASE)
_SOFTWARE_RE = re.compile(r"\b([A-Z]{2}\.\d{2}\.\d{2}\.\d{4})\b")
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CONFLICTING_FAMILY_RE = re.compile(
    r"\b(?:2530|2540|2920|2930M|3810M|5400R|6200F|6300[FM]|6400|CX)\b",
    re.IGNORECASE,
)
_CLI_ERROR_RE = re.compile(
    r"(?:^|\n)\s*(?:%\s*)?(?:invalid input|unknown command|incomplete input|"
    r"ambiguous input|command authorization failed|permission denied|"
    r"authorization failed|unrecognized command)\b",
    re.IGNORECASE,
)

PAGER_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^[ \t]*--\s*MORE\s*--,\s*next page:\s*Space,\s*"
        r"next line:\s*Enter,\s*quit:\s*Control-C[ \t]*$",
        re.IGNORECASE | re.MULTILINE,
    ),
)


def parse_ipv4_targets(text: str, *, port: int = 22) -> list[DeviceTarget]:
    """Parse one IPv4 address per non-blank line and reject the whole batch.

    All invalid and duplicate lines are reported together. No partial target list
    is returned when any issue exists.
    """

    issues: list[str] = []
    parsed: list[tuple[int, str]] = []
    seen: dict[str, int] = {}

    if not 1 <= port <= 65535:
        raise InputValidationError(["SSH port must be between 1 and 65535."])

    for line_number, raw in enumerate(text.splitlines(), start=1):
        value = raw.strip()
        if not value:
            continue
        try:
            address = IPv4Address(value)
        except ValueError:
            issues.append(f"Line {line_number}: invalid IPv4 address '{value}'.")
            continue
        normalized = str(address)
        if normalized in seen:
            issues.append(
                f"Line {line_number}: duplicate IPv4 address '{normalized}' "
                f"(first entered on line {seen[normalized]})."
            )
            continue
        seen[normalized] = line_number
        parsed.append((line_number, normalized))

    if not parsed and not issues:
        issues.append("Enter at least one IPv4 address.")
    if issues:
        raise InputValidationError(issues)
    return [DeviceTarget(ip=ip, port=port) for _, ip in parsed]


def validate_cli_response(command: str, output: str) -> None:
    if not isinstance(output, str):
        raise CollectionFailure(
            ErrorCode.COMMAND_REJECTED,
            f"{command} returned an invalid response.",
        )
    if _CLI_ERROR_RE.search(_ANSI_RE.sub("", output)):
        raise CollectionFailure(
            ErrorCode.COMMAND_REJECTED,
            f"The device rejected '{command}'.",
        )


def contains_pager_marker(output: str) -> bool:
    clean = _ANSI_RE.sub("", output)
    return any(pattern.search(clean) for pattern in PAGER_MARKERS)


def validate_output_limits(output: str, *, max_bytes: int, max_lines: int) -> None:
    if len(output.encode("utf-8")) > max_bytes or _line_count(output) > max_lines:
        raise CollectionFailure(
            ErrorCode.OUTPUT_LIMIT_EXCEEDED,
            "Command output exceeded the configured safety limit.",
        )


def validate_device_identity(
    version_output: str,
    modules_output: str = "",
    *,
    hostname: str | None = None,
) -> DeviceIdentity:
    """Return a 2930F identity only when official, non-conflicting evidence exists.

    ``show version`` is mandatory and must itself identify either the 2930F
    family or one official 2930F SKU. ``show modules`` can supply the missing
    SKU but can never independently turn a generic version response into a
    supported device. Any other switch-family evidence fails closed.
    """

    version = _ANSI_RE.sub("", version_output or "")
    modules = _ANSI_RE.sub("", modules_output or "")
    if not version.strip():
        raise _unsupported("The show version response was empty.")
    if _CONFLICTING_FAMILY_RE.search(version) or _CONFLICTING_FAMILY_RE.search(modules):
        raise _unsupported("A conflicting switch family was detected.")

    version_skus = _recognized_skus(version)
    module_skus = _recognized_skus(modules)
    reported_version_skus = {match.upper() for match in _SKU_RE.findall(version)}
    if reported_version_skus - ARUBA_2930F_MODELS.keys():
        raise _unsupported("show version reported an unknown chassis SKU.")
    has_family_in_version = bool(re.search(r"\b2930F\b", version, re.IGNORECASE))
    if not has_family_in_version and not version_skus:
        raise _unsupported("show version did not identify an Aruba 2930F.")

    candidates = version_skus or module_skus
    if len(candidates) != 1:
        reason = "No official Aruba 2930F SKU was found."
        if len(candidates) > 1:
            reason = "Conflicting Aruba 2930F SKUs were reported."
        raise _unsupported(reason)

    sku = next(iter(candidates))
    all_reported = version_skus | module_skus
    if any(reported != sku for reported in all_reported):
        raise _unsupported("Conflicting Aruba 2930F SKUs were reported.")

    software_match = _SOFTWARE_RE.search(version.upper())
    return DeviceIdentity(
        hostname=hostname,
        model=ARUBA_2930F_MODELS[sku],
        sku=sku,
        software_version=software_match.group(1) if software_match else None,
    )


def hostname_from_prompt(prompt: str) -> str | None:
    clean = _ANSI_RE.sub("", prompt or "").strip()
    if not clean or "\n" in clean:
        return None
    match = re.fullmatch(r"([A-Za-z0-9_.-]+)[#>]", clean)
    if match is None:
        return None
    value = match.group(1)
    if len(value) > 255:
        return None
    return value


def require_valid_prompt(prompt: str) -> str | None:
    hostname = hostname_from_prompt(prompt)
    if hostname is None:
        raise CollectionFailure(
            ErrorCode.PROMPT_PARSE_FAILED,
            "The final device prompt could not be verified.",
            transient=True,
        )
    return hostname


def normalize_config_text(output: str) -> str:
    """Normalize a verified config to the exact UTF-8/CRLF payload to be saved."""

    text = output.replace("\r\n", "\n").replace("\r", "\n")
    if not any(line.strip() for line in text.splitlines()):
        raise CollectionFailure(
            ErrorCode.PROMPT_PARSE_FAILED,
            "The running configuration output was empty.",
        )
    if contains_pager_marker(text):
        raise CollectionFailure(
            ErrorCode.PROMPT_PARSE_FAILED,
            "A paging marker remained in the configuration output.",
            transient=True,
        )
    return text.rstrip("\n").replace("\n", "\r\n") + "\r\n"


def _recognized_skus(text: str) -> set[str]:
    reported = {match.upper() for match in _SKU_RE.findall(text)}
    return reported & ARUBA_2930F_MODELS.keys()


def _unsupported(message: str) -> CollectionFailure:
    return CollectionFailure(ErrorCode.MODEL_UNSUPPORTED, message)


def _line_count(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)
