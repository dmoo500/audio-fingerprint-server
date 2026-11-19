"""Verify that credential safeguards reject newly introduced synthetic secrets."""

import secrets
import shlex
import subprocess
import sys
from pathlib import Path

import pytest


def test_private_key_hook_rejects_key_material(tmp_path: Path) -> None:
    pytest.importorskip("pre_commit_hooks")
    candidate = tmp_path / "synthetic.txt"
    candidate.write_text("-----BEGIN " + "RSA PRIVATE KEY-----\nnot-a-real-key\n")
    result = subprocess.run(
        [sys.executable, "-m", "pre_commit_hooks.detect_private_key", str(candidate)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "private key" in result.stdout.lower()


def test_secret_hook_rejects_new_tokens(tmp_path: Path) -> None:
    pytest.importorskip("detect_secrets")
    root = Path(__file__).resolve().parents[1]
    candidate = tmp_path / "synthetic.py"
    candidate.write_text("api_" + "key = " + repr(secrets.token_urlsafe(40)) + "\n")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "detect_secrets.pre_commit_hook",
            "--no-verify",
            "--baseline",
            str(root / ".secrets.baseline"),
            str(candidate),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "secret" in (result.stdout + result.stderr).lower()


def test_lockfile_integrity_exclusion_does_not_hide_credentials(tmp_path: Path) -> None:
    """Ignore package checksums while continuing to detect credentials in lockfiles."""
    pytest.importorskip("detect_secrets")
    yaml = pytest.importorskip("yaml")
    root = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((root / ".pre-commit-config.yaml").read_text())
    hooks = config["repos"][0]["hooks"]
    entry = next(hook["entry"] for hook in hooks if hook["id"] == "detect-secrets")
    args = shlex.split(entry)[2:]
    candidate = tmp_path / "pnpm-lock.yaml"
    checksum = next(
        line
        for line in (root / "recognition-frontend" / "pnpm-lock.yaml").read_text().splitlines()
        if "integrity: sha512-" in line
    )
    candidate.write_text(f"packages:\n  example:\n  {checksum}\n")
    command = [sys.executable, "-m", "detect_secrets.pre_commit_hook", *args, str(candidate)]
    clean = subprocess.run(command, cwd=root, capture_output=True, text=True, check=False)
    assert clean.returncode == 0, clean.stdout + clean.stderr
    with candidate.open("a") as file:
        file.write("api_" + "key: " + secrets.token_urlsafe(40) + "\n")
    detected = subprocess.run(command, cwd=root, capture_output=True, text=True, check=False)
    assert detected.returncode == 1
