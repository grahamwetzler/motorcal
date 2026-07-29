import threading

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


def test_same_holder_can_renew_before_expiry(tmp_path):
    conn = _fresh_conn(tmp_path)
    assert acquire_lease(conn, "worker-a", ttl_seconds=60, now=1000.0) is True
    assert acquire_lease(conn, "worker-a", ttl_seconds=60, now=1030.0) is True  # renewal, not expired yet
    assert current_lease_holder(conn, now=1085.0) == "worker-a"  # expiry extended by the renewal
    assert current_lease_holder(conn, now=1091.0) is None  # now past the renewed expiry (1030+60=1090)


def test_two_connections_racing_for_the_lease_only_one_wins(tmp_path):
    db_path = tmp_path / "shared.db"
    setup_conn = connect(db_path)
    init_schema(setup_conn)
    setup_conn.close()

    barrier = threading.Barrier(2)
    results = {}

    def worker(name):
        conn = connect(db_path)
        barrier.wait()  # force genuine overlap between the two acquire_lease calls
        results[name] = acquire_lease(conn, name, ttl_seconds=60, now=1000.0)
        conn.close()

    threads = [threading.Thread(target=worker, args=(name,)) for name in ("worker-a", "worker-b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results.values()) == [False, True]
