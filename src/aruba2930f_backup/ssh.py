"""Mockable ArubaOS-Switch SSH session with pinned host keys and bounded reads."""

from __future__ import annotations

import re
import socket
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any, Protocol

from .hostkeys import HostKeyStore, sha256_fingerprint
from .models import (
    CollectionFailure,
    CollectionOptions,
    Credentials,
    DeviceTarget,
    ErrorCode,
    HostKeyObservation,
)
from .validation import PAGER_MARKERS, require_valid_prompt, validate_cli_response


class SSHSession(Protocol):
    target: DeviceTarget

    def connect(self) -> None: ...

    def enter_enable(self) -> None: ...

    def disable_paging(self) -> None: ...

    def set_terminal_width(self) -> None: ...

    def send_show(self, command: str, *, cancel_event: CancellationSignal | None = None) -> str: ...

    def get_prompt(self) -> str: ...

    def close(self) -> None: ...


class SSHSessionFactory(Protocol):
    def create(
        self,
        target: DeviceTarget,
        credentials: Credentials,
        options: CollectionOptions,
    ) -> SSHSession: ...


class CancellationSignal(Protocol):
    def is_set(self) -> bool: ...


ConnectionBuilder = Callable[[DeviceTarget, Credentials, CollectionOptions, HostKeyStore], Any]
_ALLOWED_SHOW_COMMANDS = frozenset({"show version", "show modules", "show running-config"})


class NetmikoSessionFactory:
    def __init__(
        self,
        host_key_store: HostKeyStore,
        *,
        connection_builder: ConnectionBuilder | None = None,
    ) -> None:
        self.host_key_store = host_key_store
        self.connection_builder = connection_builder

    def create(
        self,
        target: DeviceTarget,
        credentials: Credentials,
        options: CollectionOptions,
    ) -> NetmikoSSHSession:
        return NetmikoSSHSession(
            target,
            credentials,
            options,
            self.host_key_store,
            connection_builder=self.connection_builder,
        )


class NetmikoSSHSession:
    """A deliberately explicit replacement for Netmiko's automatic prep.

    The Aruba driver normally performs enable/terminal-width/paging setup in
    ``session_preparation``. We connect with ``auto_connect=False`` and call the
    operations ourselves so ``no page`` is verified before any ``show`` command
    and repeated for every retry/reconnection.
    """

    def __init__(
        self,
        target: DeviceTarget,
        credentials: Credentials,
        options: CollectionOptions,
        host_key_store: HostKeyStore,
        *,
        connection_builder: ConnectionBuilder | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.target = target
        self._credentials = credentials
        self._options = options
        self._host_key_store = host_key_store
        self._connection_builder = connection_builder or _build_pinned_connection
        self._clock = clock
        self._sleep = sleeper
        self._connection: Any | None = None
        self._prompt: str | None = None
        self._paging_prepared = False
        self._terminal_width_prepared = False

    def connect(self) -> None:
        self._paging_prepared = False
        self._terminal_width_prepared = False
        try:
            connection = self._connection_builder(
                self.target,
                self._credentials,
                self._options,
                self._host_key_store,
            )
            self._connection = connection
            connection.establish_connection()
            connection.set_base_prompt()
            self._prompt = _verified_exec_prompt(connection.find_prompt())
        except Exception as exc:
            self.close()
            raise _mapped_failure(exc, during="connect") from exc

    def enter_enable(self) -> None:
        connection = self._require_connection()
        self._paging_prepared = False
        self._terminal_width_prepared = False
        try:
            connection.enable()
            if hasattr(connection, "check_enable_mode") and not connection.check_enable_mode():
                raise CollectionFailure(
                    ErrorCode.ENABLE_FAILED,
                    "The privileged EXEC prompt was not reached.",
                )
            self._prompt = _verified_exec_prompt(connection.find_prompt())
        except Exception as exc:
            raise _mapped_failure(exc, during="enable") from exc

    def disable_paging(self) -> None:
        try:
            self._execute_setup_command("no page")
        except Exception as exc:
            if isinstance(exc, CollectionFailure) and exc.code is ErrorCode.CANCELLED:
                raise
            raise CollectionFailure(
                ErrorCode.PAGING_SETUP_FAILED,
                "The device did not accept and verify 'no page'.",
            ) from exc
        self._paging_prepared = True
        self._terminal_width_prepared = False

    def set_terminal_width(self) -> None:
        if not self._paging_prepared:
            raise CollectionFailure(
                ErrorCode.PAGING_SETUP_FAILED,
                "Paging must be disabled before setting the terminal width.",
            )
        try:
            self._execute_setup_command("terminal width 511")
        except Exception as exc:
            raise _mapped_failure(exc, during="command") from exc
        self._terminal_width_prepared = True

    def send_show(
        self,
        command: str,
        *,
        cancel_event: CancellationSignal | None = None,
    ) -> str:
        normalized_command = " ".join(command.strip().split())
        if normalized_command.lower() not in _ALLOWED_SHOW_COMMANDS:
            raise ValueError("Only the registered read-only show commands are permitted.")
        normalized_command = normalized_command.lower()
        if not self._paging_prepared:
            raise CollectionFailure(
                ErrorCode.PAGING_SETUP_FAILED,
                "'no page' must be verified before any show command.",
            )
        if not self._terminal_width_prepared:
            raise CollectionFailure(
                ErrorCode.COMMAND_REJECTED,
                "Terminal width 511 must be verified before any show command.",
            )
        connection = self._require_connection()
        prompt = self._prompt or _verified_exec_prompt(connection.find_prompt())
        try:
            if hasattr(connection, "clear_buffer"):
                connection.clear_buffer()
            connection.write_channel(connection.normalize_cmd(normalized_command))
            raw = self._read_until_prompt(
                prompt,
                cancel_event=cancel_event,
            )
            output = _strip_command_and_prompt(raw, normalized_command, prompt)
            validate_cli_response(normalized_command, output)
            return output
        except Exception as exc:
            raise _mapped_failure(exc, during="command") from exc

    def get_prompt(self) -> str:
        connection = self._require_connection()
        try:
            prompt = _verified_exec_prompt(connection.find_prompt())
        except Exception as exc:
            raise _mapped_failure(exc, during="prompt") from exc
        self._prompt = prompt
        return prompt

    def close(self) -> None:
        connection, self._connection = self._connection, None
        self._paging_prepared = False
        self._terminal_width_prepared = False
        if connection is not None:
            with suppress(Exception):
                connection.disconnect()

    def _execute_setup_command(self, command: str) -> None:
        connection = self._require_connection()
        prompt = self._prompt or _verified_exec_prompt(connection.find_prompt())
        output = connection.send_command(
            command,
            expect_string=re.escape(prompt),
            read_timeout=min(30.0, self._options.command_timeout_seconds),
            strip_prompt=False,
            strip_command=False,
            cmd_verify=True,
        )
        validate_cli_response(command, output)
        verified = _verified_exec_prompt(connection.find_prompt())
        self._prompt = verified

    def _read_until_prompt(
        self,
        prompt: str,
        *,
        cancel_event: CancellationSignal | None,
    ) -> str:
        connection = self._require_connection()
        deadline = self._clock() + self._options.command_timeout_seconds
        output = ""
        pager_advances = 0

        while self._clock() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                self.close()
                raise CollectionFailure(ErrorCode.CANCELLED, "Collection was cancelled.")
            chunk = connection.read_channel()
            if chunk:
                output += chunk
                _enforce_output_bounds(
                    output,
                    max_bytes=self._options.max_output_bytes,
                    max_lines=self._options.max_output_lines,
                )
                output = (
                    _remove_backspaces(_strip_ansi(output))
                    .replace("\r\n", "\n")
                    .replace("\r", "\n")
                )
            elif output:
                # Confirm one channel-idle read before interpreting a tail token.
                # This avoids treating a legitimate line at a TCP chunk boundary
                # as either the final prompt or the device pager.
                clean_tail = output.rstrip()
                pager_match = _tail_pager_match(clean_tail)
                if pager_match is not None:
                    pager_advances += 1
                    if pager_advances > self._options.max_pager_advances:
                        raise CollectionFailure(
                            ErrorCode.OUTPUT_LIMIT_EXCEEDED,
                            "The residual pager exceeded its safety limit.",
                        )
                    prefix = clean_tail[: pager_match.start()]
                    if prefix.endswith("\n"):
                        prefix = prefix[:-1]
                    output = prefix + output[len(clean_tail) :]
                    connection.write_channel(" ")
                elif _ends_with_prompt(output, prompt):
                    return output
            self._sleep(0.05)

        raise CollectionFailure(
            ErrorCode.COMMAND_TIMEOUT,
            "The command did not return to the prompt before the timeout.",
            transient=True,
        )

    def _require_connection(self) -> Any:
        if self._connection is None:
            raise CollectionFailure(
                ErrorCode.SSH_NEGOTIATION_FAILED,
                "The SSH session is not connected.",
                transient=True,
            )
        return self._connection


class _PinnedHostKeyPolicy:
    """Paramiko policy used on the authenticated connection itself."""

    def __init__(self, target: DeviceTarget, store: HostKeyStore) -> None:
        self.target = target
        self.store = store

    def missing_host_key(self, client: Any, hostname: str, key: Any) -> None:
        del client, hostname
        observation = HostKeyObservation(
            target=self.target,
            key_type=key.get_name(),
            fingerprint=sha256_fingerprint(key.asbytes()),
        )
        self.store.require_trusted(observation)


def _build_pinned_connection(
    target: DeviceTarget,
    credentials: Credentials,
    options: CollectionOptions,
    host_key_store: HostKeyStore,
) -> Any:
    try:
        import paramiko  # type: ignore[import-untyped]
        from netmiko.hp.hp_procurve import HPProcurveSSH
    except ImportError as exc:  # pragma: no cover - packaging dependency guard
        raise RuntimeError("Netmiko and Paramiko are required for SSH collection.") from exc

    def build_ssh_client(self: Any) -> Any:
        del self
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(_PinnedHostKeyPolicy(target, host_key_store))
        return client

    pinned_driver = type(
        "PinnedHPProcurveSSH",
        (HPProcurveSSH,),
        {"_build_ssh_client": build_ssh_client},
    )
    return pinned_driver(
        device_type="aruba_osswitch",
        host=target.ip,
        port=target.port,
        username=credentials.username,
        password=credentials.password,
        secret=credentials.enable_secret or "",
        conn_timeout=options.connect_timeout_seconds,
        auth_timeout=options.connect_timeout_seconds,
        banner_timeout=options.connect_timeout_seconds,
        blocking_timeout=options.command_timeout_seconds,
        fast_cli=False,
        auto_connect=False,
    )


def _mapped_failure(exc: Exception, *, during: str) -> CollectionFailure:
    if isinstance(exc, CollectionFailure):
        return exc
    name = type(exc).__name__.lower()
    if "authentication" in name or "authfail" in name:
        code = ErrorCode.ENABLE_FAILED if during == "enable" else ErrorCode.AUTH_FAILED
        message = (
            "Privileged EXEC authentication failed."
            if during == "enable"
            else "SSH authentication failed."
        )
        return CollectionFailure(code, message)
    if (
        isinstance(exc, (TimeoutError, socket.timeout))
        or "timeout" in name
        or "readtimeout" in name
    ):
        if during == "connect":
            return CollectionFailure(
                ErrorCode.TCP_TIMEOUT,
                "The SSH endpoint did not respond before the connection timeout.",
                transient=True,
            )
        return CollectionFailure(
            ErrorCode.COMMAND_TIMEOUT,
            "The SSH operation did not finish before the timeout.",
            transient=True,
        )
    if during == "enable":
        return CollectionFailure(
            ErrorCode.ENABLE_FAILED, "Privileged EXEC mode could not be entered."
        )
    if during == "prompt":
        return CollectionFailure(
            ErrorCode.PROMPT_PARSE_FAILED,
            "The final device prompt could not be verified.",
            transient=True,
        )
    if during == "command":
        return CollectionFailure(
            ErrorCode.SSH_NEGOTIATION_FAILED,
            "The SSH channel failed while reading command output.",
            transient=True,
        )
    return CollectionFailure(
        ErrorCode.SSH_NEGOTIATION_FAILED,
        "The SSH session could not be established.",
        transient=True,
    )


def _clean_prompt(prompt: str) -> str:
    clean = _remove_backspaces(_strip_ansi(prompt or "")).strip()
    return clean if "\n" not in clean else clean.splitlines()[-1].strip()


def _verified_exec_prompt(prompt: str) -> str:
    clean = _clean_prompt(prompt)
    require_valid_prompt(clean)
    return clean


def _strip_ansi(value: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)


def _remove_backspaces(value: str) -> str:
    while "\x08" in value:
        updated = re.sub(r"[^\x08]\x08", "", value)
        if updated == value:
            return value.replace("\x08", "")
        value = updated
    return value


def _ends_with_prompt(output: str, prompt: str) -> bool:
    return bool(re.search(rf"(?m)^{re.escape(prompt)}\s*$", output.rstrip()))


def _tail_pager_match(output: str) -> re.Match[str] | None:
    for pattern in PAGER_MARKERS:
        matches = tuple(pattern.finditer(output))
        if matches and matches[-1].end() == len(output):
            return matches[-1]
    return None


def _strip_command_and_prompt(output: str, command: str, prompt: str) -> str:
    lines = output.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines:
        first = lines[0].strip()
        if first in {command, f"{prompt}{command}", f"{prompt} {command}"}:
            lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].strip() == prompt:
        lines.pop()
    return "\n".join(lines).strip("\n")


def _enforce_output_bounds(output: str, *, max_bytes: int, max_lines: int) -> None:
    if len(output.encode("utf-8")) > max_bytes or output.count("\n") + 1 > max_lines:
        raise CollectionFailure(
            ErrorCode.OUTPUT_LIMIT_EXCEEDED,
            "Command output exceeded the configured safety limit.",
        )
