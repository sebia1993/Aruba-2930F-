"""Shared, dependency-free models for the collection service.

The credential object deliberately suppresses its secret fields from ``repr``.
It is an in-memory transport object only; no serialization helper is provided.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from ipaddress import IPv4Address
from math import isfinite
from pathlib import Path
from typing import Final


class ErrorCode(StrEnum):
    INPUT_INVALID = "INPUT_INVALID"
    HOST_KEY_REJECTED = "HOST_KEY_REJECTED"
    HOST_KEY_CHANGED = "HOST_KEY_CHANGED"
    TCP_TIMEOUT = "TCP_TIMEOUT"
    SSH_ALGORITHM_INCOMPATIBLE = "SSH_ALGORITHM_INCOMPATIBLE"
    SSH_NEGOTIATION_FAILED = "SSH_NEGOTIATION_FAILED"
    AUTH_FAILED = "AUTH_FAILED"
    ENABLE_FAILED = "ENABLE_FAILED"
    PAGING_SETUP_FAILED = "PAGING_SETUP_FAILED"
    MODEL_UNSUPPORTED = "MODEL_UNSUPPORTED"
    COMMAND_TIMEOUT = "COMMAND_TIMEOUT"
    COMMAND_REJECTED = "COMMAND_REJECTED"
    PROMPT_PARSE_FAILED = "PROMPT_PARSE_FAILED"
    OUTPUT_LIMIT_EXCEEDED = "OUTPUT_LIMIT_EXCEEDED"
    REPORT_WRITE_FAILED = "REPORT_WRITE_FAILED"
    CANCELLED = "CANCELLED"
    UNEXPECTED_ERROR = "UNEXPECTED_ERROR"


class DiagnosticPhase(StrEnum):
    APP = "app"
    HOST_KEY = "host_key"
    CONNECT_AUTH = "connect_auth"
    SESSION_SETUP = "session_setup"
    DEVICE_IDENTITY = "device_identity"
    CONFIG_COLLECTION = "config_collection"
    REPORT_STORAGE = "report_storage"
    UNKNOWN = "unknown"


class DiagnosticDetail(StrEnum):
    NONE = "none"
    LOGIN_BANNER_PENDING = "login_banner_pending"
    PROMPT_EMPTY = "prompt_empty"
    PROMPT_FORMAT = "prompt_format"
    PROMPT_NON_EXEC_MODE = "prompt_non_exec_mode"
    PROMPT_MISMATCH = "prompt_mismatch"
    PROMPT_READ_ERROR = "prompt_read_error"
    IMPORT_ERROR = "import_error"
    OS_ERROR = "os_error"
    VALUE_OR_TYPE_ERROR = "value_or_type_error"
    RUNTIME_ERROR = "runtime_error"
    MEMORY_ERROR = "memory_error"


class DeviceStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    RETRY_EXHAUSTED = "retry_exhausted"
    CANCELLED = "cancelled"


class CollectionStage(StrEnum):
    QUEUED = "queued"
    HOST_KEY_CHECKING = "host_key_checking"
    CONNECTING = "connecting"
    ENABLING = "enabling"
    DISABLING_PAGING = "disabling_paging"
    SETTING_TERMINAL_WIDTH = "setting_terminal_width"
    READING_VERSION = "reading_version"
    READING_MODULES = "reading_modules"
    VALIDATING_MODEL = "validating_model"
    READING_CONFIG = "reading_config"
    VERIFYING_PROMPT = "verifying_prompt"
    RETRYING = "retrying"
    RETRY_QUEUED = "retry_queued"
    RETRY_WAIT = "retry_wait"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class HostKeyTrustState(StrEnum):
    TRUSTED = "trusted"
    UNKNOWN = "unknown"
    CHANGED = "changed"
    REJECTED = "rejected"


class CollectionFailure(Exception):
    """A sanitized, operator-visible failure crossing a service boundary."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        transient: bool = False,
        diagnostic_phase: DiagnosticPhase = DiagnosticPhase.UNKNOWN,
        diagnostic_detail: DiagnosticDetail = DiagnosticDetail.NONE,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.transient = transient
        self.diagnostic_phase = diagnostic_phase
        self.diagnostic_detail = diagnostic_detail


@dataclass(frozen=True, slots=True)
class DeviceTarget:
    ip: str
    port: int = 22

    def __post_init__(self) -> None:
        try:
            normalized = str(IPv4Address(self.ip.strip()))
        except (AttributeError, ValueError) as exc:
            raise ValueError("A valid IPv4 address is required.") from exc
        if not 1 <= self.port <= 65535:
            raise ValueError("SSH port must be between 1 and 65535.")
        object.__setattr__(self, "ip", normalized)

    @property
    def endpoint(self) -> str:
        return f"{self.ip}:{self.port}"


@dataclass(frozen=True, slots=True)
class Credentials:
    username: str
    password: str = field(repr=False)
    enable_secret: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.username.strip():
            raise ValueError("Username is required.")
        if not self.password:
            raise ValueError("Password is required.")


@dataclass(frozen=True, slots=True)
class CollectionOptions:
    concurrency: int = 10
    max_attempts: int = 4
    connect_timeout_seconds: float = 15.0
    command_timeout_seconds: float = 180.0
    max_output_bytes: int = 20 * 1024 * 1024
    max_output_lines: int = 250_000
    max_pager_advances: int = 10_000
    retry_delays_seconds: tuple[float, ...] = (5.0, 15.0, 30.0)

    def __post_init__(self) -> None:
        if not 1 <= self.concurrency <= 20:
            raise ValueError("Concurrency must be between 1 and 20.")
        if not 1 <= self.max_attempts <= 4:
            raise ValueError("Max attempts must be between 1 and 4.")
        if self.connect_timeout_seconds <= 0 or self.command_timeout_seconds <= 0:
            raise ValueError("Timeouts must be positive.")
        if self.max_output_bytes <= 0 or self.max_output_lines <= 0:
            raise ValueError("Output limits must be positive.")
        if self.max_pager_advances < 0:
            raise ValueError("Pager advance limit cannot be negative.")
        if len(self.retry_delays_seconds) < self.max_attempts - 1:
            raise ValueError("Retry delays must cover every retry attempt.")
        if any(
            not isinstance(delay, (int, float)) or not isfinite(delay) or delay < 0
            for delay in self.retry_delays_seconds
        ):
            raise ValueError("Retry delays must be finite, non-negative numbers.")


@dataclass(frozen=True, slots=True)
class HostKeyObservation:
    target: DeviceTarget
    key_type: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class HostKeyCheck:
    observation: HostKeyObservation
    state: HostKeyTrustState
    known_fingerprint: str | None = None
    message: str = ""
    error_code: ErrorCode | None = None
    attempts: int = 1
    retryable: bool = False
    retry_exhausted: bool = False

    @property
    def target(self) -> DeviceTarget:
        return self.observation.target


@dataclass(frozen=True, slots=True)
class ApprovedHostKey:
    endpoint: str
    key_type: str
    fingerprint: str
    approved_at: str


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    hostname: str | None
    model: str
    sku: str
    software_version: str | None = None


@dataclass(frozen=True, slots=True)
class CollectionEvent:
    target: DeviceTarget
    stage: CollectionStage
    attempt: int
    message: str = ""
    error_code: ErrorCode | None = None
    round: int | None = None
    delay_seconds: float | None = None


@dataclass(slots=True)
class DeviceResult:
    target: DeviceTarget
    status: DeviceStatus
    attempts: int
    started_at: datetime
    finished_at: datetime
    duration_seconds: float
    hostname: str | None = None
    model: str | None = None
    sku: str | None = None
    software_version: str | None = None
    config_text: str | None = field(default=None, repr=False)
    config_path: Path | None = None
    config_sha256: str | None = None
    error_code: ErrorCode | None = None
    error_message: str = ""
    warnings: tuple[str, ...] = ()
    host_key_attempts: int = 0
    failure_phase: DiagnosticPhase = DiagnosticPhase.UNKNOWN
    diagnostic_detail: DiagnosticDetail = DiagnosticDetail.NONE
    diagnostic_code: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is DeviceStatus.SUCCESS


DEFAULT_OPTIONS: Final = CollectionOptions()
