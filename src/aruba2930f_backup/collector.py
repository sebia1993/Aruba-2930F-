"""Concurrent, retry-bounded orchestration for Aruba 2930F config collection."""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from math import isfinite

from .diagnostics import diagnostic_code_for_result, diagnostic_detail_for_exception
from .hostkeys import HostKeyProbe, HostKeyStore, ParamikoHostKeyProbe
from .models import (
    CollectionEvent,
    CollectionFailure,
    CollectionOptions,
    CollectionStage,
    Credentials,
    DeviceResult,
    DeviceStatus,
    DeviceTarget,
    DiagnosticDetail,
    DiagnosticPhase,
    ErrorCode,
    HostKeyCheck,
    HostKeyObservation,
    HostKeyTrustState,
)
from .ssh import NetmikoSessionFactory, SSHSession, SSHSessionFactory
from .validation import (
    hostname_from_running_config,
    normalize_config_text,
    require_valid_prompt,
    validate_device_identity,
    validate_output_limits,
)

EventCallback = Callable[[CollectionEvent], None]


class _CancellationView:
    def __init__(self, internal: threading.Event, external: threading.Event | None) -> None:
        self.internal = internal
        self.external = external

    def is_set(self) -> bool:
        return self.internal.is_set() or bool(self.external and self.external.is_set())

    def wait(self, delay_seconds: float) -> bool:
        """Return ``True`` when cancelled, including during a retry delay."""

        if self.is_set():
            return True
        deadline = time.monotonic() + delay_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self.is_set()
            if self.internal.wait(min(remaining, 0.05)) or self.is_set():
                return True


@dataclass(frozen=True, slots=True)
class _AttemptOutcome:
    result: DeviceResult
    retryable: bool = False


class ArubaCollector:
    """Coordinates host-key review and bounded, read-only device collection."""

    def __init__(
        self,
        session_factory: SSHSessionFactory | None = None,
        *,
        host_key_store: HostKeyStore | None = None,
        host_key_probe: HostKeyProbe | None = None,
    ) -> None:
        self.host_key_store = host_key_store or HostKeyStore()
        self.host_key_probe = host_key_probe or ParamikoHostKeyProbe()
        self.session_factory = session_factory or NetmikoSessionFactory(self.host_key_store)
        self._cancel_event = threading.Event()
        self._active_lock = threading.RLock()
        self._active_sessions: dict[str, SSHSession] = {}

    def begin_run(self) -> None:
        """Reset cancellation only at a new top-level run boundary."""

        with self._active_lock:
            if self._active_sessions:
                raise RuntimeError("A collection run is still active.")
            self._cancel_event.clear()

    def probe_host_keys(
        self,
        targets: Iterable[DeviceTarget],
        *,
        options: CollectionOptions | None = None,
        cancel_event: threading.Event | None = None,
        on_event: EventCallback | None = None,
    ) -> list[HostKeyCheck]:
        """Probe host keys in deferred rounds and preserve input order."""

        resolved_options = options or CollectionOptions()
        ordered_targets = tuple(targets)
        final_checks: list[HostKeyCheck | None] = [None] * len(ordered_targets)
        pending_indices = list(range(len(ordered_targets)))
        last_checks: dict[int, HostKeyCheck] = {}

        for attempt in range(1, resolved_options.max_attempts + 1):
            if not pending_indices:
                break
            if self._is_cancelled(cancel_event):
                raise CollectionFailure(ErrorCode.CANCELLED, "Host-key review was cancelled.")
            round_checks = self.probe_host_keys_round(
                (ordered_targets[index] for index in pending_indices),
                attempt=attempt,
                options=resolved_options,
                cancel_event=cancel_event,
                on_event=on_event,
            )
            next_pending: list[int] = []
            for index, check in zip(pending_indices, round_checks, strict=True):
                last_checks[index] = check
                if check.retryable and not check.retry_exhausted:
                    next_pending.append(index)
                else:
                    final_checks[index] = check
            if not next_pending:
                pending_indices = []
                break

            delay_seconds = resolved_options.retry_delays_seconds[attempt - 1]
            for index in next_pending:
                previous = last_checks[index]
                _emit(
                    on_event,
                    CollectionEvent(
                        target=ordered_targets[index],
                        stage=CollectionStage.RETRY_WAIT,
                        attempt=attempt + 1,
                        message="Waiting before the next host-key retry round.",
                        error_code=previous.error_code,
                        round=attempt + 1,
                        delay_seconds=delay_seconds,
                    ),
                )
            if not self.wait_for_retry_delay(delay_seconds, cancel_event=cancel_event):
                raise CollectionFailure(ErrorCode.CANCELLED, "Host-key review was cancelled.")
            pending_indices = next_pending

        for index, final_check in enumerate(final_checks):
            if final_check is None:
                final_checks[index] = last_checks[index]
        return [final_check for final_check in final_checks if final_check is not None]

    def probe_host_keys_round(
        self,
        targets: Iterable[DeviceTarget],
        *,
        attempt: int,
        options: CollectionOptions | None = None,
        cancel_event: threading.Event | None = None,
        on_event: EventCallback | None = None,
    ) -> list[HostKeyCheck]:
        """Probe every supplied target exactly once and preserve input order."""

        resolved_options = options or CollectionOptions()
        if not 1 <= attempt <= resolved_options.max_attempts:
            raise ValueError("Attempt must be within the configured attempt limit.")
        ordered_targets = tuple(targets)
        if not ordered_targets:
            return []
        if self._is_cancelled(cancel_event):
            raise CollectionFailure(ErrorCode.CANCELLED, "Host-key review was cancelled.")

        cancellation = _CancellationView(self._cancel_event, cancel_event)
        with ThreadPoolExecutor(
            max_workers=min(resolved_options.concurrency, len(ordered_targets)),
            thread_name_prefix="aruba2930f-hostkey",
        ) as executor:
            futures: list[Future[HostKeyCheck]] = [
                executor.submit(
                    self._probe_host_key_once,
                    target,
                    attempt,
                    resolved_options,
                    cancellation,
                    on_event,
                )
                for target in ordered_targets
            ]
            return [future.result() for future in futures]

    def wait_for_retry_delay(
        self,
        delay_seconds: float,
        *,
        cancel_event: threading.Event | None = None,
    ) -> bool:
        """Wait cooperatively; return ``False`` as soon as cancellation is seen."""

        if not isinstance(delay_seconds, (int, float)) or not isfinite(delay_seconds):
            raise ValueError("Retry delay must be a finite number.")
        if delay_seconds < 0:
            raise ValueError("Retry delay cannot be negative.")
        cancellation = _CancellationView(self._cancel_event, cancel_event)
        return not cancellation.wait(float(delay_seconds))

    def _probe_host_key_once(
        self,
        target: DeviceTarget,
        attempt: int,
        options: CollectionOptions,
        cancellation: _CancellationView,
        on_event: EventCallback | None,
    ) -> HostKeyCheck:
        _raise_if_cancelled(cancellation, message="Host-key review was cancelled.")
        _stage(on_event, target, CollectionStage.HOST_KEY_CHECKING, attempt)
        try:
            observation = self.host_key_probe.probe(
                target,
                timeout=options.connect_timeout_seconds,
            )
            _raise_if_cancelled(cancellation, message="Host-key review was cancelled.")
            checked = self.host_key_store.check(observation)
        except CollectionFailure as exc:
            if exc.code is ErrorCode.CANCELLED or cancellation.is_set():
                raise CollectionFailure(
                    ErrorCode.CANCELLED, "Host-key review was cancelled."
                ) from exc
            return _host_key_failure(target, attempt, options, exc, on_event)
        except Exception:
            return _host_key_failure(
                target,
                attempt,
                options,
                CollectionFailure(
                    ErrorCode.UNEXPECTED_ERROR,
                    "An unexpected internal error stopped host-key review.",
                ),
                on_event,
            )

        error_code = (
            ErrorCode.HOST_KEY_CHANGED
            if checked.state is HostKeyTrustState.CHANGED
            else checked.error_code
        )
        result = replace(
            checked,
            error_code=error_code,
            attempts=attempt,
            retryable=False,
            retry_exhausted=False,
        )
        if result.state is HostKeyTrustState.CHANGED:
            _emit(
                on_event,
                CollectionEvent(
                    target=target,
                    stage=CollectionStage.FAILED,
                    attempt=attempt,
                    message=result.message,
                    error_code=ErrorCode.HOST_KEY_CHANGED,
                    round=attempt,
                ),
            )
        return result

    def approve_host_keys(self, checks: Iterable[HostKeyCheck]) -> None:
        self.host_key_store.approve(checks)

    def cancel(self) -> None:
        """Stop assignment and close active sessions; safe to call from the GUI."""

        self._cancel_event.set()
        with self._active_lock:
            sessions = tuple(self._active_sessions.values())
        for session in sessions:
            with suppress(Exception):
                session.close()

    def collect_many(
        self,
        targets: Iterable[DeviceTarget],
        credentials: Credentials,
        options: CollectionOptions | None = None,
        *,
        cancel_event: threading.Event | None = None,
        on_event: EventCallback | None = None,
    ) -> list[DeviceResult]:
        """Collect one attempt per pending target in each deferred retry round."""

        resolved_options = options or CollectionOptions()
        ordered_targets = tuple(targets)
        if not ordered_targets:
            return []

        started_at = [datetime.now(UTC) for _target in ordered_targets]
        results: list[DeviceResult | None] = [None] * len(ordered_targets)
        pending_indices = list(range(len(ordered_targets)))
        previous_failures: dict[int, DeviceResult] = {}
        cancellation = _CancellationView(self._cancel_event, cancel_event)
        for target in ordered_targets:
            _emit(on_event, CollectionEvent(target, CollectionStage.QUEUED, 0, round=0))

        with ThreadPoolExecutor(
            max_workers=min(resolved_options.concurrency, len(ordered_targets)),
            thread_name_prefix="aruba2930f",
        ) as executor:
            for attempt in range(1, resolved_options.max_attempts + 1):
                if not pending_indices:
                    break
                if cancellation.is_set():
                    self._cancel_pending_results(
                        pending_indices,
                        ordered_targets,
                        results,
                        attempts=attempt - 1,
                        started_at=started_at,
                        on_event=on_event,
                    )
                    pending_indices = []
                    break

                if attempt > 1:
                    delay_seconds = resolved_options.retry_delays_seconds[attempt - 2]
                    for index in pending_indices:
                        previous = previous_failures[index]
                        _emit(
                            on_event,
                            CollectionEvent(
                                target=ordered_targets[index],
                                stage=CollectionStage.RETRY_WAIT,
                                attempt=attempt,
                                message="Waiting before the next backup retry round.",
                                error_code=previous.error_code,
                                round=attempt,
                                delay_seconds=delay_seconds,
                            ),
                        )
                    if cancellation.wait(delay_seconds):
                        self._cancel_pending_results(
                            pending_indices,
                            ordered_targets,
                            results,
                            attempts=attempt - 1,
                            started_at=started_at,
                            on_event=on_event,
                        )
                        pending_indices = []
                        break

                futures: dict[int, Future[_AttemptOutcome]] = {
                    index: executor.submit(
                        self._collect_attempt,
                        ordered_targets[index],
                        credentials,
                        resolved_options,
                        attempt,
                        started_at[index],
                        cancellation,
                        on_event,
                    )
                    for index in pending_indices
                }
                next_pending: list[int] = []
                for index in pending_indices:
                    outcome = futures[index].result()
                    if outcome.retryable:
                        previous_failures[index] = outcome.result
                        next_pending.append(index)
                    else:
                        results[index] = outcome.result
                pending_indices = next_pending

        for index, target in enumerate(ordered_targets):
            if results[index] is None:
                results[index] = _failed_result(
                    target,
                    resolved_options.max_attempts,
                    started_at[index],
                    ErrorCode.UNEXPECTED_ERROR,
                    "Collection ended without a result.",
                    on_event,
                )
        return [result for result in results if result is not None]

    def collect_one(
        self,
        target: DeviceTarget,
        credentials: Credentials,
        *,
        options: CollectionOptions | None = None,
        cancel_event: threading.Event | None = None,
        on_event: EventCallback | None = None,
    ) -> DeviceResult:
        """Collect one endpoint using the same deferred retry campaign."""

        return self.collect_many(
            [target],
            credentials,
            options,
            cancel_event=cancel_event,
            on_event=on_event,
        )[0]

    def _collect_attempt(
        self,
        target: DeviceTarget,
        credentials: Credentials,
        resolved_options: CollectionOptions,
        attempt_number: int,
        started_at: datetime,
        cancellation: _CancellationView,
        on_event: EventCallback | None,
    ) -> _AttemptOutcome:

        for attempt in (attempt_number,):
            attempt_warnings: list[str] = []
            failure_phase = DiagnosticPhase.CONNECT_AUTH
            if cancellation.is_set():
                result = _cancelled_result(target, attempts=attempt - 1, started_at=started_at)
                _emit(
                    on_event,
                    CollectionEvent(
                        target,
                        CollectionStage.CANCELLED,
                        attempt,
                        result.error_message,
                        round=attempt,
                    ),
                )
                return _AttemptOutcome(result)

            session: SSHSession | None = None
            try:
                session = self.session_factory.create(target, credentials, resolved_options)
                self._register_session(target, session)
                _stage(on_event, target, CollectionStage.CONNECTING, attempt)
                session.connect()
                _raise_if_cancelled(cancellation)
                if credentials.enable_secret:
                    failure_phase = DiagnosticPhase.SESSION_SETUP
                    _stage(on_event, target, CollectionStage.ENABLING, attempt)
                    session.enter_enable()
                    _raise_if_cancelled(cancellation)

                # Security/order invariant: no show command may move above these calls.
                failure_phase = DiagnosticPhase.SESSION_SETUP
                _stage(on_event, target, CollectionStage.DISABLING_PAGING, attempt)
                session.disable_paging()
                _raise_if_cancelled(cancellation)
                _stage(on_event, target, CollectionStage.SETTING_TERMINAL_WIDTH, attempt)
                session.set_terminal_width()
                _raise_if_cancelled(cancellation)

                failure_phase = DiagnosticPhase.DEVICE_IDENTITY
                _stage(on_event, target, CollectionStage.READING_VERSION, attempt)
                version_output = session.send_show("show version", cancel_event=cancellation)
                _raise_if_cancelled(cancellation)
                validate_output_limits(
                    version_output,
                    max_bytes=resolved_options.max_output_bytes,
                    max_lines=resolved_options.max_output_lines,
                )

                _stage(on_event, target, CollectionStage.READING_MODULES, attempt)
                try:
                    modules_output = session.send_show("show modules", cancel_event=cancellation)
                except CollectionFailure as exc:
                    if exc.code is not ErrorCode.COMMAND_REJECTED:
                        raise
                    # A rejected supplementary command is safe only if show version
                    # alone contains the exact family and one official SKU.
                    validate_device_identity(version_output)
                    modules_output = ""
                    attempt_warnings.append("SHOW_MODULES_UNAVAILABLE")
                _raise_if_cancelled(cancellation)

                _stage(on_event, target, CollectionStage.VALIDATING_MODEL, attempt)
                identity = validate_device_identity(version_output, modules_output)

                failure_phase = DiagnosticPhase.CONFIG_COLLECTION
                _stage(on_event, target, CollectionStage.READING_CONFIG, attempt)
                config_output = session.send_show("show running-config", cancel_event=cancellation)
                _raise_if_cancelled(cancellation)
                validate_output_limits(
                    config_output,
                    max_bytes=resolved_options.max_output_bytes,
                    max_lines=resolved_options.max_output_lines,
                )

                _stage(on_event, target, CollectionStage.VERIFYING_PROMPT, attempt)
                prompt_hostname = require_valid_prompt(session.get_prompt())
                _raise_if_cancelled(cancellation)
                config_text = normalize_config_text(config_output)
                hostname = prompt_hostname or hostname_from_running_config(config_text)
                identity = replace(identity, hostname=hostname)
                config_sha256 = hashlib.sha256(config_text.encode("utf-8")).hexdigest()
                finished_at = datetime.now(UTC)
                result = DeviceResult(
                    target=target,
                    status=DeviceStatus.SUCCESS,
                    attempts=attempt,
                    started_at=started_at,
                    finished_at=finished_at,
                    duration_seconds=(finished_at - started_at).total_seconds(),
                    hostname=identity.hostname,
                    model=identity.model,
                    sku=identity.sku,
                    software_version=identity.software_version,
                    config_text=config_text,
                    config_sha256=config_sha256,
                    warnings=tuple(dict.fromkeys(attempt_warnings)),
                )
                _emit(
                    on_event,
                    CollectionEvent(
                        target,
                        CollectionStage.COMPLETED,
                        attempt,
                        "Collection completed.",
                        round=attempt,
                    ),
                )
                return _AttemptOutcome(result)
            except CollectionFailure as exc:
                resolved_phase = (
                    exc.diagnostic_phase
                    if exc.diagnostic_phase is not DiagnosticPhase.UNKNOWN
                    else failure_phase
                )
                if exc.code is ErrorCode.CANCELLED or cancellation.is_set():
                    result = _cancelled_result(target, attempts=attempt, started_at=started_at)
                    _emit(
                        on_event,
                        CollectionEvent(
                            target,
                            CollectionStage.CANCELLED,
                            attempt,
                            result.error_message,
                            ErrorCode.CANCELLED,
                            round=attempt,
                        ),
                    )
                    return _AttemptOutcome(result)
                if exc.transient and attempt < resolved_options.max_attempts:
                    result = _failed_result(
                        target,
                        attempt,
                        started_at,
                        exc.code,
                        exc.safe_message,
                        None,
                        failure_phase=resolved_phase,
                        diagnostic_detail=exc.diagnostic_detail,
                    )
                    _emit(
                        on_event,
                        CollectionEvent(
                            target,
                            CollectionStage.RETRY_QUEUED,
                            attempt,
                            "A transient failure was queued for the next retry round.",
                            exc.code,
                            round=attempt,
                        ),
                    )
                    return _AttemptOutcome(result, retryable=True)
                status = DeviceStatus.RETRY_EXHAUSTED if exc.transient else DeviceStatus.FAILED
                message = (
                    "The retry limit was reached. " + exc.safe_message
                    if status is DeviceStatus.RETRY_EXHAUSTED
                    else exc.safe_message
                )
                return _AttemptOutcome(
                    _failed_result(
                        target,
                        attempt,
                        started_at,
                        exc.code,
                        message,
                        on_event,
                        status=status,
                        failure_phase=resolved_phase,
                        diagnostic_detail=exc.diagnostic_detail,
                    )
                )
            except Exception as exc:
                return _AttemptOutcome(
                    _failed_result(
                        target,
                        attempt,
                        started_at,
                        ErrorCode.UNEXPECTED_ERROR,
                        "An unexpected internal error stopped collection.",
                        on_event,
                        failure_phase=failure_phase,
                        diagnostic_detail=diagnostic_detail_for_exception(exc),
                    )
                )
            finally:
                if session is not None:
                    with suppress(Exception):
                        session.close()
                    self._unregister_session(target, session)

        # The loop always returns on success/final failure. This is defensive.
        return _AttemptOutcome(
            _failed_result(
                target,
                attempt,
                started_at,
                ErrorCode.UNEXPECTED_ERROR,
                "Collection ended without a result.",
                on_event,
            )
        )

    def _cancel_pending_results(
        self,
        indices: Iterable[int],
        targets: tuple[DeviceTarget, ...],
        results: list[DeviceResult | None],
        *,
        attempts: int,
        started_at: list[datetime],
        on_event: EventCallback | None,
    ) -> None:
        for index in indices:
            result = _cancelled_result(
                targets[index],
                attempts=attempts,
                started_at=started_at[index],
            )
            results[index] = result
            _emit(
                on_event,
                CollectionEvent(
                    targets[index],
                    CollectionStage.CANCELLED,
                    attempts,
                    result.error_message,
                    ErrorCode.CANCELLED,
                    round=attempts,
                ),
            )

    def _register_session(self, target: DeviceTarget, session: SSHSession) -> None:
        with self._active_lock:
            self._active_sessions[target.endpoint] = session

    def _unregister_session(self, target: DeviceTarget, session: SSHSession) -> None:
        with self._active_lock:
            if self._active_sessions.get(target.endpoint) is session:
                del self._active_sessions[target.endpoint]

    def _is_cancelled(self, external: threading.Event | None) -> bool:
        return self._cancel_event.is_set() or bool(external and external.is_set())


def _host_key_failure(
    target: DeviceTarget,
    attempt: int,
    options: CollectionOptions,
    failure: CollectionFailure,
    callback: EventCallback | None,
) -> HostKeyCheck:
    retryable = failure.transient
    retry_exhausted = failure.transient and attempt >= options.max_attempts
    stage = (
        CollectionStage.RETRY_QUEUED
        if retryable and not retry_exhausted
        else CollectionStage.FAILED
    )
    message = (
        "The host-key retry limit was reached. " + failure.safe_message
        if retry_exhausted
        else failure.safe_message
    )
    result = HostKeyCheck(
        observation=HostKeyObservation(target, "", ""),
        state=HostKeyTrustState.REJECTED,
        message=message,
        error_code=failure.code,
        attempts=attempt,
        retryable=retryable,
        retry_exhausted=retry_exhausted,
    )
    _emit(
        callback,
        CollectionEvent(
            target=target,
            stage=stage,
            attempt=attempt,
            message=message,
            error_code=failure.code,
            round=attempt,
        ),
    )
    return result


def _stage(
    callback: EventCallback | None,
    target: DeviceTarget,
    stage: CollectionStage,
    attempt: int,
) -> None:
    _emit(callback, CollectionEvent(target, stage, attempt, round=attempt))


def _raise_if_cancelled(
    cancellation: _CancellationView,
    *,
    message: str = "Collection was cancelled.",
) -> None:
    if cancellation.is_set():
        raise CollectionFailure(ErrorCode.CANCELLED, message)


def _emit(callback: EventCallback | None, event: CollectionEvent) -> None:
    if callback is None:
        return
    # UI callbacks are observers and must not alter device outcomes.
    with suppress(Exception):
        callback(event)


def _cancelled_result(
    target: DeviceTarget,
    *,
    attempts: int,
    started_at: datetime | None = None,
) -> DeviceResult:
    started = started_at or datetime.now(UTC)
    finished = datetime.now(UTC)
    return DeviceResult(
        target=target,
        status=DeviceStatus.CANCELLED,
        attempts=attempts,
        started_at=started,
        finished_at=finished,
        duration_seconds=(finished - started).total_seconds(),
        error_code=ErrorCode.CANCELLED,
        error_message="Collection was cancelled.",
    )


def _failed_result(
    target: DeviceTarget,
    attempt: int,
    started_at: datetime,
    error_code: ErrorCode,
    message: str,
    callback: EventCallback | None,
    *,
    status: DeviceStatus = DeviceStatus.FAILED,
    failure_phase: DiagnosticPhase = DiagnosticPhase.UNKNOWN,
    diagnostic_detail: DiagnosticDetail = DiagnosticDetail.NONE,
) -> DeviceResult:
    finished = datetime.now(UTC)
    result = DeviceResult(
        target=target,
        status=status,
        attempts=attempt,
        started_at=started_at,
        finished_at=finished,
        duration_seconds=(finished - started_at).total_seconds(),
        error_code=error_code,
        error_message=message,
        failure_phase=failure_phase,
        diagnostic_detail=diagnostic_detail,
    )
    result.diagnostic_code = diagnostic_code_for_result(result)
    _emit(
        callback,
        CollectionEvent(
            target,
            CollectionStage.FAILED,
            attempt,
            message,
            error_code,
            round=attempt,
        ),
    )
    return result
