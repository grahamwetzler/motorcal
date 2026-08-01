from tests.conftest import UID_DOMAIN, make_config, make_event, make_series, write_config_dir

from motorcal import state as state_module
from motorcal.cli import main
from motorcal.state import State


def _config_dir(tmp_path):
    return write_config_dir(
        tmp_path,
        make_config(series={"wec": make_series(events=[make_event("wec-2026-imola-race")])}),
    )


def test_main_with_no_subcommand_returns_1(capsys):
    assert main([]) == 1
    assert capsys.readouterr().err != ""


def test_validate_config_accepts_a_valid_directory(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("UID_DOMAIN", UID_DOMAIN)
    exit_code = main(["validate-config", "--config", str(_config_dir(tmp_path))])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "valid" in out
    assert "1 series, 1 events" in out


def test_validate_config_rejects_an_invalid_directory(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("UID_DOMAIN", UID_DOMAIN)
    config_dir = _config_dir(tmp_path)
    (config_dir / "wec.yaml").write_text("name: X\nevents: not_a_list\n")

    assert main(["validate-config", "--config", str(config_dir)]) == 1
    assert capsys.readouterr().err != ""


def test_validate_config_rejects_a_missing_directory(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("UID_DOMAIN", UID_DOMAIN)
    assert main(["validate-config", "--config", str(tmp_path / "nope")]) == 1
    assert capsys.readouterr().err != ""


def test_validate_config_rejects_a_directory_with_no_series(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("UID_DOMAIN", UID_DOMAIN)
    config_dir = _config_dir(tmp_path)
    (config_dir / "wec.yaml").unlink()

    assert main(["validate-config", "--config", str(config_dir)]) == 1
    assert "No series files" in capsys.readouterr().err


def test_validate_config_requires_uid_domain(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("UID_DOMAIN", raising=False)

    assert main(["validate-config", "--config", str(_config_dir(tmp_path))]) == 1
    assert "UID_DOMAIN" in capsys.readouterr().err


def test_serve_refuses_to_start_when_uid_domain_has_changed(tmp_path, capsys, monkeypatch):
    config_dir = _config_dir(tmp_path)
    state_path = tmp_path / "state.yaml"
    state_module.save(state_path, State(uid_domain="old.example.com"))
    monkeypatch.setenv("UID_DOMAIN", UID_DOMAIN)

    # UID_DOMAIN (racing.example.com) differs from the domain already bound
    # above -- this must be refused before starting the scheduler or the HTTP
    # server (uvicorn.run would otherwise block forever).
    exit_code = main(["serve", "--config", str(config_dir), "--state", str(state_path)])

    assert exit_code == 1
    assert "uid_domain" in capsys.readouterr().err


def test_serve_requires_uid_domain(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("UID_DOMAIN", raising=False)

    exit_code = main([
        "serve", "--config", str(_config_dir(tmp_path)), "--state", str(tmp_path / "state.yaml"),
    ])

    assert exit_code == 1
    assert "UID_DOMAIN" in capsys.readouterr().err
