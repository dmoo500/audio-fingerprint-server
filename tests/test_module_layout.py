"""Keep productive Python modules below the agreed size limit."""

from pathlib import Path


def test_productive_python_modules_have_at_most_500_lines() -> None:
    package = Path(__file__).resolve().parents[1] / "fingerprint_server"
    oversized = {
        str(path.relative_to(package)): len(path.read_text().splitlines())
        for path in package.rglob("*.py")
        if len(path.read_text().splitlines()) > 500
    }
    assert not oversized, f"Split productive modules along domain boundaries: {oversized}"
