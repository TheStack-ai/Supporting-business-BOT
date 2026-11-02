"""Encrypted bot state on a dedicated Git branch; failed restores never start fresh."""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import zlib

from cryptography.fernet import Fernet, InvalidToken

BRANCH = "bot-state"
REQUIRED_FILES = frozenset({"notified_keys.json", "program_state.json", "bot.db"})
OPTIONAL_FILES = frozenset({"decisions.jsonl", "recovery_state.json"})
STATE_FILES = REQUIRED_FILES | OPTIONAL_FILES
REVISION_FILE = ".state-revision"
MAX_PAYLOAD_BYTES = 64 * 1024 * 1024


def git(*args: str, input: bytes | None = None, env: dict | None = None) -> bytes:
    result = subprocess.run(
        ["git", *args], input=input, capture_output=True, env=env, timeout=120
    )
    if result.returncode:
        # Git's stderr can contain access-bearing remote URLs.
        raise RuntimeError("state_git_operation_failed:" + args[0])
    return result.stdout


def cipher() -> Fernet:
    key = os.getenv("STATE_ENCRYPTION_KEY", "")
    if not key:
        raise RuntimeError("state_encryption_key_missing")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError("state_encryption_key_invalid") from exc


def remote_head(*, allow_missing: bool = False) -> str | None:
    ref = "refs/heads/" + BRANCH
    listing = git("ls-remote", "--heads", "origin", ref).strip()
    if not listing:
        if allow_missing:
            return None
        raise RuntimeError("durable_state_branch_missing")
    git("fetch", "--quiet", "--no-tags", "origin", ref)
    return git("rev-parse", "FETCH_HEAD").decode("ascii").strip()


def _validate_directory(directory: Path) -> None:
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise RuntimeError("unsafe_state_directory")
    for name in STATE_FILES | {REVISION_FILE}:
        path = directory / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise RuntimeError("unsafe_state_path:" + name)


def _json_object(raw: bytes, name: str) -> dict:
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError("invalid_state_file:" + name) from exc
    if not isinstance(value, dict):
        raise RuntimeError("invalid_state_file:" + name)
    return value


def _validate_files(files: dict[str, bytes]) -> None:
    if not REQUIRED_FILES <= files.keys() or not files.keys() <= STATE_FILES:
        raise RuntimeError("invalid_state_file_manifest")
    notified = _json_object(files["notified_keys.json"], "notified_keys.json")
    entries = notified.get("entries")
    if not isinstance(entries, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in entries.items()
    ):
        raise RuntimeError("invalid_state_file:notified_keys.json")
    programs = _json_object(files["program_state.json"], "program_state.json")
    snapshots = programs.get("programs")
    if programs.get("schema_version") != 1 or not isinstance(snapshots, dict):
        raise RuntimeError("invalid_state_file:program_state.json")
    snapshot_fields = (
        "source", "title", "status", "apply_start_at", "apply_end_at", "agency", "summary_raw"
    )
    if not isinstance(programs.get("updated_at"), str) or any(
        not isinstance(snapshot, dict)
        or any(not isinstance(snapshot.get(field), str) for field in snapshot_fields)
        for snapshot in snapshots.values()
    ):
        raise RuntimeError("invalid_state_file:program_state.json")
    if "recovery_state.json" in files:
        _json_object(files["recovery_state.json"], "recovery_state.json")
    if "decisions.jsonl" in files:
        for line in files["decisions.jsonl"].splitlines():
            if line.strip():
                _json_object(line, "decisions.jsonl")
    if not files["bot.db"].startswith(b"SQLite format 3\x00"):
        raise RuntimeError("invalid_state_file:bot.db")
    with tempfile.TemporaryDirectory() as temporary:
        snapshot = Path(temporary) / "bot.db"
        snapshot.write_bytes(files["bot.db"])
        try:
            with sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True) as db:
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("state_database_integrity_failed")
                tables = {
                    row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                if not {"programs", "company_profile"} <= tables:
                    raise RuntimeError("invalid_state_database_schema")
        except sqlite3.DatabaseError as exc:
            raise RuntimeError("state_database_integrity_failed") from exc


def snapshot_files(directory: Path) -> dict[str, bytes]:
    _validate_directory(directory)
    if not all((directory / name).is_file() for name in REQUIRED_FILES):
        raise RuntimeError("required_state_file_missing")
    files = {
        name: (directory / name).read_bytes()
        for name in STATE_FILES - {"bot.db"}
        if (directory / name).exists()
    }
    # SQLite backup includes committed WAL data without copying a live database file.
    with tempfile.TemporaryDirectory() as temporary:
        target = Path(temporary) / "bot.db"
        try:
            with sqlite3.connect(
                (directory / "bot.db").resolve().as_uri() + "?mode=ro", uri=True
            ) as source, sqlite3.connect(target) as destination:
                source.backup(destination)
        except sqlite3.DatabaseError as exc:
            raise RuntimeError("state_database_integrity_failed") from exc
        files["bot.db"] = target.read_bytes()
    _validate_files(files)
    return files


def encrypt_files(files: dict[str, bytes]) -> bytes:
    _validate_files(files)
    payload = json.dumps(
        {"schema_version": 1, "files": {
            name: base64.b64encode(content).decode("ascii")
            for name, content in sorted(files.items())
        }}, separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise RuntimeError("state_snapshot_too_large")
    return cipher().encrypt(zlib.compress(payload, 9))


def decrypt_files(encrypted: bytes) -> dict[str, bytes]:
    try:
        compressed = cipher().decrypt(encrypted)
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, MAX_PAYLOAD_BYTES + 1)
        if len(raw) > MAX_PAYLOAD_BYTES or not decoder.eof or decoder.unused_data:
            raise RuntimeError("invalid_state_archive")
        payload = _json_object(raw, "archive")
        manifest = payload.get("files")
        if payload.get("schema_version") != 1 or not isinstance(manifest, dict):
            raise RuntimeError("invalid_state_archive")
        # No archive extraction, paths, links, or unknown filenames are accepted.
        if not REQUIRED_FILES <= manifest.keys() or not manifest.keys() <= STATE_FILES:
            raise RuntimeError("invalid_state_file_manifest")
        files = {
            name: base64.b64decode(content, validate=True)
            for name, content in manifest.items()
        }
        _validate_files(files)
        return files
    except (InvalidToken, zlib.error, TypeError, ValueError, binascii.Error) as exc:
        raise RuntimeError("state_archive_authentication_or_format_failed") from exc


def _write_revision(directory: Path, revision: str) -> None:
    path = directory / REVISION_FILE
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="ascii", dir=directory,
        prefix=REVISION_FILE + "-", delete=False,
    ) as stream:
        temporary = Path(stream.name)
        stream.write(revision + "\n")
    try:
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def restore(path: str | Path) -> str:
    directory = Path(path)
    _validate_directory(directory)
    head = remote_head()
    assert head is not None
    files = decrypt_files(git("show", head + ":state.enc"))
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Validate and stage every byte before replacing any state. A partial install
    # has no revision marker and therefore cannot subsequently publish itself.
    with tempfile.TemporaryDirectory(prefix=".restore-", dir=directory) as temporary:
        staged = Path(temporary)
        for name, content in files.items():
            target = staged / name
            target.write_bytes(content)
            target.chmod(0o600)
        (directory / REVISION_FILE).unlink(missing_ok=True)
        for name in STATE_FILES:
            if name in files:
                (staged / name).replace(directory / name)
            else:
                (directory / name).unlink(missing_ok=True)
        # Never let sidecars from a previously opened DB replay into the snapshot.
        for suffix in ("-wal", "-shm"):
            (directory / ("bot.db" + suffix)).unlink(missing_ok=True)
        _write_revision(directory, head)
    return head


def save(path: str | Path, *, initialize: bool = False) -> str:
    directory = Path(path)
    files = snapshot_files(directory)
    encrypted = encrypt_files(files)
    head = remote_head(allow_missing=initialize)
    if initialize and head is not None:
        raise RuntimeError("durable_state_already_initialized")
    revision = directory / REVISION_FILE
    if head is not None and (
        not revision.exists() or revision.read_text(encoding="ascii").strip() != head
    ):
        raise RuntimeError("stale_state_snapshot_restore_latest_before_writing")
    blob = git("hash-object", "-w", "--stdin", input=encrypted).decode("ascii").strip()
    with tempfile.TemporaryDirectory() as temporary:
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = str(Path(temporary) / "index")
        if head:
            git("read-tree", head, env=env)
            previous = git("rev-parse", head + ":state.enc").decode("ascii").strip()
            git("update-index", "--add", "--cacheinfo", "100644", previous, "previous.enc", env=env)
        else:
            git("read-tree", "--empty", env=env)
        git("update-index", "--add", "--cacheinfo", "100644", blob, "state.enc", env=env)
        tree = git("write-tree", env=env).decode("ascii").strip()
        env.update(
            GIT_AUTHOR_NAME="Bot state service",
            GIT_AUTHOR_EMAIL="bot-state@users.noreply.github.com",
            GIT_COMMITTER_NAME="Bot state service",
            GIT_COMMITTER_EMAIL="bot-state@users.noreply.github.com",
        )
        parents = ("-p", head) if head else ()
        commit = git(
            "commit-tree", tree, *parents,
            input=b"Persist encrypted bot state\n", env=env,
        ).decode("ascii").strip()
        # A race after remote_head is rejected by the normal fast-forward check.
        git("push", "--quiet", "origin", commit + ":refs/heads/" + BRANCH)
    _write_revision(directory, commit)
    return commit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("restore", "save"))
    parser.add_argument("path")
    parser.add_argument("--initialize", action="store_true")
    args = parser.parse_args()
    if args.initialize and args.mode != "save":
        parser.error("--initialize is only valid with save")
    try:
        revision = restore(args.path) if args.mode == "restore" else save(
            args.path, initialize=args.initialize
        )
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        # Keep local paths and subprocess stderr out of GitHub logs as well.
        message = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        parser.exit(1, "state_archive_failed: " + message + "\n")
    print(json.dumps({"operation": args.mode, "revision": revision, "status": "ok"}))


if __name__ == "__main__":
    main()
