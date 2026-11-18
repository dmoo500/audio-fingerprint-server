"""Bounded, lossless PostgreSQL landmark posting lists."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import psycopg

POSTINGS_VERSION = 2
REFERENCE_BLOCK = 32
OFFSET_BLOCK = 1024

CREATE = """
CREATE FUNCTION valid_posting(rb bigint, ob integer, refs bigint[], offsets integer[])
RETURNS boolean LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT cardinality(refs) BETWEEN 1 AND 32768
       AND array_ndims(refs)=1 AND array_ndims(offsets)=1
       AND array_lower(refs,1)=1 AND array_lower(offsets,1)=1
       AND cardinality(refs)=cardinality(offsets)
       AND NOT EXISTS (
           SELECT 1 FROM (
               SELECT r,o,lag(r) OVER (ORDER BY n) pr,lag(o) OVER (ORDER BY n) po
               FROM unnest(refs,offsets) WITH ORDINALITY p(r,o,n)
           ) p WHERE r IS NULL OR o IS NULL OR r<=0 OR o<0
               OR r/32<>rb OR o/1024<>ob
               OR pr>r OR (pr=r AND po>=o)
       )
$$;
CREATE TABLE landmark_postings (
    hash bytea NOT NULL CHECK(octet_length(hash)=10),
    reference_block bigint NOT NULL,
    offset_block integer NOT NULL,
    reference_ids bigint[] NOT NULL,
    offsets integer[] NOT NULL,
    PRIMARY KEY(hash,reference_block,offset_block),
    CHECK(valid_posting(reference_block,offset_block,reference_ids,offsets))
);
CREATE INDEX postings_references ON landmark_postings USING gin(reference_ids);
CREATE FUNCTION check_posting_references() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE ref bigint;
BEGIN
    IF NOT valid_posting(NEW.reference_block,NEW.offset_block,NEW.reference_ids,NEW.offsets)
    THEN RETURN NEW; END IF;
    FOR ref IN SELECT DISTINCT unnest(NEW.reference_ids) LOOP
        PERFORM id FROM reference_tracks WHERE id=ref FOR KEY SHARE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Missing landmark reference %',ref USING ERRCODE='23503';
        END IF;
    END LOOP;
    RETURN NEW;
END
$$;
CREATE TRIGGER postings_reference_check BEFORE INSERT OR UPDATE ON landmark_postings
    FOR EACH ROW EXECUTE FUNCTION check_posting_references();
CREATE FUNCTION protect_posting_reference() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='UPDATE' AND NEW.id=OLD.id THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM landmark_postings WHERE reference_ids @> ARRAY[OLD.id]) THEN
        RAISE EXCEPTION 'Reference % still owns landmarks',OLD.id USING ERRCODE='23503';
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER postings_reference_delete BEFORE DELETE OR UPDATE OF id ON reference_tracks
    FOR EACH ROW EXECUTE FUNCTION protect_posting_reference();
"""

VIEW = """
CREATE VIEW landmarks AS
SELECT hash,p.reference_id,p."offset" FROM landmark_postings
CROSS JOIN LATERAL unnest(reference_ids,offsets) p(reference_id,"offset")
"""


def insert[Row](
    db: psycopg.Connection[Row], reference_id: int, fingerprints: list[tuple[str, int]]
) -> None:
    """Insert a reference into bounded sorted postings within the caller's write transaction."""
    groups: dict[tuple[bytes, int], set[int]] = defaultdict(set)
    for digest, offset in fingerprints:
        offsets = groups[(bytes.fromhex(digest), offset // OFFSET_BLOCK)]
        if offset in offsets:
            raise psycopg.errors.UniqueViolation("Duplicate landmark in reference")
        offsets.add(offset)
    db.execute(
        "CREATE TEMP TABLE new_postings (LIKE landmark_postings INCLUDING CONSTRAINTS) "
        "ON COMMIT DROP"
    )
    with db.cursor().copy("COPY new_postings FROM STDIN") as copy:
        for (binary_digest, block), offsets in groups.items():
            ordered = sorted(offsets)
            copy.write_row(
                (
                    binary_digest,
                    reference_id // REFERENCE_BLOCK,
                    block,
                    [reference_id] * len(ordered),
                    ordered,
                )
            )
    # Existing posting rows are locked by the store's schema-wide write transaction lock.
    db.execute("""
        INSERT INTO landmark_postings AS existing
        SELECT * FROM new_postings
        ON CONFLICT(hash,reference_block,offset_block) DO UPDATE SET
            (reference_ids,offsets)=(
                SELECT array_agg(r ORDER BY r,o),array_agg(o ORDER BY r,o)
                FROM (SELECT DISTINCT r,o
                      FROM unnest(existing.reference_ids || excluded.reference_ids,
                                  existing.offsets || excluded.offsets) p(r,o)) pairs
            )
    """)


def delete(db: psycopg.Connection[dict[str, Any]], reference_id: int) -> int:
    """Remove one reference's postings, preserving every other reference."""
    rows = db.execute(
        "SELECT hash,reference_block,offset_block,reference_ids,offsets "
        "FROM landmark_postings WHERE reference_ids @> ARRAY[%s]::bigint[]",
        (reference_id,),
    ).fetchall()
    removed = 0
    for row in rows:
        remaining = [
            (ref, offset)
            for ref, offset in zip(row["reference_ids"], row["offsets"], strict=True)
            if ref != reference_id
        ]
        removed += len(row["reference_ids"]) - len(remaining)
        key = (row["hash"], row["reference_block"], row["offset_block"])
        if remaining:
            db.execute(
                "UPDATE landmark_postings SET reference_ids=%s,offsets=%s "
                "WHERE hash=%s AND reference_block=%s AND offset_block=%s",
                ([ref for ref, _ in remaining], [offset for _, offset in remaining], *key),
            )
        else:
            db.execute(
                "DELETE FROM landmark_postings "
                "WHERE hash=%s AND reference_block=%s AND offset_block=%s",
                key,
            )
    return removed
