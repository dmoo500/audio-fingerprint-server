"""Guard package resources and documentation links after source reorganization."""

import importlib.resources
import runpy
from pathlib import Path


def test_documentation_links_resolve() -> None:
    root = Path(__file__).resolve().parents[1]
    check = runpy.run_path(str(root / "scripts" / "check_docs.py"))["check_links"]
    assert check(root) == []


def test_admin_frontend_is_a_package_resource() -> None:
    assets = importlib.resources.files("fingerprint_server").joinpath("admin", "dist")
    for parts in (("index.html",), ("assets", "app.js"), ("assets", "index.css")):
        assert assets.joinpath(*parts).is_file()
