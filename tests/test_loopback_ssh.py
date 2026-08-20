from __future__ import annotations

from tests.loopback_server import LoopbackArubaSSHServer, wait_for

from aruba2930f_backup.collector import ArubaCollector
from aruba2930f_backup.hostkeys import HostKeyStore, sha256_fingerprint
from aruba2930f_backup.models import (
    CollectionOptions,
    Credentials,
    DeviceStatus,
    DeviceTarget,
    ErrorCode,
    HostKeyCheck,
    HostKeyObservation,
    HostKeyTrustState,
)


def test_production_ssh_stack_against_loopback_aruba_fixture(tmp_path) -> None:
    """Exercise real Paramiko/Netmiko boundaries without any external network."""

    with LoopbackArubaSSHServer() as server:
        target = DeviceTarget("127.0.0.1", server.port)
        store = HostKeyStore(tmp_path / "known_hosts.json")
        collector = ArubaCollector(host_key_store=store)
        options = CollectionOptions(
            concurrency=1,
            max_attempts=1,
            connect_timeout_seconds=3,
            command_timeout_seconds=5,
        )

        checks = collector.probe_host_keys([target], options=options)
        assert checks[0].state is HostKeyTrustState.UNKNOWN
        assert checks[0].observation.fingerprint.startswith("SHA256:")
        assert server.auth_attempts == []  # Preflight sent no username/password.

        collector.approve_host_keys(checks)
        results = collector.collect_many(
            [target],
            Credentials(server.username, server.password),
            options,
        )

        assert len(results) == 1
        result = results[0]
        assert result.status is DeviceStatus.SUCCESS
        assert result.hostname == "edge-lab"
        assert result.sku == "JL253A"
        assert result.software_version == "WC.16.11.0025"
        assert result.config_text is not None
        assert "Running configuration:\r\n" in result.config_text
        assert "hostname edge-lab\r\n" in result.config_text
        assert "vlan 1\r\n" in result.config_text
        assert result.config_sha256
        assert wait_for(lambda: len(server.commands) >= 5)
        assert server.commands[:5] == [
            "no page",
            "terminal width 511",
            "show version",
            "show modules",
            "show running-config",
        ]
        assert server.commands[5:] == ["logout"]  # Netmiko's normal disconnect command.
        assert server.pager_advances == 1
        assert len(server.auth_attempts) == 1
        assert server.errors == []


def test_production_ssh_stack_supports_legacy_2930f_algorithms(tmp_path) -> None:
    """Keep compatibility with 2930F endpoints that only offer SHA-1 SSH algorithms."""

    with LoopbackArubaSSHServer(legacy_algorithms_only=True) as server:
        target = DeviceTarget("127.0.0.1", server.port)
        store = HostKeyStore(tmp_path / "known_hosts.json")
        collector = ArubaCollector(host_key_store=store)
        options = CollectionOptions(
            concurrency=1,
            max_attempts=1,
            connect_timeout_seconds=3,
            command_timeout_seconds=5,
        )

        checks = collector.probe_host_keys([target], options=options)
        assert checks[0].state is HostKeyTrustState.UNKNOWN
        assert server.auth_attempts == []

        collector.approve_host_keys(checks)
        results = collector.collect_many(
            [target],
            Credentials(server.username, server.password),
            options,
        )

        assert results[0].status is DeviceStatus.SUCCESS
        assert results[0].software_version == "WC.16.11.0025"
        assert wait_for(lambda: "show running-config" in server.commands)
        assert len(server.auth_attempts) == 1
        assert server.errors == []


def test_production_ssh_stack_advances_aruba_login_banner(tmp_path) -> None:
    with LoopbackArubaSSHServer(login_banner=True) as server:
        target = DeviceTarget("127.0.0.1", server.port)
        store = HostKeyStore(tmp_path / "known_hosts.json")
        collector = ArubaCollector(host_key_store=store)
        options = CollectionOptions(
            concurrency=1,
            max_attempts=1,
            connect_timeout_seconds=3,
            command_timeout_seconds=5,
        )

        checks = collector.probe_host_keys([target], options=options)
        collector.approve_host_keys(checks)
        result = collector.collect_one(
            target,
            Credentials(server.username, server.password),
            options=options,
        )

        assert result.status is DeviceStatus.SUCCESS
        assert result.hostname == "edge-lab"
        assert server.banner_advances == 1
        assert wait_for(lambda: "show running-config" in server.commands)
        assert server.errors == []


def test_production_ssh_stack_accepts_complex_exec_prompt_as_opaque_token(tmp_path) -> None:
    with LoopbackArubaSSHServer(prompt="(Aruba 2930F PoE+) #") as server:
        target = DeviceTarget("127.0.0.1", server.port)
        store = HostKeyStore(tmp_path / "known_hosts.json")
        collector = ArubaCollector(host_key_store=store)
        options = CollectionOptions(
            concurrency=1,
            max_attempts=1,
            connect_timeout_seconds=3,
            command_timeout_seconds=5,
        )

        checks = collector.probe_host_keys([target], options=options)
        collector.approve_host_keys(checks)
        result = collector.collect_one(
            target,
            Credentials(server.username, server.password),
            options=options,
        )

        assert result.status is DeviceStatus.SUCCESS
        assert result.hostname == "edge-lab"
        assert wait_for(lambda: "show running-config" in server.commands)
        assert server.commands.count("show running-config") == 1
        assert server.errors == []


def test_changed_loopback_key_is_blocked_before_authentication(tmp_path) -> None:
    """Prove the authenticated transport pins the reviewed key before credentials."""

    with LoopbackArubaSSHServer() as server:
        target = DeviceTarget("127.0.0.1", server.port)
        store = HostKeyStore(tmp_path / "known_hosts.json")
        store.approve(
            [
                HostKeyCheck(
                    observation=HostKeyObservation(
                        target,
                        server.host_key.get_name(),
                        sha256_fingerprint(b"different-loopback-host-key"),
                    ),
                    state=HostKeyTrustState.UNKNOWN,
                )
            ]
        )
        collector = ArubaCollector(host_key_store=store)
        collector.begin_run()
        results = collector.collect_many(
            [target],
            Credentials(server.username, server.password),
            CollectionOptions(
                concurrency=1,
                max_attempts=1,
                connect_timeout_seconds=3,
                command_timeout_seconds=5,
            ),
        )

    assert results[0].status is DeviceStatus.FAILED
    assert results[0].error_code is ErrorCode.HOST_KEY_CHANGED
    assert server.auth_attempts == []
    assert server.commands == []
