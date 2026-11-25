"""Verify bounded postings, transactional migrations and the SQLite return path."""

import runpy
import sqlite3
from pathlib import Path

import pytest
from test_postgres_migration import migration as migration
from test_postgres_migration import postgres as postgres
from test_postgres_migration import source as source
from test_postgres_reverse_migration import reverse as reverse

from fingerprint_server.database.postgres.postings import insert


@pytest.fixture
def layout(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    return runpy.run_path(str(scripts / "migrate_postgres_layout.py"))


def test_roundtrip(source, postgres, migration, reverse, layout, tmp_path):
    with sqlite3.connect(source) as db:
        migration["migrate"](db, postgres, "postings_test")
    forward = layout["migrate"](postgres, "postings_test", 2)
    assert forward["changed"]
    assert not layout["migrate"](postgres, "postings_test", 2)["changed"]
    restored = tmp_path / "restored.db"
    assert reverse["export_sqlite"](postgres, "postings_test", restored)["counts"]["landmarks"] == 1
    with postgres.transaction():
        postgres.execute("SET LOCAL search_path TO postings_test,pg_catalog")
        # A large internal ID, offset buckets and repeated insertion remain lossless.
        postgres.execute(
            "INSERT INTO reference_tracks(id,reference_key,metadata,created_at) "
            "VALUES (5000000000,'large','{}',0)"
        )
        insert(postgres, 5000000000, [("0" * 20, 0), ("0" * 20, 1024), ("f" * 20, 30000)])
    backwards = layout["migrate"](postgres, "postings_test", 1)
    assert backwards["changed"]
    assert not layout["migrate"](postgres, "postings_test", 1)["changed"]
    again = layout["migrate"](postgres, "postings_test", 2)
    assert again["landmarks_sha256"] == backwards["landmarks_sha256"]
    assert (
        reverse["export_sqlite"](postgres, "postings_test", tmp_path / "restored_again.db")[
            "counts"
        ]["landmarks"]
        == 4
    )


def test_constraints(source, postgres, migration, layout):
    import psycopg

    with sqlite3.connect(source) as db:
        migration["migrate"](db, postgres, "constraints_test")
    layout["migrate"](postgres, "constraints_test", 2)
    with postgres.transaction():
        postgres.execute("SET LOCAL search_path TO constraints_test,pg_catalog")
        for refs, offsets in (
            ([1], []),
            ([1, 1], [1, 1]),
            ([1], [-1]),
            ([1], [1024]),
            ([33], [0]),
            ([1, None], [0, 1]),
            ([], []),
            ([1, 1], [2, 1]),
        ):
            with pytest.raises(psycopg.errors.CheckViolation), postgres.transaction():
                postgres.execute(
                    "INSERT INTO landmark_postings VALUES(%s,0,0,%s,%s)",
                    (bytes.fromhex("f" * 20), refs, offsets),
                )
        with pytest.raises(psycopg.errors.ForeignKeyViolation), postgres.transaction():
            insert(postgres, 2, [("f" * 20, 0)])
        with pytest.raises(psycopg.errors.ForeignKeyViolation), postgres.transaction():
            postgres.execute("DELETE FROM reference_tracks WHERE id=1")


def test_active_service_and_failure_rollback(source, postgres, migration, layout, monkeypatch):
    import psycopg

    with sqlite3.connect(source) as db:
        migration["migrate"](db, postgres, "locked_test")
    with psycopg.connect("", autocommit=True) as owner:
        owner.execute(
            "SELECT pg_advisory_lock_shared(hashtextextended('fingerprint:locked_test:api',0))"
        )
        with pytest.raises(RuntimeError, match="Stop all"):
            layout["migrate"](postgres, "locked_test", 2)
    original = layout["checksum"]
    calls = 0

    def broken(db, table):
        nonlocal calls
        calls += 1
        return original(db, table) if calls == 1 else "corrupted"

    monkeypatch.setitem(layout["migrate"].__globals__, "checksum", broken)
    with pytest.raises(ValueError, match="checksum mismatch"):
        layout["migrate"](postgres, "locked_test", 2)
    with postgres.transaction():
        assert postgres.execute(
            "SELECT format_version FROM locked_test.migration_info"
        ).fetchone() == (3,)
        assert postgres.execute(
            "SELECT to_regclass('locked_test.landmark_postings')"
        ).fetchone() == (None,)
