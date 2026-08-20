from __future__ import annotations

import json

import pytest

from aruba2930f_backup.hostkeys import HostKeyStore, sha256_fingerprint
from aruba2930f_backup.models import (
    CollectionFailure,
    DeviceTarget,
    ErrorCode,
    HostKeyObservation,
    HostKeyTrustState,
)

TARGET = DeviceTarget("192.0.2.10", 22)


def observation(key: bytes = b"public-key-a", key_type: str = "ssh-ed25519") -> HostKeyObservation:
    return HostKeyObservation(TARGET, key_type, sha256_fingerprint(key))


def test_sha256_fingerprint_uses_openssh_format_without_padding() -> None:
    fingerprint = sha256_fingerprint(b"test-key")

    assert fingerprint.startswith("SHA256:")
    assert "=" not in fingerprint


def test_unknown_key_requires_explicit_approval_then_matches(tmp_path) -> None:
    path = tmp_path / "known_hosts.json"
    store = HostKeyStore(path)

    check = store.check(observation())
    assert check.state is HostKeyTrustState.UNKNOWN
    assert not path.exists()

    store.approve([check])

    assert store.check(observation()).state is HostKeyTrustState.TRUSTED
    approved = store.list_approved()
    assert len(approved) == 1
    assert approved[0].endpoint == TARGET.endpoint
    assert approved[0].key_type == "ssh-ed25519"
    assert approved[0].fingerprint == observation().fingerprint
    assert approved[0].approved_at


def test_changed_key_fails_closed_and_cannot_be_reapproved_until_removed(tmp_path) -> None:
    store = HostKeyStore(tmp_path / "known_hosts.json")
    first = store.check(observation(b"first"))
    store.approve([first])

    changed = store.check(observation(b"second"))
    assert changed.state is HostKeyTrustState.CHANGED
    assert changed.known_fingerprint == observation(b"first").fingerprint
    with pytest.raises(CollectionFailure) as required:
        store.require_trusted(changed.observation)
    assert required.value.code is ErrorCode.HOST_KEY_CHANGED
    with pytest.raises(CollectionFailure) as approval:
        store.approve([changed])
    assert approval.value.code is ErrorCode.HOST_KEY_CHANGED

    assert store.remove(TARGET)
    assert not store.remove(TARGET.endpoint)
    newly_unknown = store.check(observation(b"second"))
    assert newly_unknown.state is HostKeyTrustState.UNKNOWN
    store.approve([newly_unknown])
    assert store.check(observation(b"second")).state is HostKeyTrustState.TRUSTED


def test_key_type_change_is_treated_as_changed(tmp_path) -> None:
    store = HostKeyStore(tmp_path / "known_hosts.json")
    store.approve([store.check(observation())])

    changed = store.check(observation(key_type="ssh-rsa"))

    assert changed.state is HostKeyTrustState.CHANGED


def test_unknown_key_is_never_written_by_check_or_require(tmp_path) -> None:
    path = tmp_path / "known_hosts.json"
    store = HostKeyStore(path)

    with pytest.raises(CollectionFailure) as captured:
        store.require_trusted(observation())

    assert captured.value.code is ErrorCode.HOST_KEY_REJECTED
    assert not path.exists()


def test_corrupt_or_wrong_schema_store_fails_closed_without_overwrite(tmp_path) -> None:
    path = tmp_path / "known_hosts.json"
    path.write_text("not-json", encoding="utf-8")

    with pytest.raises(CollectionFailure) as corrupt:
        HostKeyStore(path).check(observation())
    assert corrupt.value.code is ErrorCode.HOST_KEY_REJECTED
    assert path.read_text(encoding="utf-8") == "not-json"

    path.write_text(json.dumps({"schema_version": 99, "endpoints": {}}), encoding="utf-8")
    with pytest.raises(CollectionFailure):
        HostKeyStore(path).list_approved()

    path.write_text(
        json.dumps({"schema_version": 1, "endpoints": {TARGET.endpoint: "not-a-record"}}),
        encoding="utf-8",
    )
    with pytest.raises(CollectionFailure) as invalid_record:
        HostKeyStore(path).list_approved()
    assert invalid_record.value.code is ErrorCode.HOST_KEY_REJECTED


def test_approval_is_atomic_and_store_contains_no_temporary_files(tmp_path) -> None:
    path = tmp_path / "nested" / "known_hosts.json"
    store = HostKeyStore(path)
    store.approve([store.check(observation())])

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert list(payload["endpoints"]) == [TARGET.endpoint]
    assert list(path.parent.glob("*.tmp")) == []


def test_approve_rejects_fabricated_non_unknown_review(tmp_path) -> None:
    store = HostKeyStore(tmp_path / "known_hosts.json")
    check = store.check(observation())
    fabricated = type(check)(check.observation, HostKeyTrustState.TRUSTED)

    with pytest.raises(CollectionFailure) as captured:
        store.approve([fabricated])

    assert captured.value.code is ErrorCode.HOST_KEY_REJECTED
