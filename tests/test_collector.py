from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import pytest
from tests.fakes import MODULES_2930F, RUNNING_CONFIG, ScriptedFactory, ScriptedSession

from aruba2930f_backup.collector import ArubaCollector
from aruba2930f_backup.diagnostics import decode_diagnostic_code
from aruba2930f_backup.hostkeys import HostKeyStore, sha256_fingerprint
from aruba2930f_backup.models import (
    CollectionFailure,
    CollectionOptions,
    CollectionStage,
    Credentials,
    DeviceStatus,
    DeviceTarget,
    DiagnosticDetail,
    DiagnosticPhase,
    ErrorCode,
    HostKeyObservation,
    HostKeyTrustState,
)

TARGET = DeviceTarget("192.0.2.40")
CREDENTIALS = Credentials("operator", "session-password", "enable-secret")
FAST_RETRY_OPTIONS = CollectionOptions(retry_delays_seconds=(0.0, 0.0, 0.0))


def test_collection_enforces_exact_command_order_and_normalizes_hash() -> None:
    factory = ScriptedFactory()
    collector = ArubaCollector(factory)

    result = collector.collect_one(TARGET, CREDENTIALS, options=FAST_RETRY_OPTIONS)

    assert result.status is DeviceStatus.SUCCESS
    assert factory.sessions[0].calls == [
        "connect",
        "enable",
        "no page",
        "terminal width 511",
        "show version",
        "show modules",
        "show running-config",
        "get prompt",
        "close",
    ]
    assert factory.sessions[0].calls.count("show running-config") == 1
    assert result.config_text == RUNNING_CONFIG.replace("\n", "\r\n")
    assert result.config_sha256 == hashlib.sha256(result.config_text.encode("utf-8")).hexdigest()
    assert result.hostname == "edge-lab"
    assert result.sku == "JL253A"


def test_complex_prompt_uses_running_config_hostname_without_extra_ssh_round_trip() -> None:
    factory = ScriptedFactory(
        lambda _index, target: ScriptedSession(
            target,
            prompt="(Aruba 2930F PoE+) #",
            responses={"show running-config": 'Running configuration:\nhostname "config-edge"\n'},
        )
    )

    result = ArubaCollector(factory).collect_one(
        TARGET,
        CREDENTIALS,
        options=FAST_RETRY_OPTIONS,
    )

    assert result.status is DeviceStatus.SUCCESS
    assert result.hostname == "config-edge"
    assert factory.sessions[0].calls == [
        "connect",
        "enable",
        "no page",
        "terminal width 511",
        "show version",
        "show modules",
        "show running-config",
        "get prompt",
        "close",
    ]
    assert factory.sessions[0].calls.count("show running-config") == 1


def test_simple_prompt_hostname_takes_precedence_over_running_config() -> None:
    factory = ScriptedFactory(
        lambda _index, target: ScriptedSession(
            target,
            prompt="prompt-edge#",
            responses={"show running-config": 'hostname "config-edge"\n'},
        )
    )

    result = ArubaCollector(factory).collect_one(
        TARGET,
        CREDENTIALS,
        options=FAST_RETRY_OPTIONS,
    )

    assert result.hostname == "prompt-edge"


def test_missing_prompt_and_config_hostname_preserves_ip_filename_fallback() -> None:
    factory = ScriptedFactory(
        lambda _index, target: ScriptedSession(
            target,
            prompt="(Aruba 2930F PoE+) #",
            responses={"show running-config": "Running configuration:\nvlan 1\n"},
        )
    )

    result = ArubaCollector(factory).collect_one(
        TARGET,
        CREDENTIALS,
        options=FAST_RETRY_OPTIONS,
    )

    assert result.hostname is None


def test_no_enable_secret_skips_enable_but_keeps_no_page_first() -> None:
    factory = ScriptedFactory()
    collector = ArubaCollector(factory)

    collector.collect_one(TARGET, Credentials("operator", "password"))

    calls = factory.sessions[0].calls
    assert "enable" not in calls
    assert calls.index("no page") < calls.index("show version")


def test_transient_failure_reconnects_up_to_four_attempts_and_reapplies_no_page() -> None:
    def builder(index: int, target: DeviceTarget) -> ScriptedSession:
        failures = {}
        if index < 3:
            failures["show running-config"] = CollectionFailure(
                ErrorCode.COMMAND_TIMEOUT,
                "Command timed out.",
                transient=True,
            )
        return ScriptedSession(target, failure_by_command=failures)

    factory = ScriptedFactory(builder)
    collector = ArubaCollector(factory)

    result = collector.collect_one(TARGET, CREDENTIALS, options=FAST_RETRY_OPTIONS)

    assert result.status is DeviceStatus.SUCCESS
    assert result.attempts == 4
    assert len(factory.sessions) == 4
    assert all(session.calls.count("no page") == 1 for session in factory.sessions)
    assert all(session.calls.count("show running-config") == 1 for session in factory.sessions)
    assert all(session.closed for session in factory.sessions)


def test_collect_many_defers_retry_until_every_target_finishes_current_round() -> None:
    targets = [DeviceTarget("192.0.2.51"), DeviceTarget("192.0.2.52")]
    attempts_by_endpoint = {target.endpoint: 0 for target in targets}

    def builder(index: int, target: DeviceTarget) -> ScriptedSession:
        del index
        attempts_by_endpoint[target.endpoint] += 1
        failures = (
            {
                "connect": CollectionFailure(
                    ErrorCode.TCP_TIMEOUT,
                    "Connection timed out.",
                    transient=True,
                )
            }
            if attempts_by_endpoint[target.endpoint] == 1
            else {}
        )
        return ScriptedSession(target, failure_by_command=failures)

    factory = ScriptedFactory(builder)
    results = ArubaCollector(factory).collect_many(
        targets,
        CREDENTIALS,
        CollectionOptions(
            concurrency=1,
            max_attempts=2,
            retry_delays_seconds=(0.0,),
        ),
    )

    assert [session.target for session in factory.sessions] == [*targets, *targets]
    assert [result.status for result in results] == [DeviceStatus.SUCCESS, DeviceStatus.SUCCESS]
    assert [result.attempts for result in results] == [2, 2]
    assert all(result.host_key_attempts == 0 for result in results)


def test_transient_failure_after_four_rounds_is_retry_exhausted() -> None:
    factory = ScriptedFactory(
        lambda index, target: ScriptedSession(
            target,
            failure_by_command={
                "connect": CollectionFailure(
                    ErrorCode.TCP_TIMEOUT,
                    "Connection timed out.",
                    transient=True,
                )
            },
        )
    )

    result = ArubaCollector(factory).collect_one(
        TARGET,
        CREDENTIALS,
        options=FAST_RETRY_OPTIONS,
    )

    assert result.status is DeviceStatus.RETRY_EXHAUSTED
    assert result.error_code is ErrorCode.TCP_TIMEOUT
    assert result.attempts == 4
    assert len(factory.sessions) == 4


def test_cancel_during_retry_wait_is_prompt_and_preserves_completed_attempt_count() -> None:
    wait_started = threading.Event()
    cancelled = threading.Event()
    events = []
    factory = ScriptedFactory(
        lambda index, target: ScriptedSession(
            target,
            failure_by_command={
                "connect": CollectionFailure(
                    ErrorCode.TCP_TIMEOUT,
                    "Connection timed out.",
                    transient=True,
                )
            },
        )
    )
    collector = ArubaCollector(factory)
    holder = []

    def on_event(event: object) -> None:
        events.append(event)
        if getattr(event, "stage", None) is CollectionStage.RETRY_WAIT:
            wait_started.set()

    worker = threading.Thread(
        target=lambda: holder.append(
            collector.collect_one(
                TARGET,
                CREDENTIALS,
                options=CollectionOptions(retry_delays_seconds=(5.0, 0.0, 0.0)),
                cancel_event=cancelled,
                on_event=on_event,
            )
        ),
        daemon=True,
    )
    worker.start()
    assert wait_started.wait(timeout=1)
    cancelled.set()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert holder[0].status is DeviceStatus.CANCELLED
    assert holder[0].attempts == 1
    assert len(factory.sessions) == 1
    retry_wait = next(event for event in events if event.stage is CollectionStage.RETRY_WAIT)
    assert retry_wait.round == 2
    assert retry_wait.delay_seconds == 5.0
    assert retry_wait.error_code is ErrorCode.TCP_TIMEOUT


def test_non_transient_authentication_failure_is_not_retried() -> None:
    factory = ScriptedFactory(
        lambda index, target: ScriptedSession(
            target,
            failure_by_command={
                "connect": CollectionFailure(ErrorCode.AUTH_FAILED, "SSH authentication failed.")
            },
        )
    )

    result = ArubaCollector(factory).collect_one(TARGET, CREDENTIALS)

    assert result.status is DeviceStatus.FAILED
    assert result.error_code is ErrorCode.AUTH_FAILED
    assert result.attempts == 1
    assert len(factory.sessions) == 1


def test_no_page_failure_prevents_every_show_command() -> None:
    factory = ScriptedFactory(
        lambda index, target: ScriptedSession(
            target,
            failure_by_command={
                "no page": CollectionFailure(
                    ErrorCode.PAGING_SETUP_FAILED,
                    "The device did not accept and verify 'no page'.",
                )
            },
        )
    )

    result = ArubaCollector(factory).collect_one(TARGET, CREDENTIALS)

    assert result.error_code is ErrorCode.PAGING_SETUP_FAILED
    assert result.failure_phase is DiagnosticPhase.SESSION_SETUP
    assert result.diagnostic_code is not None
    assert not any(call.startswith("show ") for call in factory.sessions[0].calls)


def test_prompt_failure_records_config_phase_detail_and_retry_count() -> None:
    factory = ScriptedFactory(
        lambda index, target: ScriptedSession(
            target,
            failure_by_command={
                "get prompt": CollectionFailure(
                    ErrorCode.PROMPT_PARSE_FAILED,
                    "The final device prompt could not be verified.",
                    transient=True,
                    diagnostic_detail=DiagnosticDetail.PROMPT_FORMAT,
                )
            },
        )
    )

    result = ArubaCollector(factory).collect_one(TARGET, CREDENTIALS, options=FAST_RETRY_OPTIONS)

    assert result.status is DeviceStatus.RETRY_EXHAUSTED
    assert result.failure_phase is DiagnosticPhase.CONFIG_COLLECTION
    assert result.diagnostic_detail is DiagnosticDetail.PROMPT_FORMAT
    assert result.diagnostic_code is not None
    decoded = decode_diagnostic_code(result.diagnostic_code)
    assert decoded.phase is DiagnosticPhase.CONFIG_COLLECTION
    assert decoded.backup_attempts == 4


def test_rejected_show_modules_is_warning_only_with_exact_version_evidence() -> None:
    factory = ScriptedFactory(
        lambda index, target: ScriptedSession(
            target,
            failure_by_command={
                "show modules": CollectionFailure(
                    ErrorCode.COMMAND_REJECTED,
                    "The device rejected 'show modules'.",
                )
            },
        )
    )

    result = ArubaCollector(factory).collect_one(TARGET, CREDENTIALS)

    assert result.status is DeviceStatus.SUCCESS
    assert result.warnings == ("SHOW_MODULES_UNAVAILABLE",)
    assert factory.sessions[0].calls.count("show running-config") == 1


def test_rejected_show_modules_is_warning_only_with_family_evidence() -> None:
    factory = ScriptedFactory(
        lambda index, target: ScriptedSession(
            target,
            responses={"show version": "Aruba 2930F Switch\nWC.16.11.0025"},
            failure_by_command={
                "show modules": CollectionFailure(
                    ErrorCode.COMMAND_REJECTED,
                    "The device rejected 'show modules'.",
                )
            },
        )
    )

    result = ArubaCollector(factory).collect_one(TARGET, CREDENTIALS)

    assert result.status is DeviceStatus.SUCCESS
    assert result.model == "Aruba 2930F"
    assert result.sku is None
    assert result.warnings == ("SHOW_MODULES_UNAVAILABLE",)
    assert factory.sessions[0].calls.count("show running-config") == 1


def test_rejected_modules_cannot_rescue_generic_version_output() -> None:
    factory = ScriptedFactory(
        lambda index, target: ScriptedSession(
            target,
            responses={"show version": "ArubaOS-Switch WC.16.11.0025"},
            failure_by_command={
                "show modules": CollectionFailure(
                    ErrorCode.COMMAND_REJECTED,
                    "The device rejected 'show modules'.",
                )
            },
        )
    )

    result = ArubaCollector(factory).collect_one(TARGET, CREDENTIALS)

    assert result.error_code is ErrorCode.MODEL_UNSUPPORTED
    assert result.diagnostic_detail is DiagnosticDetail.IDENTITY_EVIDENCE_MISSING
    assert result.diagnostic_code is not None
    assert (
        decode_diagnostic_code(result.diagnostic_code).detail
        is DiagnosticDetail.IDENTITY_EVIDENCE_MISSING
    )
    assert "show running-config" not in factory.sessions[0].calls


def test_jl255a_from_modules_allows_exactly_one_running_config_read() -> None:
    factory = ScriptedFactory(
        lambda index, target: ScriptedSession(
            target,
            responses={
                "show version": "Image stamp:\nWC.16.10.0024\nBoot Image: Primary",
                "show modules": (
                    "Status and Counters - Module Information\n"
                    "Chassis: Aruba 2930F-24G-PoE+-4SFP+ JL255A"
                ),
            },
        )
    )

    result = ArubaCollector(factory).collect_one(TARGET, CREDENTIALS)

    assert result.status is DeviceStatus.SUCCESS
    assert result.model == "Aruba 2930F 24G PoE+ 4SFP+"
    assert result.sku == "JL255A"
    assert factory.sessions[0].calls.count("show running-config") == 1


def test_unsupported_model_is_blocked_before_running_config() -> None:
    factory = ScriptedFactory(
        lambda index, target: ScriptedSession(
            target,
            responses={
                "show version": "Aruba 2930M JL253A Switch",
                "show modules": MODULES_2930F,
            },
        )
    )

    result = ArubaCollector(factory).collect_one(TARGET, CREDENTIALS)

    assert result.error_code is ErrorCode.MODEL_UNSUPPORTED
    assert result.diagnostic_detail is DiagnosticDetail.IDENTITY_FAMILY_CONFLICT
    assert "show running-config" not in factory.sessions[0].calls


def test_pre_cancelled_batch_opens_no_sessions_and_preserves_target_order() -> None:
    targets = [DeviceTarget("192.0.2.3"), DeviceTarget("192.0.2.1")]
    cancelled = threading.Event()
    cancelled.set()
    factory = ScriptedFactory()

    results = ArubaCollector(factory).collect_many(
        targets,
        CREDENTIALS,
        CollectionOptions(concurrency=1),
        cancel_event=cancelled,
    )

    assert [result.target for result in results] == targets
    assert all(result.status is DeviceStatus.CANCELLED for result in results)
    assert factory.sessions == []


def test_cancel_closes_an_active_session() -> None:
    entered = threading.Event()
    closed = threading.Event()

    class BlockingSession(ScriptedSession):
        def connect(self) -> None:
            self.calls.append("connect")
            entered.set()
            closed.wait(timeout=2)

        def close(self) -> None:
            super().close()
            closed.set()

    factory = ScriptedFactory(lambda index, target: BlockingSession(target))
    collector = ArubaCollector(factory)
    holder: list[object] = []

    worker = threading.Thread(
        target=lambda: holder.extend(collector.collect_many([TARGET], CREDENTIALS)),
        daemon=True,
    )
    worker.start()
    assert entered.wait(timeout=1)
    collector.cancel()
    worker.join(timeout=3)

    assert not worker.is_alive()
    assert closed.is_set()
    assert factory.sessions[0].closed
    assert holder[0].status is DeviceStatus.CANCELLED  # type: ignore[union-attr]


def test_events_cover_retry_and_completion_without_exposing_credentials() -> None:
    factory = ScriptedFactory(
        lambda index, target: ScriptedSession(
            target,
            failure_by_command=(
                {
                    "show version": CollectionFailure(
                        ErrorCode.COMMAND_TIMEOUT,
                        "Timeout.",
                        transient=True,
                    )
                }
                if index == 0
                else {}
            ),
        )
    )
    events = []

    result = ArubaCollector(factory).collect_one(
        TARGET,
        CREDENTIALS,
        options=FAST_RETRY_OPTIONS,
        on_event=events.append,
    )

    assert result.status is DeviceStatus.SUCCESS
    assert CollectionStage.RETRY_QUEUED in [event.stage for event in events]
    assert CollectionStage.RETRY_WAIT in [event.stage for event in events]
    assert events[-1].stage is CollectionStage.COMPLETED
    rendered = repr(events)
    assert "session-password" not in rendered
    assert "enable-secret" not in rendered


def test_callback_exception_does_not_change_collection_outcome() -> None:
    def broken_callback(event: object) -> None:
        del event
        raise RuntimeError("observer failed")

    result = ArubaCollector(ScriptedFactory()).collect_one(
        TARGET,
        CREDENTIALS,
        on_event=broken_callback,
    )

    assert result.status is DeviceStatus.SUCCESS


def test_probe_host_keys_returns_review_states_and_approve_persists(tmp_path: Path) -> None:
    store = HostKeyStore(tmp_path / "known_hosts.json")
    observation = HostKeyObservation(
        TARGET,
        "ssh-ed25519",
        sha256_fingerprint(b"server-public-key"),
    )

    class Probe:
        def probe(self, target: DeviceTarget, *, timeout: float = 15.0) -> HostKeyObservation:
            assert target == TARGET
            assert timeout == 15.0
            return observation

    collector = ArubaCollector(
        ScriptedFactory(),
        host_key_store=store,
        host_key_probe=Probe(),
    )
    checks = collector.probe_host_keys([TARGET])
    assert checks[0].state is HostKeyTrustState.UNKNOWN

    collector.approve_host_keys(checks)
    assert collector.probe_host_keys([TARGET])[0].state is HostKeyTrustState.TRUSTED


def test_probe_failure_is_retry_bounded_per_target_and_does_not_abort_batch(tmp_path: Path) -> None:
    unreachable = DeviceTarget("192.0.2.41")
    reachable = DeviceTarget("192.0.2.42")
    calls: dict[str, int] = {unreachable.ip: 0, reachable.ip: 0}
    call_order: list[str] = []

    class Probe:
        def probe(self, target: DeviceTarget, *, timeout: float = 15.0) -> HostKeyObservation:
            del timeout
            calls[target.ip] += 1
            call_order.append(target.ip)
            if target == unreachable:
                raise CollectionFailure(
                    ErrorCode.TCP_TIMEOUT,
                    "The SSH endpoint did not respond before the connection timeout.",
                    transient=True,
                )
            return HostKeyObservation(
                target,
                "ssh-ed25519",
                sha256_fingerprint(b"reachable-server-key"),
            )

    collector = ArubaCollector(
        ScriptedFactory(),
        host_key_store=HostKeyStore(tmp_path / "known_hosts.json"),
        host_key_probe=Probe(),
    )

    checks = collector.probe_host_keys(
        [unreachable, reachable],
        options=CollectionOptions(concurrency=1, retry_delays_seconds=(0.0, 0.0, 0.0)),
    )

    assert [check.target for check in checks] == [unreachable, reachable]
    assert checks[0].state is HostKeyTrustState.REJECTED
    assert checks[0].error_code is ErrorCode.TCP_TIMEOUT
    assert checks[0].attempts == 4
    assert checks[0].retryable
    assert checks[0].retry_exhausted
    assert checks[1].state is HostKeyTrustState.UNKNOWN
    assert checks[1].attempts == 1
    assert calls == {unreachable.ip: 4, reachable.ip: 1}
    assert call_order == [
        unreachable.ip,
        reachable.ip,
        unreachable.ip,
        unreachable.ip,
        unreachable.ip,
    ]


def test_host_key_retry_wait_is_cancellable_and_emits_structured_delay(tmp_path: Path) -> None:
    cancelled = threading.Event()
    events = []

    class Probe:
        def probe(self, target: DeviceTarget, *, timeout: float = 15.0) -> HostKeyObservation:
            del timeout
            raise CollectionFailure(
                ErrorCode.TCP_TIMEOUT,
                "The endpoint timed out.",
                transient=True,
            )

    collector = ArubaCollector(
        ScriptedFactory(),
        host_key_store=HostKeyStore(tmp_path / "known_hosts.json"),
        host_key_probe=Probe(),
    )

    def on_event(event: object) -> None:
        events.append(event)
        if getattr(event, "stage", None) is CollectionStage.RETRY_WAIT:
            cancelled.set()

    with pytest.raises(CollectionFailure) as caught:
        collector.probe_host_keys(
            [TARGET],
            options=CollectionOptions(retry_delays_seconds=(5.0, 0.0, 0.0)),
            cancel_event=cancelled,
            on_event=on_event,
        )

    assert caught.value.code is ErrorCode.CANCELLED
    retry_wait = next(event for event in events if event.stage is CollectionStage.RETRY_WAIT)
    assert retry_wait.round == 2
    assert retry_wait.delay_seconds == 5.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 4, "retry_delays_seconds": (0.0, 0.0)},
        {"retry_delays_seconds": (0.0, -1.0, 0.0)},
        {"retry_delays_seconds": (0.0, float("inf"), 0.0)},
        {"retry_delays_seconds": (0.0, float("nan"), 0.0)},
    ],
)
def test_collection_options_reject_invalid_retry_delays(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        CollectionOptions(**kwargs)  # type: ignore[arg-type]


def test_public_retry_wait_contract_distinguishes_elapsed_from_cancelled() -> None:
    collector = ArubaCollector(ScriptedFactory())
    assert collector.wait_for_retry_delay(0.0)
    cancelled = threading.Event()
    cancelled.set()
    assert not collector.wait_for_retry_delay(5.0, cancel_event=cancelled)


def test_unexpected_factory_failure_is_sanitized() -> None:
    class BrokenFactory:
        def create(self, target: object, credentials: object, options: object) -> object:
            del target, credentials, options
            raise RuntimeError("internal secret detail")

    result = ArubaCollector(BrokenFactory()).collect_one(TARGET, CREDENTIALS)  # type: ignore[arg-type]

    assert result.error_code is ErrorCode.UNEXPECTED_ERROR
    assert "internal secret detail" not in result.error_message
