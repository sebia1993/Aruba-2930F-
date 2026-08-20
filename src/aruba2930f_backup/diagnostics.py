"""Compact, privacy-preserving offline diagnostic codes."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from . import __version__
from .models import (
    CollectionFailure,
    DeviceResult,
    DeviceStatus,
    DiagnosticDetail,
    DiagnosticPhase,
    ErrorCode,
)

SCHEMA_PREFIX = "A3F1"
_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CROCKFORD_VALUES = {character: index for index, character in enumerate(_CROCKFORD_ALPHABET)}
_CROCKFORD_ALIASES = str.maketrans({"O": "0", "I": "1", "L": "1"})


class DiagnosticStatus(StrEnum):
    FAILED = "failed"
    RETRY_EXHAUSTED = "retry_exhausted"
    CANCELLED = "cancelled"
    FATAL_APP = "fatal_app"


_PHASE_TO_ID = {
    DiagnosticPhase.APP: 0,
    DiagnosticPhase.HOST_KEY: 1,
    DiagnosticPhase.CONNECT_AUTH: 2,
    DiagnosticPhase.SESSION_SETUP: 3,
    DiagnosticPhase.DEVICE_IDENTITY: 4,
    DiagnosticPhase.CONFIG_COLLECTION: 5,
    DiagnosticPhase.REPORT_STORAGE: 6,
    DiagnosticPhase.UNKNOWN: 7,
}
_ID_TO_PHASE = {identifier: phase for phase, identifier in _PHASE_TO_ID.items()}

# These identifiers are part of the public diagnostic schema. Never derive
# them from enum iteration and never reuse an identifier for another error.
_ERROR_TO_ID = {
    ErrorCode.INPUT_INVALID: 1,
    ErrorCode.HOST_KEY_REJECTED: 2,
    ErrorCode.HOST_KEY_CHANGED: 3,
    ErrorCode.TCP_TIMEOUT: 4,
    ErrorCode.SSH_ALGORITHM_INCOMPATIBLE: 5,
    ErrorCode.SSH_NEGOTIATION_FAILED: 6,
    ErrorCode.AUTH_FAILED: 7,
    ErrorCode.ENABLE_FAILED: 8,
    ErrorCode.PAGING_SETUP_FAILED: 9,
    ErrorCode.MODEL_UNSUPPORTED: 10,
    ErrorCode.COMMAND_TIMEOUT: 11,
    ErrorCode.COMMAND_REJECTED: 12,
    ErrorCode.PROMPT_PARSE_FAILED: 13,
    ErrorCode.OUTPUT_LIMIT_EXCEEDED: 14,
    ErrorCode.REPORT_WRITE_FAILED: 15,
    ErrorCode.CANCELLED: 16,
    ErrorCode.UNEXPECTED_ERROR: 17,
}
_ID_TO_ERROR = {identifier: error for error, identifier in _ERROR_TO_ID.items()}

_STATUS_TO_ID = {
    DiagnosticStatus.FAILED: 0,
    DiagnosticStatus.RETRY_EXHAUSTED: 1,
    DiagnosticStatus.CANCELLED: 2,
    DiagnosticStatus.FATAL_APP: 3,
}
_ID_TO_STATUS = {identifier: status for status, identifier in _STATUS_TO_ID.items()}

_DETAIL_TO_ID = {
    DiagnosticDetail.NONE: 0,
    DiagnosticDetail.LOGIN_BANNER_PENDING: 1,
    DiagnosticDetail.PROMPT_EMPTY: 2,
    DiagnosticDetail.PROMPT_FORMAT: 3,
    DiagnosticDetail.PROMPT_NON_EXEC_MODE: 4,
    DiagnosticDetail.PROMPT_MISMATCH: 5,
    DiagnosticDetail.PROMPT_READ_ERROR: 6,
    DiagnosticDetail.IMPORT_ERROR: 7,
    DiagnosticDetail.OS_ERROR: 8,
    DiagnosticDetail.VALUE_OR_TYPE_ERROR: 9,
    DiagnosticDetail.RUNTIME_ERROR: 10,
    DiagnosticDetail.MEMORY_ERROR: 11,
    DiagnosticDetail.IDENTITY_EVIDENCE_MISSING: 12,
    DiagnosticDetail.IDENTITY_SKU_UNSUPPORTED: 13,
    DiagnosticDetail.IDENTITY_FAMILY_CONFLICT: 14,
    DiagnosticDetail.IDENTITY_SKU_CONFLICT: 15,
}
_ID_TO_DETAIL = {identifier: detail for detail, identifier in _DETAIL_TO_ID.items()}


@dataclass(frozen=True, slots=True)
class DecodedDiagnostic:
    code: str
    version: str
    phase: DiagnosticPhase
    error_code: ErrorCode | None
    status: DiagnosticStatus
    host_key_attempts: int
    backup_attempts: int
    detail: DiagnosticDetail

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_version(version: str) -> tuple[int, int, int]:
    parts = version.split(".")
    if len(parts) != 3 or any(not part.isdecimal() for part in parts):
        raise ValueError("Diagnostic versions must use major.minor.patch integers.")
    major, minor, patch = (int(part) for part in parts)
    if major > 15 or minor > 63 or patch > 255:
        raise ValueError("Diagnostic version exceeds the schema bit allocation.")
    return major, minor, patch


def _bounded_attempts(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 7:
        raise ValueError("Attempt counts must be integers between 0 and 7.")
    return value


def _coerce_status(value: DiagnosticStatus | DeviceStatus | str) -> DiagnosticStatus:
    if isinstance(value, DiagnosticStatus):
        return value
    if isinstance(value, DeviceStatus):
        if value is DeviceStatus.FAILED:
            return DiagnosticStatus.FAILED
        if value is DeviceStatus.RETRY_EXHAUSTED:
            return DiagnosticStatus.RETRY_EXHAUSTED
        if value is DeviceStatus.CANCELLED:
            return DiagnosticStatus.CANCELLED
        raise ValueError("Successful or unfinished results do not have diagnostic codes.")
    try:
        return DiagnosticStatus(value)
    except ValueError as exc:
        raise ValueError("Unknown diagnostic status.") from exc


def _crc5_epc(data: bytes) -> int:
    """Return CRC-5/EPC (poly=0x09, init=0x09, xorout=0)."""

    crc = 0x09
    for byte in data:
        for shift in range(7, -1, -1):
            input_bit = (byte >> shift) & 1
            top_bit = (crc >> 4) & 1
            crc = (crc << 1) & 0x1F
            if top_bit ^ input_bit:
                crc ^= 0x09
    return crc


def encode_diagnostic_code(
    *,
    version: str = __version__,
    phase: DiagnosticPhase,
    error_code: ErrorCode | None,
    status: DiagnosticStatus | DeviceStatus | str,
    host_key_attempts: int = 0,
    backup_attempts: int = 0,
    detail: DiagnosticDetail = DiagnosticDetail.NONE,
) -> str:
    """Encode non-sensitive failure metadata into a 15-character code."""

    major, minor, patch = _parse_version(version)
    normalized_status = _coerce_status(status)
    fields = (
        (major, 4),
        (minor, 6),
        (patch, 8),
        (_PHASE_TO_ID[phase], 3),
        (_ERROR_TO_ID[error_code] if error_code is not None else 0, 5),
        (_STATUS_TO_ID[normalized_status], 2),
        (_bounded_attempts(host_key_attempts), 3),
        (_bounded_attempts(backup_attempts), 3),
        (_DETAIL_TO_ID[detail], 4),
        (0, 2),
    )
    payload = 0
    for value, width in fields:
        payload = (payload << width) | value
    payload_bytes = payload.to_bytes(5, "big")
    encoded_payload = "".join(
        _CROCKFORD_ALPHABET[(payload >> shift) & 0x1F] for shift in range(35, -1, -5)
    )
    check_character = _CROCKFORD_ALPHABET[_crc5_epc(SCHEMA_PREFIX.encode("ascii") + payload_bytes)]
    return f"{SCHEMA_PREFIX}-{encoded_payload}-{check_character}"


def _normalized_code(code: str) -> tuple[str, str]:
    parts = code.strip().upper().split("-")
    if len(parts) != 3 or parts[0] != SCHEMA_PREFIX or len(parts[1]) != 8 or len(parts[2]) != 1:
        raise ValueError("Diagnostic code must use A3F1-XXXXXXXX-C format.")
    payload_text = parts[1].translate(_CROCKFORD_ALIASES)
    check_text = parts[2].translate(_CROCKFORD_ALIASES)
    if any(character not in _CROCKFORD_VALUES for character in payload_text + check_text):
        raise ValueError("Diagnostic code contains an invalid Crockford Base32 character.")
    return payload_text, check_text


def decode_diagnostic_code(code: str) -> DecodedDiagnostic:
    """Validate and decode one diagnostic code."""

    payload_text, check_text = _normalized_code(code)
    payload = 0
    for character in payload_text:
        payload = (payload << 5) | _CROCKFORD_VALUES[character]
    payload_bytes = payload.to_bytes(5, "big")
    expected_check = _crc5_epc(SCHEMA_PREFIX.encode("ascii") + payload_bytes)
    if _CROCKFORD_VALUES[check_text] != expected_check:
        raise ValueError("Diagnostic code check character does not match.")

    remaining = payload
    widths = (4, 6, 8, 3, 5, 2, 3, 3, 4, 2)
    values: list[int] = []
    remaining_bits = 40
    for width in widths:
        remaining_bits -= width
        values.append((remaining >> remaining_bits) & ((1 << width) - 1))
    (
        major,
        minor,
        patch,
        phase_id,
        error_id,
        status_id,
        host_attempts,
        backup_attempts,
        detail_id,
        reserved,
    ) = values
    if reserved != 0:
        raise ValueError("Diagnostic code uses reserved schema bits.")
    if error_id not in {0, *_ID_TO_ERROR}:
        raise ValueError("Diagnostic code uses a reserved error identifier.")
    if detail_id not in _ID_TO_DETAIL:
        raise ValueError("Diagnostic code uses a reserved detail identifier.")

    normalized = encode_diagnostic_code(
        version=f"{major}.{minor}.{patch}",
        phase=_ID_TO_PHASE[phase_id],
        error_code=_ID_TO_ERROR.get(error_id),
        status=_ID_TO_STATUS[status_id],
        host_key_attempts=host_attempts,
        backup_attempts=backup_attempts,
        detail=_ID_TO_DETAIL[detail_id],
    )
    return DecodedDiagnostic(
        code=normalized,
        version=f"{major}.{minor}.{patch}",
        phase=_ID_TO_PHASE[phase_id],
        error_code=_ID_TO_ERROR.get(error_id),
        status=_ID_TO_STATUS[status_id],
        host_key_attempts=host_attempts,
        backup_attempts=backup_attempts,
        detail=_ID_TO_DETAIL[detail_id],
    )


def diagnostic_code_for_result(result: DeviceResult, *, version: str = __version__) -> str | None:
    """Return a code for a final failed result and no code for success/cancellation."""

    if result.status not in {DeviceStatus.FAILED, DeviceStatus.RETRY_EXHAUSTED}:
        return None
    return encode_diagnostic_code(
        version=version,
        phase=result.failure_phase,
        error_code=result.error_code,
        status=result.status,
        host_key_attempts=min(max(result.host_key_attempts, 0), 7),
        backup_attempts=min(max(result.attempts, 0), 7),
        detail=result.diagnostic_detail,
    )


def diagnostic_detail_for_exception(exc: BaseException) -> DiagnosticDetail:
    if isinstance(exc, MemoryError):
        return DiagnosticDetail.MEMORY_ERROR
    if isinstance(exc, ImportError):
        return DiagnosticDetail.IMPORT_ERROR
    if isinstance(exc, OSError):
        return DiagnosticDetail.OS_ERROR
    if isinstance(exc, (ValueError, TypeError)):
        return DiagnosticDetail.VALUE_OR_TYPE_ERROR
    if isinstance(exc, RuntimeError):
        return DiagnosticDetail.RUNTIME_ERROR
    return DiagnosticDetail.NONE


def diagnostic_code_for_exception(exc: BaseException, *, version: str = __version__) -> str:
    """Create an app-fatal code without encoding exception text or type names."""

    if isinstance(exc, CollectionFailure):
        phase = (
            exc.diagnostic_phase
            if exc.diagnostic_phase is not DiagnosticPhase.UNKNOWN
            else DiagnosticPhase.APP
        )
        detail = (
            exc.diagnostic_detail
            if exc.diagnostic_detail is not DiagnosticDetail.NONE
            else diagnostic_detail_for_exception(exc.__cause__ or exc)
        )
        error_code = exc.code
    else:
        phase = DiagnosticPhase.APP
        detail = diagnostic_detail_for_exception(exc)
        error_code = ErrorCode.UNEXPECTED_ERROR
    return encode_diagnostic_code(
        version=version,
        phase=phase,
        error_code=error_code,
        status=DiagnosticStatus.FATAL_APP,
        detail=detail,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Decode Aruba 2930F offline diagnostic codes")
    parser.add_argument("codes", nargs="+", help="One or more A3F1 diagnostic codes")
    parser.add_argument("--json", action="store_true", help="Print a JSON array")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        decoded = [decode_diagnostic_code(code) for code in args.codes]
    except ValueError as exc:
        print(f"diagnostic-code: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps([item.to_dict() for item in decoded], ensure_ascii=False))
    else:
        for item in decoded:
            error_name = item.error_code.value if item.error_code is not None else "UNKNOWN"
            print(
                f"{item.code}: version={item.version} phase={item.phase.value} "
                f"error={error_name} status={item.status.value} "
                f"host_key_attempts={item.host_key_attempts} "
                f"backup_attempts={item.backup_attempts} detail={item.detail.value}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
