"""Verify that credential safeguards reject newly introduced synthetic secrets."""

import secrets
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
