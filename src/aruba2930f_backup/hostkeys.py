"""Explicit SSH host-key probing and per-endpoint fingerprint trust."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import tempfile
import threading
from collections.abc import Iterable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .models import (
    ApprovedHostKey,
    CollectionFailure,
    DeviceTarget,
    ErrorCode,
    HostKeyCheck,
    HostKeyObservation,
    HostKeyTrustState,
)


class HostKeyProbe(Protocol):
    def probe(self, target: DeviceTarget, *, timeout: float = 15.0) -> HostKeyObservation: ...


def sha256_fingerprint(key_bytes: bytes) -> str:
    digest = hashlib.sha256(key_bytes).digest()
    encoded = base64.b64encode(digest).decode("ascii").rstrip("=")
    return f"SHA256:{encoded}"


def default_host_key_store_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "Aruba2930FConfigBackup" / "known_hosts.json"


class HostKeyStore:
    """Thread-safe JSON store that never auto-accepts unknown or changed keys."""

    _SCHEMA_VERSION = 1

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else default_host_key_store_path()
        self._lock = threading.RLock()

    def check(self, observation: HostKeyObservation) -> HostKeyCheck:
        with self._lock:
            endpoints = self._read_endpoints()
            record = endpoints.get(observation.target.endpoint)
            if record is None:
                return HostKeyCheck(
                    observation=observation,
                    state=HostKeyTrustState.UNKNOWN,
                    message="This endpoint has not been approved yet.",
                )
            known = str(record.get("fingerprint", ""))
            known_type = str(record.get("key_type", ""))
            if known == observation.fingerprint and known_type == observation.key_type:
                return HostKeyCheck(
                    observation=observation,
                    state=HostKeyTrustState.TRUSTED,
                    known_fingerprint=known,
                    message="The host key matches the approved fingerprint.",
                )
            return HostKeyCheck(
                observation=observation,
                state=HostKeyTrustState.CHANGED,
                known_fingerprint=known or None,
                message="The host key differs from the approved fingerprint.",
            )

    def require_trusted(self, observation: HostKeyObservation) -> None:
        check = self.check(observation)
        if check.state is HostKeyTrustState.TRUSTED:
            return
        if check.state is HostKeyTrustState.CHANGED:
            raise CollectionFailure(
                ErrorCode.HOST_KEY_CHANGED,
                "The SSH host key changed; the connection was blocked.",
            )
        raise CollectionFailure(
            ErrorCode.HOST_KEY_REJECTED,
            "The SSH host key has not been explicitly approved.",
        )

    def approve(self, checks: Iterable[HostKeyCheck]) -> None:
        """Persist only checks that are still unknown under the current store state."""

        requested = tuple(checks)
        if not requested:
            return
        with self._lock:
            endpoints = self._read_endpoints()
            additions: dict[str, dict[str, str]] = {}
            for supplied in requested:
                observation = supplied.observation
                current_record = endpoints.get(observation.target.endpoint)
                if current_record is not None:
                    known_fingerprint = str(current_record.get("fingerprint", ""))
                    known_type = str(current_record.get("key_type", ""))
                    if (
                        known_fingerprint == observation.fingerprint
                        and known_type == observation.key_type
                    ):
                        continue
                    raise CollectionFailure(
                        ErrorCode.HOST_KEY_CHANGED,
                        "A changed SSH host key cannot be approved as a new key.",
                    )
                if supplied.state is not HostKeyTrustState.UNKNOWN:
                    raise CollectionFailure(
                        ErrorCode.HOST_KEY_REJECTED,
                        "Only an explicitly reviewed unknown host key can be approved.",
                    )
                _validate_observation(observation)
                additions[observation.target.endpoint] = {
                    "key_type": observation.key_type,
                    "fingerprint": observation.fingerprint,
                    "approved_at": datetime.now(UTC).isoformat(),
                }
            if not additions:
                return
            endpoints.update(additions)
            self._write_endpoints(endpoints)

    def list_approved(self) -> tuple[ApprovedHostKey, ...]:
        with self._lock:
            endpoints = self._read_endpoints()
            approved: list[ApprovedHostKey] = []
            for endpoint, record in sorted(endpoints.items()):
                approved.append(
                    ApprovedHostKey(
                        endpoint=endpoint,
                        key_type=str(record.get("key_type", "")),
                        fingerprint=str(record.get("fingerprint", "")),
                        approved_at=str(record.get("approved_at", "")),
                    )
                )
            return tuple(approved)

    def remove(self, endpoint: str | DeviceTarget) -> bool:
        """Explicitly forget one endpoint so a newly probed key can be reviewed."""

        key = endpoint.endpoint if isinstance(endpoint, DeviceTarget) else endpoint.strip()
        with self._lock:
            endpoints = self._read_endpoints()
            if key not in endpoints:
                return False
            del endpoints[key]
            self._write_endpoints(endpoints)
            return True

    def _read_endpoints(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CollectionFailure(
                ErrorCode.HOST_KEY_REJECTED,
                "The approved host-key store could not be read safely.",
            ) from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != self._SCHEMA_VERSION:
            raise CollectionFailure(
                ErrorCode.HOST_KEY_REJECTED,
                "The approved host-key store has an unsupported format.",
            )
        endpoints = payload.get("endpoints")
        if not isinstance(endpoints, dict):
            raise CollectionFailure(
                ErrorCode.HOST_KEY_REJECTED,
                "The approved host-key store has an invalid endpoint table.",
            )
        for endpoint, record in endpoints.items():
            if not isinstance(endpoint, str) or not isinstance(record, dict):
                raise CollectionFailure(
                    ErrorCode.HOST_KEY_REJECTED,
                    "The approved host-key store contains an invalid record.",
                )
            key_type = record.get("key_type")
            fingerprint = record.get("fingerprint")
            approved_at = record.get("approved_at")
            if (
                not isinstance(key_type, str)
                or not re_full_key_type(key_type)
                or not isinstance(fingerprint, str)
                or not fingerprint.startswith("SHA256:")
                or len(fingerprint) < 20
                or not isinstance(approved_at, str)
                or not approved_at
            ):
                raise CollectionFailure(
                    ErrorCode.HOST_KEY_REJECTED,
                    "The approved host-key store contains an invalid record.",
                )
        return endpoints

    def _write_endpoints(self, endpoints: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": self._SCHEMA_VERSION,
            "endpoints": dict(sorted(endpoints.items())),
        }
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                json.dump(payload, handle, ensure_ascii=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
                temp_name = handle.name
            Path(temp_name).replace(self.path)
        except OSError as exc:
            raise CollectionFailure(
                ErrorCode.HOST_KEY_REJECTED,
                "The approved host-key store could not be updated.",
            ) from exc
        finally:
            if temp_name:
                with suppress(OSError):
                    Path(temp_name).unlink(missing_ok=True)


class ParamikoHostKeyProbe:
    """Retrieve a server key before any username or password is transmitted."""

    def probe(self, target: DeviceTarget, *, timeout: float = 15.0) -> HostKeyObservation:
        try:
            import paramiko  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - packaging dependency guard
            raise RuntimeError("Paramiko is required for SSH host-key probing.") from exc

        connection: socket.socket | None = None
        transport: Any | None = None
        try:
            connection = socket.create_connection((target.ip, target.port), timeout=timeout)
            connection.settimeout(timeout)
            transport = paramiko.Transport(connection)
            transport.start_client(timeout=timeout)
            key = transport.get_remote_server_key()
            return HostKeyObservation(
                target=target,
                key_type=key.get_name(),
                fingerprint=sha256_fingerprint(key.asbytes()),
            )
        except TimeoutError as exc:
            raise CollectionFailure(
                ErrorCode.TCP_TIMEOUT,
                "The SSH endpoint did not respond before the connection timeout.",
                transient=True,
            ) from exc
        except (OSError, paramiko.SSHException) as exc:
            raise CollectionFailure(
                ErrorCode.SSH_NEGOTIATION_FAILED,
                "The SSH server key could not be retrieved safely.",
                transient=True,
            ) from exc
        finally:
            if transport is not None:
                transport.close()
            if connection is not None:
                connection.close()


def _validate_observation(observation: HostKeyObservation) -> None:
    if not re_full_key_type(observation.key_type):
        raise CollectionFailure(
            ErrorCode.HOST_KEY_REJECTED,
            "The SSH host key type is invalid.",
        )
    if not observation.fingerprint.startswith("SHA256:") or len(observation.fingerprint) < 20:
        raise CollectionFailure(
            ErrorCode.HOST_KEY_REJECTED,
            "The SSH host-key fingerprint is invalid.",
        )


def re_full_key_type(value: str) -> bool:
    if not value or len(value) > 128:
        return False
    return all(character.isalnum() or character in "@._+-" for character in value)
