"""Guard package resources and documentation links after source reorganization."""

import importlib.resources
import runpy
from pathlib import Path


def test_documentation_links_resolve() -> None:
    root = Path(__file__).resolve().parents[1]
    check = runpy.run_path(str(root / "scripts" / "check_docs.py"))["check_links"]
    assert check(root) == []


def test_admin_templates_are_package_resources() -> None:
    templates = importlib.resources.files("fingerprint_server").joinpath("admin", "templates")
    for name in ("base.html", "login.html", "admin_keys.html", "admin_overview.html"):
        assert templates.joinpath(name).is_file()
