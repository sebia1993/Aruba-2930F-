"""Deterministic loopback-only SSH fixture for the Aruba collector tests."""

from __future__ import annotations

import socket
import threading
import time
from contextlib import suppress
from types import TracebackType

import paramiko

PAGER = "-- MORE --, next page: Space, next line: Enter, quit: Control-C"


class _ServerInterface(paramiko.ServerInterface):
    def __init__(self, owner: LoopbackArubaSSHServer) -> None:
        self.owner = owner
        self.shell_requested = threading.Event()

    def get_allowed_auths(self, username: str) -> str:
        del username
        return "password"

    def check_auth_password(self, username: str, password: str) -> int:
        with self.owner._lock:
            self.owner.auth_attempts.append((username, password))
        if username == self.owner.username and password == self.owner.password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind: str, chanid: int) -> int:
        del chanid
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(
        self,
        channel: paramiko.Channel,
        term: bytes,
        width: int,
        height: int,
        pixelwidth: int,
        pixelheight: int,
        modes: bytes,
    ) -> bool:
        del channel, term, width, height, pixelwidth, pixelheight, modes
        return True

    def check_channel_shell_request(self, channel: paramiko.Channel) -> bool:
        del channel
        self.shell_requested.set()
        return True


class LoopbackArubaSSHServer:
    """Small password-authenticated Aruba-like CLI bound only to 127.0.0.1."""

    prompt = "edge-lab#"
    username = "fixture-operator"
    password = "test-password"

    def __init__(self) -> None:
        self.host_key = paramiko.RSAKey.generate(2048)
        self.commands: list[str] = []
        self.auth_attempts: list[tuple[str, str]] = []
        self.pager_advances = 0
        self.errors: list[str] = []
        self.port = 0
        self._listener: socket.socket | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._accept_thread: threading.Thread | None = None
        self._client_threads: list[threading.Thread] = []

    def __enter__(self) -> LoopbackArubaSSHServer:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        listener.settimeout(0.1)
        self.port = int(listener.getsockname()[1])
        self._listener = listener
        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            name="loopback-aruba-accept",
            daemon=True,
        )
        self._accept_thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self._stop.set()
        if self._listener is not None:
            with suppress(OSError):
                self._listener.close()
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2)
        for thread in tuple(self._client_threads):
            thread.join(timeout=2)

    def _accept_loop(self) -> None:
        assert self._listener is not None
        while not self._stop.is_set():
            try:
                client, address = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if not self._stop.is_set():
                    self.errors.append("listener_error")
                return
            if address[0] != "127.0.0.1":
                client.close()
                self.errors.append("non_loopback_client")
                continue
            thread = threading.Thread(
                target=self._handle_client,
                args=(client,),
                name="loopback-aruba-client",
                daemon=True,
            )
            self._client_threads.append(thread)
            thread.start()

    def _handle_client(self, client: socket.socket) -> None:
        transport: paramiko.Transport | None = None
        channel: paramiko.Channel | None = None
        try:
            transport = paramiko.Transport(client)
            transport.add_server_key(self.host_key)
            server = _ServerInterface(self)
            transport.start_server(server=server)

            while transport.is_active() and not self._stop.is_set():
                channel = transport.accept(timeout=0.1)
                if channel is not None:
                    break
            if channel is None:
                return  # Host-key-only preflight deliberately closes before auth.
            if not server.shell_requested.wait(timeout=2):
                self.errors.append("shell_not_requested")
                return

            channel.settimeout(0.1)
            channel.sendall(self.prompt.encode("utf-8"))
            command_buffer = ""
            previous_was_cr = False
            waiting_for_pager = False
            while transport.is_active() and not self._stop.is_set():
                try:
                    received = channel.recv(4096)
                except TimeoutError:
                    continue
                if not received:
                    return
                for character in received.decode("utf-8", errors="strict"):
                    if waiting_for_pager and character == " ":
                        waiting_for_pager = False
                        with self._lock:
                            self.pager_advances += 1
                        channel.sendall(
                            ("\r\nvlan 1\r\n   name DEFAULT_VLAN\r\n" + self.prompt).encode("utf-8")
                        )
                        continue
                    if character == "\n" and previous_was_cr:
                        previous_was_cr = False
                        continue
                    if character in {"\r", "\n"}:
                        previous_was_cr = character == "\r"
                        command = command_buffer.strip()
                        command_buffer = ""
                        if not command:
                            channel.sendall(self.prompt.encode("utf-8"))
                            continue
                        waiting_for_pager = self._respond(channel, command)
                        continue
                    previous_was_cr = False
                    command_buffer += character
        except EOFError, OSError, paramiko.SSHException:
            # The unauthenticated host-key probe intentionally tears down early.
            if transport is not None and transport.is_active() and not self._stop.is_set():
                self.errors.append("client_transport_error")
        finally:
            if channel is not None:
                with suppress(Exception):
                    channel.close()
            if transport is not None:
                transport.close()
            client.close()

    def _respond(self, channel: paramiko.Channel, command: str) -> bool:
        with self._lock:
            self.commands.append(command)
        prefix = f"{command}\r\n"
        if command == "no page" or command == "terminal width 511":
            channel.sendall((prefix + self.prompt).encode("utf-8"))
            return False
        if command == "show version":
            channel.sendall(
                (
                    prefix
                    + "Aruba JL253A 2930F-24G-4SFP+ Switch\r\n"
                    + "Software revision WC.16.11.0025\r\n"
                    + self.prompt
                ).encode("utf-8")
            )
            return False
        if command == "show modules":
            channel.sendall(
                (
                    prefix
                    + "Status and Counters - Module Information\r\n"
                    + "Chassis: Aruba 2930F-24G-4SFP+ JL253A\r\n"
                    + self.prompt
                ).encode("utf-8")
            )
            return False
        if command == "show running-config":
            channel.sendall(
                (prefix + "Running configuration:\r\n" + "hostname edge-lab\r\n" + PAGER).encode(
                    "utf-8"
                )
            )
            return True
        channel.sendall((prefix + "% Invalid input\r\n" + self.prompt).encode("utf-8"))
        return False


def wait_for(predicate: object, *, timeout: float = 2.0) -> bool:
    """Poll a zero-argument callable without introducing a long blocking sleep."""

    callback = predicate
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if callable(callback) and callback():
            return True
        time.sleep(0.01)
    return False
