# Aruba 2930F Config Backup

## Purpose

This repository contains a Windows GUI that collects `show running-config`
from Aruba 2930F ArubaOS-Switch devices over SSH. It is read-only: never add
configuration-changing commands or live-device tests without explicit user
approval.

## Development

- Use Python 3.14 and the `src/` package layout.
- Run tests with `python -m pytest`.
- Run repository validation with `powershell -ExecutionPolicy Bypass -File .\tools\validate.ps1`.
- Build the Windows portable package with `powershell -ExecutionPolicy Bypass -File .\build_windows.ps1`.
- Keep SSH, parser, and filesystem boundaries mockable; fixtures must not
  contain real customer addresses, hostnames, credentials, or configurations.

## Security and release rules

- Credentials and device lists are session-only. Never log or persist them.
- Persist only explicitly approved SSH host-key fingerprints and fail closed on
  changes.
- Reapply and verify `no page` on every connection before any `show` command.
- Fixture, package, and smoke-test results are not live-device proof.
- Stage explicit paths only. Do not commit build output, local reports, secrets,
  virtual environments, or generated release archives.
- A release requires passing tests, package verification, SBOM generation, and
  an explicit tag from the merged default branch.
