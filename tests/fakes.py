from __future__ import annotations

import threading
from collections.abc import Callable

from aruba2930f_backup.models import (
    CollectionFailure,
    CollectionOptions,
    Credentials,
    DeviceTarget,
)

VERSION_2930F = """Aruba JL253A 2930F-24G-4SFP+ Switch
Software revision WC.16.11.0025
"""
MODULES_2930F = """Status and Counters - Module Information
Chassis: Aruba 2930F-24G-4SFP+ JL253A
"""
RUNNING_CONFIG = """Running configuration:
hostname edge-lab
vlan 1
   name DEFAULT_VLAN
"""


class ScriptedSession:
    def __init__(
        self,
        target: DeviceTarget,
        *,
        failure_by_command: dict[str, CollectionFailure] | None = None,
        responses: dict[str, str] | None = None,
        prompt: str = "edge-lab#",
        on_command: Callable[[str], None] | None = None,
    ) -> None:
        self.target = target
        self.failure_by_command = failure_by_command or {}
        self.responses = {
            "show version": VERSION_2930F,
            "show modules": MODULES_2930F,
            "show running-config": RUNNING_CONFIG,
            **(responses or {}),
        }
        self.prompt = prompt
        self.on_command = on_command
        self.calls: list[str] = []
        self.closed = False

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if self.on_command:
            self.on_command(name)
        failure = self.failure_by_command.get(name)
        if failure is not None:
            raise failure

    def connect(self) -> None:
        self._call("connect")

    def enter_enable(self) -> None:
        self._call("enable")

    def disable_paging(self) -> None:
        self._call("no page")

    def set_terminal_width(self) -> None:
        self._call("terminal width 511")

    def send_show(
        self,
        command: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> str:
        if cancel_event and cancel_event.is_set():
            from aruba2930f_backup.models import ErrorCode

            raise CollectionFailure(ErrorCode.CANCELLED, "Collection was cancelled.")
        self._call(command)
        return self.responses[command]

    def get_prompt(self) -> str:
        self._call("get prompt")
        return self.prompt

    def close(self) -> None:
        self.closed = True
        self.calls.append("close")


class ScriptedFactory:
    def __init__(
        self,
        builder: Callable[[int, DeviceTarget], ScriptedSession] | None = None,
    ) -> None:
        self.builder = builder
        self.sessions: list[ScriptedSession] = []
        self.credentials_seen: list[Credentials] = []
        self.options_seen: list[CollectionOptions] = []

    def create(
        self,
        target: DeviceTarget,
        credentials: Credentials,
        options: CollectionOptions,
    ) -> ScriptedSession:
        index = len(self.sessions)
        session = self.builder(index, target) if self.builder else ScriptedSession(target)
        self.sessions.append(session)
        self.credentials_seen.append(credentials)
        self.options_seen.append(options)
        return session
