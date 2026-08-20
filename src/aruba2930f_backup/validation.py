"""Input, device identity, prompt, and command-output validation."""

from __future__ import annotations

import re
from ipaddress import IPv4Address

from .models import (
    CollectionFailure,
    DeviceIdentity,
    DeviceTarget,
    DiagnosticDetail,
    ErrorCode,
)


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
_FAMILY_2930F_RE = re.compile(r"\b2930F\b", re.IGNORECASE)
_APPENDED_CLI_MODE_RE = re.compile(r"^.+\([^()\r\n]+\)[ \t]*[#>]$")
_MAX_PROMPT_LENGTH = 256
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
    """Return a 2930F identity from compatible, non-conflicting CLI evidence.

    ``show version`` remains mandatory, but ArubaOS-Switch commonly reports only
    software and boot details there. Chassis evidence from ``show modules`` is
    therefore considered equally. Exact official SKUs take precedence, while a
    standalone 2930F family marker is sufficient for a generic identity.
    """

    version = _ANSI_RE.sub("", version_output or "")
    modules = _ANSI_RE.sub("", modules_output or "")
    if not version.strip():
        raise _unsupported(
            "The show version response was empty.",
            DiagnosticDetail.IDENTITY_EVIDENCE_MISSING,
        )
    if _CONFLICTING_FAMILY_RE.search(version) or _CONFLICTING_FAMILY_RE.search(modules):
        raise _unsupported(
            "A conflicting switch family was detected.",
            DiagnosticDetail.IDENTITY_FAMILY_CONFLICT,
        )

    version_skus = _recognized_skus(version)
    module_skus = _recognized_skus(modules)
    candidates = version_skus | module_skus
    explicit_skus = _all_skus(version) | _explicit_module_identity_skus(modules)
    unsupported_skus = explicit_skus - ARUBA_2930F_MODELS.keys()
    if unsupported_skus:
        detail = (
            DiagnosticDetail.IDENTITY_SKU_CONFLICT
            if candidates or len(unsupported_skus) > 1
            else DiagnosticDetail.IDENTITY_SKU_UNSUPPORTED
        )
        raise _unsupported("Unsupported or conflicting chassis SKU evidence was detected.", detail)

    software_match = _SOFTWARE_RE.search(version.upper())
    if len(candidates) == 1:
        sku = next(iter(candidates))
        model = ARUBA_2930F_MODELS[sku]
        display_sku: str | None = sku
    elif len(candidates) > 1:
        model = "Aruba 2930F VSF"
        display_sku = ", ".join(sorted(candidates))
    elif _FAMILY_2930F_RE.search(version) or _FAMILY_2930F_RE.search(modules):
        model = "Aruba 2930F"
        display_sku = None
    else:
        raise _unsupported(
            "The command responses did not identify an Aruba 2930F.",
            DiagnosticDetail.IDENTITY_EVIDENCE_MISSING,
        )

    return DeviceIdentity(
        hostname=hostname,
        model=model,
        sku=display_sku,
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
    """Validate an exact EXEC prompt while extracting only simple hostnames.

    Aruba prompts can contain display wrappers, spaces, and model characters
    that are not valid DNS hostname characters. Those prompts are safe to use
    as opaque command terminators, but they are not promoted to hostname
    metadata. Appended CLI modes such as ``switch(config)#`` remain blocked.
    """

    clean = _ANSI_RE.sub("", prompt or "").strip()
    if not clean:
        detail = DiagnosticDetail.PROMPT_EMPTY
    elif _APPENDED_CLI_MODE_RE.fullmatch(clean):
        detail = DiagnosticDetail.PROMPT_NON_EXEC_MODE
    elif (
        len(clean) > _MAX_PROMPT_LENGTH
        or "\n" in clean
        or "\r" in clean
        or clean[-1] not in "#>"
        or not clean[:-1].strip()
        or any(not character.isprintable() for character in clean)
    ):
        detail = DiagnosticDetail.PROMPT_FORMAT
    else:
        return hostname_from_prompt(clean)

    raise CollectionFailure(
        ErrorCode.PROMPT_PARSE_FAILED,
        "The final device prompt could not be verified.",
        transient=True,
        diagnostic_detail=detail,
    )


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
    reported = _all_skus(text)
    return reported & ARUBA_2930F_MODELS.keys()


def _all_skus(text: str) -> set[str]:
    return {match.upper() for match in _SKU_RE.findall(text)}


def _explicit_module_identity_skus(text: str) -> set[str]:
    """Return module-output SKUs only from lines that describe chassis identity.

    ``show modules`` can also list transceiver and expansion-module part numbers.
    Those rows must not turn an otherwise supported chassis into an unknown SKU.
    """

    reported: set[str] = set()
    for line in text.splitlines():
        if re.search(r"\b(?:chassis|model)\b", line, re.IGNORECASE) or re.search(
            r"\b(?:2930F|2930M|2530|2540|2920|3810M|5400R|6200F|6300[FM]|6400|CX)\b",
            line,
            re.IGNORECASE,
        ):
            reported.update(_all_skus(line))
    return reported


def _unsupported(message: str, detail: DiagnosticDetail) -> CollectionFailure:
    return CollectionFailure(
        ErrorCode.MODEL_UNSUPPORTED,
        message,
        diagnostic_detail=detail,
    )


def _line_count(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)
