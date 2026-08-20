from __future__ import annotations

import hashlib
import threading
from pathlib import Path

from tests.fakes import MODULES_2930F, RUNNING_CONFIG, ScriptedFactory, ScriptedSession

from aruba2930f_backup.collector import ArubaCollector
from aruba2930f_backup.hostkeys import HostKeyStore, sha256_fingerprint
from aruba2930f_backup.models import (
    CollectionFailure,
    CollectionOptions,
    CollectionStage,
    Credentials,
    DeviceStatus,
    DeviceTarget,
    ErrorCode,
    HostKeyObservation,
    HostKeyTrustState,
)

TARGET = DeviceTarget("192.0.2.40")
CREDENTIALS = Credentials("operator", "session-password", "enable-secret")


def test_collection_enforces_exact_command_order_and_normalizes_hash() -> None:
    factory = ScriptedFactory()
    collector = ArubaCollector(factory)

    result = collector.collect_one(TARGET, CREDENTIALS)

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

    result = collector.collect_one(TARGET, CREDENTIALS)

    assert result.status is DeviceStatus.SUCCESS
    assert result.attempts == 4
    assert len(factory.sessions) == 4
    assert all(session.calls.count("no page") == 1 for session in factory.sessions)
    assert all(session.calls.count("show running-config") == 1 for session in factory.sessions)
    assert all(session.closed for session in factory.sessions)


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
    assert not any(call.startswith("show ") for call in factory.sessions[0].calls)


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
    assert "show running-config" not in factory.sessions[0].calls


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

    result = ArubaCollector(factory).collect_one(TARGET, CREDENTIALS, on_event=events.append)

    assert result.status is DeviceStatus.SUCCESS
    assert CollectionStage.RETRYING in [event.stage for event in events]
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

    class Probe:
        def probe(self, target: DeviceTarget, *, timeout: float = 15.0) -> HostKeyObservation:
            del timeout
            calls[target.ip] += 1
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

    checks = collector.probe_host_keys([unreachable, reachable])

    assert [check.target for check in checks] == [unreachable, reachable]
    assert checks[0].state is HostKeyTrustState.REJECTED
    assert checks[0].error_code is ErrorCode.TCP_TIMEOUT
    assert checks[0].attempts == 4
    assert checks[1].state is HostKeyTrustState.UNKNOWN
    assert checks[1].attempts == 1
    assert calls == {unreachable.ip: 4, reachable.ip: 1}


def test_unexpected_factory_failure_is_sanitized() -> None:
    class BrokenFactory:
        def create(self, target: object, credentials: object, options: object) -> object:
            del target, credentials, options
            raise RuntimeError("internal secret detail")

    result = ArubaCollector(BrokenFactory()).collect_one(TARGET, CREDENTIALS)  # type: ignore[arg-type]

    assert result.error_code is ErrorCode.UNEXPECTED_ERROR
    assert "internal secret detail" not in result.error_message
