from pathlib import Path

from motorcal.cli import main
from motorcal.store import check_integrity, connect, init_schema, transaction, upsert_source_event


def test_init_db_creates_and_initializes_database(tmp_path, capsys):
    db_path = tmp_path / "new.db"
    exit_code = main(["init-db", "--db", str(db_path)])
    assert exit_code == 0
    assert db_path.exists()
    captured = capsys.readouterr()
    assert str(db_path) in captured.out

    conn = connect(db_path)
    assert check_integrity(conn) is True


def test_backup_command_copies_database(tmp_path, capsys):
    db_path = tmp_path / "source.db"
    dest_path = tmp_path / "backup.db"
    main(["init-db", "--db", str(db_path)])

    exit_code = main(["backup", "--db", str(db_path), "--dest", str(dest_path)])
    assert exit_code == 0
    assert dest_path.exists()
    captured = capsys.readouterr()
    assert "Backed up" in captured.out


def test_backup_command_refuses_to_back_up_corrupt_database(tmp_path, capsys):
    db_path = tmp_path / "corrupt.db"
    dest_path = tmp_path / "backup.db"
    main(["init-db", "--db", str(db_path)])

    with open(db_path, "r+b") as f:
        f.seek(100)
        f.write(b"\xff" * 200)

    exit_code = main(["backup", "--db", str(db_path), "--dest", str(dest_path)])
    assert exit_code == 1
    assert not dest_path.exists()
    captured = capsys.readouterr()
    assert captured.err != ""


def test_main_with_no_subcommand_returns_1(capsys):
    exit_code = main([])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.err != ""
