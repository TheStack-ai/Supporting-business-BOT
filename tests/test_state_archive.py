import base64
import json
from pathlib import Path
import sqlite3
import subprocess
import zlib

from cryptography.fernet import Fernet
import pytest

from src import state_archive as archive


def run_git(*args, cwd=None):
    return subprocess.check_output(["git", *args], cwd=cwd, stderr=subprocess.PIPE).decode().strip()


@pytest.fixture
def repository(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    run_git("init", "--bare", str(remote))
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    run_git("init", cwd=checkout)
    run_git("remote", "add", "origin", str(remote), cwd=checkout)
    monkeypatch.chdir(checkout)
    monkeypatch.setenv("STATE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    return checkout, remote


def create_state(directory: Path):
    directory.mkdir()
    (directory / "notified_keys.json").write_text(json.dumps({
        "entries": {"support:known": "2026-10-09T02:47:00+00:00"},
        "updated_at": "2026-10-09T02:47:00+00:00",
    }))
    (directory / "program_state.json").write_text(json.dumps({
        "schema_version": 1, "updated_at": "2026-10-09T02:47:00+00:00", "programs": {},
    }))
    with sqlite3.connect(directory / "bot.db") as db:
        db.execute("CREATE TABLE programs (program_key TEXT PRIMARY KEY)")
        db.execute("INSERT INTO programs VALUES ('support:known')")
        db.execute("CREATE TABLE company_profile (id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO company_profile VALUES (1)")
    (directory / "recovery_state.json").write_text('{"status":"required"}')
    (directory / "decisions.jsonl").write_text('{"key":"support:known","grade":"A"}\n')
    return directory


def test_roundtrip_and_fast_forward_history(repository, tmp_path):
    checkout, remote = repository
    source = create_state(tmp_path / "state")
    first = archive.save(source, initialize=True)
    restored = tmp_path / "restored"
    assert archive.restore(restored) == first
    assert (restored / "notified_keys.json").read_bytes() == (source / "notified_keys.json").read_bytes()
    assert (restored / "recovery_state.json").read_bytes() == (source / "recovery_state.json").read_bytes()
    assert (restored / "bot.db").stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(restored / "bot.db") as db:
        assert db.execute("SELECT program_key FROM programs").fetchall() == [("support:known",)]
    (restored / "decisions.jsonl").write_text('{"key":"support:new","grade":"B"}\n')
    second = archive.save(restored)
    assert run_git("rev-parse", second + "^", cwd=checkout) == first
    assert run_git("rev-parse", second + ":previous.enc", cwd=checkout) == run_git(
        "rev-parse", first + ":state.enc", cwd=checkout
    )
    assert run_git("rev-parse", "refs/heads/bot-state", cwd=remote) == second
    assert run_git("status", "--porcelain", cwd=checkout) == ""


def test_missing_branch_does_not_create_fresh_state(repository, tmp_path):
    directory = tmp_path / "restored"
    with pytest.raises(RuntimeError, match="durable_state_branch_missing"):
        archive.restore(directory)
    assert not directory.exists()
    source = create_state(tmp_path / "source")
    with pytest.raises(RuntimeError, match="durable_state_branch_missing"):
        archive.save(source)
    assert not (source / archive.REVISION_FILE).exists()


def test_initialize_cannot_replace_existing_branch(repository, tmp_path):
    source = create_state(tmp_path / "source")
    archive.save(source, initialize=True)
    with pytest.raises(RuntimeError, match="already_initialized"):
        archive.save(source, initialize=True)


@pytest.mark.parametrize("invalid", ["wrong_key", "tampered"])
def test_invalid_ciphertext_never_changes_local_state(repository, tmp_path, monkeypatch, invalid):
    source = create_state(tmp_path / "source")
    head = archive.save(source, initialize=True)
    target = create_state(tmp_path / "target")
    before = archive.snapshot_files(target)
    (target / archive.REVISION_FILE).write_text("preserve-me")
    if invalid == "wrong_key":
        monkeypatch.setenv("STATE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    else:
        original_git = archive.git

        def altered(*args, **kwargs):
            if args == ("show", head + ":state.enc"):
                return b"not an authenticated archive"
            return original_git(*args, **kwargs)

        monkeypatch.setattr(archive, "git", altered)
    with pytest.raises(RuntimeError, match="authentication_or_format"):
        archive.restore(target)
    assert archive.snapshot_files(target) == before
    assert (target / archive.REVISION_FILE).read_text() == "preserve-me"


@pytest.mark.parametrize("damage", ["missing", "bad_json", "bad_db", "missing_schema"])
def test_invalid_source_never_publishes(repository, tmp_path, damage):
    source = create_state(tmp_path / "source")
    if damage == "missing":
        (source / "notified_keys.json").unlink()
    elif damage == "bad_json":
        (source / "program_state.json").write_text('{"programs":')
    elif damage == "bad_db":
        (source / "bot.db").write_bytes(b"broken database")
    else:
        with sqlite3.connect(source / "bot.db") as db:
            db.execute("DROP TABLE company_profile")
    with pytest.raises(RuntimeError):
        archive.save(source, initialize=True)
    assert archive.remote_head(allow_missing=True) is None
    assert not (source / archive.REVISION_FILE).exists()


def test_stale_and_unrestored_snapshots_cannot_overwrite_newer_state(repository, tmp_path):
    source = create_state(tmp_path / "source")
    first = archive.save(source, initialize=True)
    other = tmp_path / "other"
    archive.restore(other)
    second = archive.save(source)
    assert first != second
    with pytest.raises(RuntimeError, match="stale_state_snapshot"):
        archive.save(other)
    (source / archive.REVISION_FILE).unlink()
    with pytest.raises(RuntimeError, match="stale_state_snapshot"):
        archive.save(source)
    assert archive.remote_head() == second


def test_concurrent_push_is_rejected_without_force(repository, tmp_path, monkeypatch):
    source = create_state(tmp_path / "source")
    first = archive.save(source, initialize=True)
    other = tmp_path / "other"
    archive.restore(other)
    second = archive.save(source)
    # Simulate a writer that observed first just before the other push completed.
    monkeypatch.setattr(archive, "remote_head", lambda **kwargs: first)
    with pytest.raises(RuntimeError, match="state_git_operation_failed:push"):
        archive.save(other)
    _, remote = repository
    assert run_git("rev-parse", "refs/heads/bot-state", cwd=remote) == second
    assert (other / archive.REVISION_FILE).read_text().strip() == first


def test_archive_disallows_unknown_paths(repository, tmp_path):
    source = create_state(tmp_path / "source")
    manifest = {name: base64.b64encode(raw).decode() for name, raw in archive.snapshot_files(source).items()}
    manifest["../escaped.txt"] = base64.b64encode(b"do not extract").decode()
    encrypted = archive.cipher().encrypt(zlib.compress(json.dumps({
        "schema_version": 1, "files": manifest,
    }).encode()))
    with pytest.raises(RuntimeError, match="file_manifest"):
        archive.decrypt_files(encrypted)
    assert not (tmp_path / "escaped.txt").exists()


def test_symlinked_state_paths_are_rejected(repository, tmp_path):
    source = create_state(tmp_path / "source")
    destination = tmp_path / "external.json"
    destination.write_text('{"entries":{}}')
    (source / "notified_keys.json").unlink()
    (source / "notified_keys.json").symlink_to(destination)
    with pytest.raises(RuntimeError, match="unsafe_state_path"):
        archive.save(source, initialize=True)
    symlink = tmp_path / "linked-directory"
    symlink.symlink_to(source, target_is_directory=True)
    with pytest.raises(RuntimeError, match="unsafe_state_directory"):
        archive.restore(symlink)


def test_sqlite_wal_commits_are_included(repository, tmp_path):
    source = create_state(tmp_path / "source")
    with sqlite3.connect(source / "bot.db") as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("INSERT INTO programs VALUES ('support:wal')")
        db.commit()
        archive.save(source, initialize=True)
    target = tmp_path / "target"
    archive.restore(target)
    with sqlite3.connect(target / "bot.db") as db:
        assert db.execute("SELECT count(*) FROM programs").fetchone()[0] == 2
