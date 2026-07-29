import json

from motorcal.config import SeriesConfig
from motorcal.ics import compute_content_hash, render_calendar_bytes, sync_feed_revision
from motorcal.store import connect, get_feed_revision, init_schema, transaction, upsert_published_event


def _fresh_conn(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    return conn


def _insert_published(conn, uid, series="wec", summary="S"):
    upsert_published_event(
        conn, uid=uid, series=series, session_type="race", summary=summary,
        start="2026-04-19T13:00:00+00:00", all_day_date=None, time_confirmed=True,
        duration_seconds=3600, location="L", description="D", status="CONFIRMED",
        sequence=1, dtstamp="2026-01-01T00:00:00+00:00", last_modified="2026-01-01T00:00:00+00:00",
        fingerprint="fp", alarms_json=json.dumps(["-1d"]),
        source_provider="thesportsdb", source_id_event="1", synthetic_uid=None,
        cancelled_at=None, retain_until=None,
    )


def test_render_calendar_bytes_produces_valid_ics_for_series(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert_published(conn, "u1", series="wec")
        _insert_published(conn, "u2", series="f1")  # different series, must be excluded

    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    ics_bytes = render_calendar_bytes(conn, "wec", series_cfg)

    assert b"BEGIN:VCALENDAR" in ics_bytes
    assert b"UID:u1" in ics_bytes
    assert b"UID:u2" not in ics_bytes
    assert b"X-WR-CALNAME:WEC" in ics_bytes


def test_render_calendar_bytes_is_deterministic_across_calls(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert_published(conn, "u1", series="wec")

    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    b1 = render_calendar_bytes(conn, "wec", series_cfg)
    b2 = render_calendar_bytes(conn, "wec", series_cfg)
    assert b1 == b2


def test_compute_content_hash_is_stable_for_identical_bytes():
    assert compute_content_hash(b"hello") == compute_content_hash(b"hello")
    assert compute_content_hash(b"hello") != compute_content_hash(b"world")


def test_sync_feed_revision_creates_a_new_revision_on_first_sync(tmp_path):
    conn = _fresh_conn(tmp_path)
    state = sync_feed_revision(conn, "wec", b"content-v1", now="t1")

    assert state.revision == compute_content_hash(b"content-v1")
    assert state.updated_at == "t1"
    row = get_feed_revision(conn, "wec")
    assert row["revision"] == state.revision
    assert row["updated_at"] == "t1"


def test_sync_feed_revision_does_not_advance_updated_at_when_content_is_unchanged(tmp_path):
    conn = _fresh_conn(tmp_path)
    sync_feed_revision(conn, "wec", b"content-v1", now="t1")

    state = sync_feed_revision(conn, "wec", b"content-v1", now="t2")  # same bytes, later "now"

    assert state.updated_at == "t1"  # unchanged -- this is the determinism guarantee
    row = get_feed_revision(conn, "wec")
    assert row["updated_at"] == "t1"


def test_sync_feed_revision_advances_when_content_changes(tmp_path):
    conn = _fresh_conn(tmp_path)
    sync_feed_revision(conn, "wec", b"content-v1", now="t1")

    state = sync_feed_revision(conn, "wec", b"content-v2", now="t2")

    assert state.revision == compute_content_hash(b"content-v2")
    assert state.updated_at == "t2"
    row = get_feed_revision(conn, "wec")
    assert row["revision"] == state.revision
    assert row["updated_at"] == "t2"
