from fastapi.testclient import TestClient

from motorcal.config import DefaultsConfig, DurationDefaults, RetentionConfig, RootConfig, UnknownTimeConfig
from motorcal.store import connect, init_schema
from motorcal.web import create_app, verify_token


def _root_config():
    return RootConfig(
        server={"base_url": "https://x.example.com", "uid_domain": "x.example.com"},
        source={"refresh_cron": "0 * * * *"},
        retention=RetentionConfig(),
        defaults=DefaultsConfig(durations=DurationDefaults(), alerts={}, include_sessions=["race"]),
        unknown_time=UnknownTimeConfig(),
        series={},
    )


def test_verify_token_accepts_a_configured_token():
    assert verify_token("good-token", ["good-token", "other-token"]) is True


def test_verify_token_rejects_an_unconfigured_token():
    assert verify_token("bad-token", ["good-token"]) is False


def test_verify_token_rejects_against_empty_token_list():
    assert verify_token("anything", []) is False


def test_livez_returns_ok_for_a_healthy_database(tmp_path):
    db_path = tmp_path / "test.db"
    conn = connect(db_path)
    init_schema(conn)
    conn.close()

    app = create_app(db_path, _root_config(), tokens=["t"])
    client = TestClient(app)
    response = client.get("/livez")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_livez_returns_503_for_a_corrupt_database(tmp_path):
    db_path = tmp_path / "test.db"
    conn = connect(db_path)
    init_schema(conn)
    conn.close()
    with open(db_path, "r+b") as f:
        f.seek(100)
        f.write(b"\xff" * 200)

    app = create_app(db_path, _root_config(), tokens=["t"])
    client = TestClient(app)
    response = client.get("/livez")

    assert response.status_code == 503
