import time

from motorcal.store import (
    acquire_lease,
    connect,
    current_lease_holder,
    init_schema,
    release_lease,
)


def _fresh_conn(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    return conn


def test_acquire_lease_succeeds_when_no_lease_exists(tmp_path):
    conn = _fresh_conn(tmp_path)
    assert acquire_lease(conn, "worker-a", ttl_seconds=60, now=1000.0) is True
    assert current_lease_holder(conn, now=1000.0) == "worker-a"


def test_second_acquire_fails_while_first_lease_is_live(tmp_path):
    conn = _fresh_conn(tmp_path)
    assert acquire_lease(conn, "worker-a", ttl_seconds=60, now=1000.0) is True
    assert acquire_lease(conn, "worker-b", ttl_seconds=60, now=1010.0) is False
    assert current_lease_holder(conn, now=1010.0) == "worker-a"


def test_acquire_succeeds_after_expiry(tmp_path):
    conn = _fresh_conn(tmp_path)
    assert acquire_lease(conn, "worker-a", ttl_seconds=60, now=1000.0) is True
    # worker-a's lease expires at 1060.0; worker-b tries at 1100.0.
    assert acquire_lease(conn, "worker-b", ttl_seconds=60, now=1100.0) is True
    assert current_lease_holder(conn, now=1100.0) == "worker-b"


def test_release_lease_removes_own_lease(tmp_path):
    conn = _fresh_conn(tmp_path)
    acquire_lease(conn, "worker-a", ttl_seconds=60, now=1000.0)
    release_lease(conn, "worker-a")
    assert current_lease_holder(conn, now=1000.0) is None


def test_release_lease_does_not_remove_someone_elses_lease(tmp_path):
    conn = _fresh_conn(tmp_path)
    acquire_lease(conn, "worker-a", ttl_seconds=60, now=1000.0)
    release_lease(conn, "worker-b")  # not the holder — must be a no-op
    assert current_lease_holder(conn, now=1000.0) == "worker-a"


def test_current_lease_holder_is_none_when_never_acquired(tmp_path):
    conn = _fresh_conn(tmp_path)
    assert current_lease_holder(conn, now=1000.0) is None


def test_two_connections_racing_for_the_lease_only_one_wins(tmp_path):
    # Simulates two separate processes/workers each holding their own connection
    # to the same database file, both trying to acquire the lease at the same moment.
    db_path = tmp_path / "shared.db"
    conn_a = connect(db_path)
    init_schema(conn_a)
    conn_b = connect(db_path)

    now = time.time()
    result_a = acquire_lease(conn_a, "worker-a", ttl_seconds=60, now=now)
    result_b = acquire_lease(conn_b, "worker-b", ttl_seconds=60, now=now)

    assert result_a is True
    assert result_b is False
    assert current_lease_holder(conn_a, now=now) == "worker-a"
