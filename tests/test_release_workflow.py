from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECK_SCRIPT = ROOT / "tools" / "check_release_ref.py"


@dataclass(frozen=True)
class ReleaseRepository:
    source: Path
    checkout: Path
    first_commit: str


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture()
def release_repository(tmp_path: Path) -> ReleaseRepository:
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    checkout = tmp_path / "checkout"

    remote.mkdir()
    source.mkdir()
    checkout.mkdir()
    _git(remote, "init", "--bare")
    _git(source, "init", "--initial-branch=main")
    _git(source, "config", "user.name", "Release Test")
    _git(source, "config", "user.email", "release-test@example.invalid")
    (source / "marker.txt").write_text("first\n", encoding="utf-8")
    _git(source, "add", "marker.txt")
    _git(source, "commit", "-m", "first")
    first_commit = _git(source, "rev-parse", "HEAD")
    _git(source, "remote", "add", "origin", str(remote))
    _git(source, "push", "origin", "main")
    _git(source, "tag", "-a", "v1.2.3", "-m", "v1.2.3", first_commit)
    _git(source, "push", "origin", "v1.2.3")

    _git(checkout, "init")
    _git(checkout, "remote", "add", "origin", str(remote))
    _git(checkout, "fetch", "--no-tags", "origin", "main")
    _git(checkout, "checkout", "--detach", first_commit)
    # Reproduce actions/checkout replacing the local annotated tag ref with a commit.
    _git(checkout, "tag", "v1.2.3", first_commit)

    return ReleaseRepository(source, checkout, first_commit)


def _run_gate(repository: Path, tag: str, event_sha: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECK_SCRIPT), "--tag", tag, "--event-sha", event_sha],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def test_release_gate_uses_remote_annotated_tag_when_local_tag_was_clobbered(
    release_repository: ReleaseRepository,
) -> None:
    local_type = _git(release_repository.checkout, "cat-file", "-t", "refs/tags/v1.2.3")
    result = _run_gate(release_repository.checkout, "v1.2.3", release_repository.first_commit)

    assert local_type == "commit"
    assert result.returncode == 0, result.stderr
    assert release_repository.first_commit in result.stdout


def test_release_gate_rejects_remote_lightweight_tag(
    release_repository: ReleaseRepository,
) -> None:
    _git(
        release_repository.source,
        "tag",
        "v1.2.4",
        release_repository.first_commit,
    )
    _git(release_repository.source, "push", "origin", "v1.2.4")

    result = _run_gate(release_repository.checkout, "v1.2.4", release_repository.first_commit)

    assert result.returncode == 1
    assert "annotated Git tag" in result.stderr


def test_release_gate_rejects_event_sha_mismatch(
    release_repository: ReleaseRepository,
) -> None:
    checkout = release_repository.checkout
    _git(checkout, "config", "user.name", "Release Test")
    _git(checkout, "config", "user.email", "release-test@example.invalid")
    _git(checkout, "commit", "--allow-empty", "-m", "different event")
    different_event = _git(checkout, "rev-parse", "HEAD")
    _git(checkout, "checkout", "--detach", release_repository.first_commit)

    result = _run_gate(checkout, "v1.2.3", different_event)

    assert result.returncode == 1
    assert "do not resolve to one commit" in result.stderr


def test_release_gate_rejects_current_main_mismatch(
    release_repository: ReleaseRepository,
) -> None:
    source = release_repository.source
    (source / "marker.txt").write_text("second\n", encoding="utf-8")
    _git(source, "add", "marker.txt")
    _git(source, "commit", "-m", "second")
    _git(source, "push", "origin", "main")

    result = _run_gate(release_repository.checkout, "v1.2.3", release_repository.first_commit)

    assert result.returncode == 1
    assert "do not resolve to one commit" in result.stderr


def test_release_gate_rejects_missing_remote_tag(
    release_repository: ReleaseRepository,
) -> None:
    result = _run_gate(release_repository.checkout, "v9.9.9", release_repository.first_commit)

    assert result.returncode == 1
    assert "Git fetch operation failed" in result.stderr


def test_release_checkout_does_not_persist_publish_credentials() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    checkout_block, _ = workflow.split("- name: Set up Python", maxsplit=1)
    assert "persist-credentials: false" in checkout_block


def test_release_workflow_cannot_mask_native_gate_failures() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    ref_step = workflow.split(
        "- name: Require a version tag on the current main commit", maxsplit=1
    )[1].split("- name: Require synchronized release version", maxsplit=1)[0]
    version_step = workflow.split("- name: Require synchronized release version", maxsplit=1)[
        1
    ].split("- name: Install locked runtime", maxsplit=1)[0]

    assert "python tools/check_release_ref.py" in ref_step
    assert "check_version.py" not in ref_step
    assert "if ($LASTEXITCODE -ne 0)" in ref_step
    assert "python tools/check_version.py" in version_step
    assert "if ($LASTEXITCODE -ne 0)" in version_step


@pytest.mark.parametrize("workflow_name", ["ci.yml", "release.yml"])
def test_workflow_dependency_installation_fails_closed(workflow_name: str) -> None:
    workflow = (ROOT / ".github" / "workflows" / workflow_name).read_text(encoding="utf-8")
    install_step = workflow.split(
        "- name: Install locked runtime and development tools", maxsplit=1
    )[1].split("- name:", maxsplit=1)[0]

    assert install_step.count("python -m pip install") == 3
    assert install_step.count("if ($LASTEXITCODE -ne 0)") == 3


def test_dependency_audit_has_only_the_documented_legacy_ssh_exception() -> None:
    validation = (ROOT / "tools" / "validate.ps1").read_text(encoding="utf-8")
    security_policy = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

    assert validation.count('"--ignore-vuln"') == 1
    assert validation.count('"PYSEC-2026-2858"') == 1
    assert "PYSEC-2026-2858" in security_policy
    assert "CVE-2026-44405" in security_policy


def test_publish_job_rechecks_remote_refs_and_artifact_provenance() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    build_job, publish_job = workflow.split("  publish-prerelease:", maxsplit=1)
    verify_position = publish_job.index("Reverify provenance and create GitHub prerelease")
    create_position = publish_job.index("gh release create")

    assert "verified_commit: ${{ steps.release_ref.outputs.commit }}" in build_job
    assert "EXPECTED_COMMIT: ${{ needs.windows_build.outputs.verified_commit }}" in publish_job
    assert "git/ref/tags/$env:RELEASE_TAG" in publish_job
    assert "git/tags/$($tagRef.object.sha)" in publish_job
    assert "git/ref/heads/main" in publish_job
    assert "BUILD_INFO\\.json" in publish_job
    assert "Get-FileHash -LiteralPath $zip -Algorithm SHA256" in publish_job
    assert verify_position < create_position
    assert "actions/checkout" not in publish_job
    assert "gh release view" not in publish_job


def test_publish_sidecar_pattern_executes_in_powershell() -> None:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is unavailable")

    workflow_lines = (
        (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8").splitlines()
    )
    start = next(
        index
        for index, line in enumerate(workflow_lines)
        if line.strip().startswith("$sidecarPattern =")
    )
    stop = next(
        index
        for index in range(start + 1, len(workflow_lines))
        if workflow_lines[index].strip().startswith("$sidecarMatch =")
    )
    pattern_expression = "\n".join(line.strip() for line in workflow_lines[start:stop])
    zip_name = "Aruba2930FConfigBackup_v1.2.3_windows_x64.zip"
    script = "\n".join(
        (
            f"$zipName = '{zip_name}'",
            pattern_expression,
            "$sidecarText = " + "'" + ("a" * 64) + f"  {zip_name}'",
            "$match = [regex]::Match($sidecarText, $sidecarPattern)",
            "if (-not $match.Success) { exit 1 }",
        )
    )

    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 0, result.stderr
