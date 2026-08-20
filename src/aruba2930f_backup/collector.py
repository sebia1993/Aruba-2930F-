"""Concurrent, retry-bounded orchestration for Aruba 2930F config collection."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime

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
    ErrorCode,
    HostKeyCheck,
    HostKeyObservation,
    HostKeyTrustState,
)
from .ssh import NetmikoSessionFactory, SSHSession, SSHSessionFactory
from .validation import (
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
    ) -> list[HostKeyCheck]:
        """Probe before credentials are used, returning reviewable trust states."""

        resolved_options = options or CollectionOptions()
        checks: list[HostKeyCheck] = []
        for target in targets:
            for attempt in range(1, resolved_options.max_attempts + 1):
                if self._is_cancelled(cancel_event):
                    raise CollectionFailure(ErrorCode.CANCELLED, "Host-key review was cancelled.")
                try:
                    observation = self.host_key_probe.probe(
                        target,
                        timeout=resolved_options.connect_timeout_seconds,
                    )
                except CollectionFailure as exc:
                    if exc.code is ErrorCode.CANCELLED:
                        raise
                    if exc.transient and attempt < resolved_options.max_attempts:
                        continue
                    checks.append(
                        HostKeyCheck(
                            observation=HostKeyObservation(target, "", ""),
                            state=HostKeyTrustState.REJECTED,
                            message=exc.safe_message,
                            error_code=exc.code,
                            attempts=attempt,
                        )
                    )
                    break
                else:
                    checks.append(replace(self.host_key_store.check(observation), attempts=attempt))
                    break
        return checks

    def approve_host_keys(self, checks: Iterable[HostKeyCheck]) -> None:
        self.host_key_store.approve(checks)

    def cancel(self) -> None:
        """Stop assignment and close active sessions; safe to call from the GUI."""

        self._cancel_event.set()
        with self._active_lock:
            sessions = tuple(self._active_sessions.values())
        for session in sessions:
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
        """Collect with at most ``concurrency`` active jobs and preserve input order."""

        resolved_options = options or CollectionOptions()
        ordered_targets = tuple(targets)
        if not ordered_targets:
            return []

        results: list[DeviceResult | None] = [None] * len(ordered_targets)
        next_index = 0
        futures: dict[Future[DeviceResult], int] = {}

        with ThreadPoolExecutor(
            max_workers=min(resolved_options.concurrency, len(ordered_targets)),
            thread_name_prefix="aruba2930f",
        ) as executor:
            while next_index < len(ordered_targets) and len(futures) < resolved_options.concurrency:
                if self._is_cancelled(cancel_event):
                    break
                futures[
                    executor.submit(
                        self.collect_one,
                        ordered_targets[next_index],
                        credentials,
                        options=resolved_options,
                        cancel_event=cancel_event,
                        on_event=on_event,
                    )
                ] = next_index
                next_index += 1

            while futures:
                done, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
                for future in done:
                    index = futures.pop(future)
                    results[index] = future.result()

                if self._is_cancelled(cancel_event):
                    self.cancel()
                    continue
                while (
                    next_index < len(ordered_targets)
                    and len(futures) < resolved_options.concurrency
                ):
                    futures[
                        executor.submit(
                            self.collect_one,
                            ordered_targets[next_index],
                            credentials,
                            options=resolved_options,
                            cancel_event=cancel_event,
                            on_event=on_event,
                        )
                    ] = next_index
                    next_index += 1

        for index, target in enumerate(ordered_targets):
            if results[index] is None:
                results[index] = _cancelled_result(target, attempts=0)
                _emit(
                    on_event,
                    CollectionEvent(
                        target, CollectionStage.CANCELLED, 0, "Collection was cancelled."
                    ),
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
        resolved_options = options or CollectionOptions()
        started_at = datetime.now(UTC)
        cancellation = _CancellationView(self._cancel_event, cancel_event)

        for attempt in range(1, resolved_options.max_attempts + 1):
            attempt_warnings: list[str] = []
            if cancellation.is_set():
                result = _cancelled_result(target, attempts=attempt - 1, started_at=started_at)
                _emit(
                    on_event,
                    CollectionEvent(
                        target, CollectionStage.CANCELLED, attempt, result.error_message
                    ),
                )
                return result

            session: SSHSession | None = None
            try:
                session = self.session_factory.create(target, credentials, resolved_options)
                self._register_session(target, session)
                _stage(on_event, target, CollectionStage.CONNECTING, attempt)
                session.connect()
                _raise_if_cancelled(cancellation)
                if credentials.enable_secret:
                    _stage(on_event, target, CollectionStage.ENABLING, attempt)
                    session.enter_enable()
                    _raise_if_cancelled(cancellation)

                # Security/order invariant: no show command may move above these calls.
                _stage(on_event, target, CollectionStage.DISABLING_PAGING, attempt)
                session.disable_paging()
                _raise_if_cancelled(cancellation)
                _stage(on_event, target, CollectionStage.SETTING_TERMINAL_WIDTH, attempt)
                session.set_terminal_width()
                _raise_if_cancelled(cancellation)

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

                _stage(on_event, target, CollectionStage.READING_CONFIG, attempt)
                config_output = session.send_show("show running-config", cancel_event=cancellation)
                _raise_if_cancelled(cancellation)
                validate_output_limits(
                    config_output,
                    max_bytes=resolved_options.max_output_bytes,
                    max_lines=resolved_options.max_output_lines,
                )

                _stage(on_event, target, CollectionStage.VERIFYING_PROMPT, attempt)
                hostname = require_valid_prompt(session.get_prompt())
                identity = replace(identity, hostname=hostname)
                config_text = normalize_config_text(config_output)
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
                        target, CollectionStage.COMPLETED, attempt, "Collection completed."
                    ),
                )
                return result
            except CollectionFailure as exc:
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
                        ),
                    )
                    return result
                if exc.transient and attempt < resolved_options.max_attempts:
                    _emit(
                        on_event,
                        CollectionEvent(
                            target,
                            CollectionStage.RETRYING,
                            attempt,
                            "A transient failure occurred; reconnecting.",
                            exc.code,
                        ),
                    )
                    continue
                return _failed_result(
                    target,
                    attempt,
                    started_at,
                    exc.code,
                    exc.safe_message,
                    on_event,
                )
            except Exception:
                return _failed_result(
                    target,
                    attempt,
                    started_at,
                    ErrorCode.UNEXPECTED_ERROR,
                    "An unexpected internal error stopped collection.",
                    on_event,
                )
            finally:
                if session is not None:
                    session.close()
                    self._unregister_session(target, session)

        # The loop always returns on success/final failure. This is defensive.
        return _failed_result(
            target,
            resolved_options.max_attempts,
            started_at,
            ErrorCode.UNEXPECTED_ERROR,
            "Collection ended without a result.",
            on_event,
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


def _stage(
    callback: EventCallback | None,
    target: DeviceTarget,
    stage: CollectionStage,
    attempt: int,
) -> None:
    _emit(callback, CollectionEvent(target, stage, attempt))


def _raise_if_cancelled(cancellation: _CancellationView) -> None:
    if cancellation.is_set():
        raise CollectionFailure(ErrorCode.CANCELLED, "Collection was cancelled.")


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
) -> DeviceResult:
    finished = datetime.now(UTC)
    result = DeviceResult(
        target=target,
        status=DeviceStatus.FAILED,
        attempts=attempt,
        started_at=started_at,
        finished_at=finished,
        duration_seconds=(finished - started_at).total_seconds(),
        error_code=error_code,
        error_message=message,
    )
    _emit(
        callback,
        CollectionEvent(target, CollectionStage.FAILED, attempt, message, error_code),
    )
    return result
