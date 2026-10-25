"""Durable local extraction queue. Source archives are always opened read-only."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path

import digest

LIBRARY = digest.PROJECT_DIR.parents[1] / "library/whatsapp"
DEFAULT_DB = LIBRARY / "enrichment.sqlite"
DEFAULT_ARCHIVE = LIBRARY / "wacrawl.db"
STATES = ("pending", "running", "done", "failed", "missing", "unsupported")
SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
 sha256 TEXT PRIMARY KEY, kind TEXT NOT NULL, suffix TEXT NOT NULL,
 size_bytes INTEGER NOT NULL, state TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, error TEXT,
 config_key TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS refs (
 source TEXT NOT NULL, ref_id TEXT NOT NULL, path TEXT NOT NULL,
 sha256 TEXT REFERENCES assets(sha256), state TEXT NOT NULL,
 metadata_json TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(source, ref_id)
);
CREATE TABLE IF NOT EXISTS results (
 sha256 TEXT NOT NULL REFERENCES assets(sha256), config_key TEXT NOT NULL,
 markdown TEXT NOT NULL, metadata_json TEXT NOT NULL,
 config_json TEXT NOT NULL, processed_at TEXT NOT NULL,
 PRIMARY KEY(sha256, config_key)
);
CREATE TABLE IF NOT EXISTS attempts (
 id INTEGER PRIMARY KEY, sha256 TEXT NOT NULL REFERENCES assets(sha256),
 config_key TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
 state TEXT NOT NULL, error TEXT
);
CREATE INDEX IF NOT EXISTS refs_hash ON refs(sha256);
CREATE INDEX IF NOT EXISTS assets_state ON assets(state);
"""


def now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def connect(path: Path, *, write: bool = False) -> sqlite3.Connection:
    path = path.expanduser().resolve()
    if not write:
        return readonly(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if tables and not {"assets", "refs", "results", "attempts"}.issubset(tables):
        connection.close()
        raise ValueError(
            "This is not an enrichment database. Refusing to change an existing archive."
        )
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(SCHEMA)
    os.chmod(path, 0o600)
    return connection


@contextlib.contextmanager
def writer_lock(path: Path):
    """A crashed process releases flock; readers never need this lock."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.with_suffix(path.suffix + ".lock").open("a+") as handle:
        os.chmod(handle.name, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "Another queue writer is running. Status/queue remain readable."
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def detect_format(path: Path) -> tuple[str, str]:
    """wacrawl media filenames are hashes without extensions; inspect file bytes."""
    with path.open("rb") as handle:
        head = handle.read(64)
    signatures = (
        (b"%PDF-", "document", ".pdf"),
        (b"\xff\xd8\xff", "image", ".jpg"),
        (b"\x89PNG\r\n\x1a\n", "image", ".png"),
        (b"GIF8", "image", ".gif"),
        (b"II*\x00", "image", ".tiff"),
        (b"MM\x00*", "image", ".tiff"),
        (b"OggS", "audio", ".ogg"),
        (b"fLaC", "audio", ".flac"),
        (b"ID3", "audio", ".mp3"),
        (b"\x1aE\xdf\xa3", "video", ".webm"),
    )
    for signature, kind, suffix in signatures:
        if head.startswith(signature):
            return kind, suffix
    if head.startswith(b"RIFF"):
        for marker, kind, suffix in (
            (b"WEBP", "image", ".webp"),
            (b"WAVE", "audio", ".wav"),
            (b"AVI ", "video", ".avi"),
        ):
            if head[8:12] == marker:
                return kind, suffix
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}:
            return "image", ".heic"
        if brand in {b"M4A ", b"M4B "}:
            return "audio", ".m4a"
        return "video", ".mp4"
    if head.startswith(b"PK\x03\x04"):
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
        for prefix, suffix in (("word/", ".docx"), ("xl/", ".xlsx"), ("ppt/", ".pptx")):
            if any(name.startswith(prefix) for name in names):
                return "document", suffix
        return "document", ".zip"
    kind = digest.classify(path)
    return kind, path.suffix.lower() if kind != "unsupported" else ""


def register(
    connection: sqlite3.Connection,
    source: str,
    ref_id: str,
    path: Path | None,
    metadata: dict,
) -> None:
    stamp = now()
    sha = None
    state = "missing"
    error = None
    try:
        available = path is not None and path.is_file()
    except OSError as exc:
        available = False
        error = f"{type(exc).__name__}: {exc}"
    if available:
        try:
            before = path.stat()
            sha = digest.sha256(path)
            kind, suffix = detect_format(path)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (
                after.st_size,
                after.st_mtime_ns,
            ):
                raise OSError(
                    "Source changed while indexing; rescan after it finishes writing"
                )
            state = "unsupported" if kind == "unsupported" else "available"
            connection.execute(
                "INSERT INTO assets(sha256,kind,suffix,size_bytes,state,updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(sha256) DO NOTHING",
                (
                    sha,
                    kind,
                    suffix,
                    after.st_size,
                    "unsupported" if kind == "unsupported" else "pending",
                    stamp,
                ),
            )
            # Recover availability without resetting completed or failed conversions.
            connection.execute(
                "UPDATE assets SET state='pending',error=NULL,updated_at=? WHERE sha256=? AND state='missing'",
                (stamp, sha),
            )
            if kind != "unsupported":
                connection.execute(
                    "UPDATE assets SET kind=?,suffix=?,state='pending',error=NULL,updated_at=? WHERE sha256=? AND state='unsupported'",
                    (kind, suffix, stamp, sha),
                )
        except (OSError, zipfile.BadZipFile) as exc:
            sha = None
            error = f"{type(exc).__name__}: {exc}"
    if error:
        metadata = {**metadata, "index_error": error}
    connection.execute(
        "INSERT INTO refs VALUES(?,?,?,?,?,?,?) ON CONFLICT(source,ref_id) DO UPDATE SET "
        "path=CASE WHEN excluded.path='' THEN refs.path ELSE excluded.path END,"
        "sha256=coalesce(excluded.sha256,refs.sha256),state=excluded.state,metadata_json=excluded.metadata_json,updated_at=excluded.updated_at",
        (
            source,
            ref_id,
            str(path) if path else "",
            sha,
            state,
            json.dumps(metadata, ensure_ascii=False),
            stamp,
        ),
    )


def sync_wacrawl(connection: sqlite3.Connection, archive_path: Path) -> int:
    archive_path = archive_path.expanduser().resolve()
    with contextlib.closing(readonly(archive_path)) as archive:
        # Native media paths can disappear on logout. Prefer --copy-media in wacrawl.
        archive.execute("BEGIN")
        source = "wacrawl:" + str(archive_path)
        roots = [archive_path.parent / "media"]
        tables = {
            r[0]
            for r in archive.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "sync_state" in tables:
            for (value,) in archive.execute(
                "SELECT value FROM sync_state WHERE key IN ('source_path','merge_source_path')"
            ):
                if value:
                    roots.append(Path(value).expanduser().resolve() / "Message/Media")
        count = 0
        rows = archive.execute(
            "SELECT event_id,msg_id,chat_jid,ts,media_path,media_type,media_title "
            "FROM messages WHERE coalesce(media_type,'') NOT IN ('','link') OR coalesce(media_path,'') != ''"
        )
        for row in rows:
            metadata = dict(row)
            raw_path = metadata.pop("media_path")
            path = Path(raw_path).expanduser() if raw_path else None
            if path is not None:
                if not path.is_absolute():
                    path = archive_path.parent / path
                path = path.resolve()
                if not any(path.is_relative_to(root.resolve()) for root in roots):
                    metadata["index_error"] = (
                        "Media path outside the archive/source media roots was not read"
                    )
                    path = None
            register(connection, source, row["event_id"], path, metadata)
            count += 1
            if count % 250 == 0:
                connection.commit()
        connection.commit()
    return count


def add_files(connection: sqlite3.Connection, root: Path) -> int:
    root = root.expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(root)
    source = "files:" + str(root)
    files = [root] if root.is_file() else sorted(root.rglob("*"))
    count = 0
    for path in files:
        if path.is_symlink() or not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower() in {".sqlite", ".db", ".sqlite-wal", ".sqlite-shm"}:
            continue
        register(
            connection,
            source,
            str(path.relative_to(root)) if path != root else root.name,
            path,
            {"filename": path.name},
        )
        count += 1
        if count % 250 == 0:
            connection.commit()
    connection.commit()
    return count


def sync_iphone(connection: sqlite3.Connection, snapshot: Path) -> int:
    """Keep exporter reply/caption metadata with each attachment reference."""
    snapshot = snapshot.expanduser().resolve()
    with (snapshot / "result.json").open(encoding="utf-8") as handle:
        chats = json.load(handle)
    if not isinstance(chats, dict):
        raise TypeError(
            "Expected the conversation dictionary from WhatsApp-Chat-Exporter"
        )
    source = "iphone:" + str(snapshot)
    count = 0
    for chat_id, chat in chats.items():
        if not isinstance(chat, dict) or not isinstance(chat.get("messages", {}), dict):
            raise TypeError("Unsupported exporter conversation schema")
        media_base = chat.get("media_base", "")
        if not isinstance(media_base, str):
            raise TypeError("Invalid exporter media_base")
        for message_key, message in chat.get("messages", {}).items():
            if not isinstance(message, dict):
                raise TypeError("Unsupported exporter message schema")
            if not message.get("media"):
                continue
            metadata = {"chat_id": chat_id, "message_key": message_key}
            metadata.update(
                {
                    key: message.get(key)
                    for key in (
                        "key_id",
                        "timestamp",
                        "sender",
                        "from_me",
                        "caption",
                        "mime",
                        "reply",
                        "quoted_data",
                    )
                }
            )
            path = None
            relative = message.get("data")
            if (
                isinstance(relative, str)
                and relative
                and message.get("mime") != "media"
            ):
                candidates = [
                    (snapshot / media_base / relative).resolve(),
                    (snapshot / relative).resolve(),
                ]
                candidates = [
                    candidate
                    for candidate in candidates
                    if candidate.is_relative_to(snapshot)
                ]
                path = next(
                    (candidate for candidate in candidates if candidate.is_file()),
                    candidates[0] if candidates else None,
                )
            register(
                connection,
                source,
                str(chat_id) + "/" + str(message_key),
                path,
                metadata,
            )
            count += 1
            if count % 250 == 0:
                connection.commit()
    connection.commit()
    return count


def summary(connection: sqlite3.Connection) -> dict:
    return {
        "assets": {
            r[0]: r[1]
            for r in connection.execute(
                "SELECT state,count(*) FROM assets GROUP BY state"
            )
        },
        "references": {
            r[0]: r[1]
            for r in connection.execute(
                "SELECT state,count(*) FROM refs GROUP BY state"
            )
        },
        "results": connection.execute("SELECT count(*) FROM results").fetchone()[0],
        "running": [
            dict(r)
            for r in connection.execute(
                "SELECT sha256,kind,attempts,updated_at FROM assets WHERE state='running'"
            )
        ],
    }


def select_asset(connection: sqlite3.Connection, prefix: str) -> sqlite3.Row:
    if not prefix or any(c not in "0123456789abcdef" for c in prefix.lower()):
        raise ValueError("Use a SHA-256 hash or unambiguous hash prefix from queue")
    matches = connection.execute(
        "SELECT * FROM assets WHERE sha256 LIKE ?", (prefix.lower() + "%",)
    ).fetchall()
    if len(matches) != 1:
        raise ValueError(
            f"Hash prefix matches {len(matches)} assets; use a longer prefix"
        )
    return matches[0]


def usable_source(connection: sqlite3.Connection, sha: str) -> Path | None:
    for row in connection.execute(
        "SELECT path FROM refs WHERE sha256=? ORDER BY source,ref_id", (sha,)
    ):
        path = Path(row[0])
        try:
            if path.is_file() and digest.sha256(path) == sha:
                return path
        except OSError:
            continue
    return None


@contextlib.contextmanager
def conversion_runtime(args, needs_ocr: bool, auto_server: bool):
    import launch

    if needs_ocr and auto_server:
        with launch.local_server() as endpoint:
            args.ocr_vlm_url = endpoint
            yield
    else:
        yield


def run_queue(connection: sqlite3.Connection, options: list[str], limit: int) -> int:
    args = digest.parse_args(["run", *options])
    # Recover only after the exclusive writer lock has proved no worker is active.
    connection.execute(
        "UPDATE assets SET state='pending',error='Previous worker interrupted',updated_at=? WHERE state='running'",
        (now(),),
    )
    connection.execute(
        "UPDATE attempts SET state='interrupted',finished_at=?,error='Previous worker interrupted' WHERE state='running'",
        (now(),),
    )
    connection.commit()
    if args.force:
        connection.execute(
            "UPDATE assets SET state='pending',updated_at=? WHERE state='done'",
            (now(),),
        )
        connection.commit()
    # Resolve backend/version configuration without starting an inference server.
    import launch

    auto_server = launch.can_auto_server(args)
    if auto_server:
        args.ocr_vlm_url = launch.OCR_URL
    config = digest.config_for_args(args)
    config_json = json.dumps(config, sort_keys=True, ensure_ascii=False)
    key = hashlib.sha256(config_json.encode()).hexdigest()
    # Each configuration has its own retained result. Failed work requires retry.
    connection.execute(
        "UPDATE assets SET state='pending',error=NULL,updated_at=? WHERE state='done' "
        "AND NOT EXISTS(SELECT 1 FROM results r WHERE r.sha256=assets.sha256 AND r.config_key=?)",
        (now(), key),
    )
    connection.commit()
    pending = connection.execute(
        "SELECT * FROM assets WHERE state='pending' ORDER BY updated_at,sha256 LIMIT ?",
        (limit or -1,),
    ).fetchall()
    if not pending:
        print("No pending assets. Use enrich status or enrich retry for failures.")
        return 0
    needs_ocr = any(
        row["kind"] in {"image", "video"} or row["suffix"] == ".pdf" for row in pending
    )
    failed = 0
    with conversion_runtime(args, needs_ocr, auto_server):
        converter = digest.Converter(args, digest.resolve_ocr_languages(args.ocr_langs))
        try:
            for index, asset in enumerate(pending, 1):
                sha = asset["sha256"]
                existing = connection.execute(
                    "SELECT 1 FROM results WHERE sha256=? AND config_key=?", (sha, key)
                ).fetchone()
                if existing and not args.force:
                    connection.execute(
                        "UPDATE assets SET state='done',config_key=?,error=NULL,updated_at=? WHERE sha256=?",
                        (key, now(), sha),
                    )
                    connection.commit()
                    continue
                source = usable_source(connection, sha)
                if source is None:
                    connection.execute(
                        "UPDATE assets SET state='missing',error='No source file with the indexed hash is available',updated_at=? WHERE sha256=?",
                        (now(), sha),
                    )
                    connection.commit()
                    failed += 1
                    continue
                stamp = now()
                connection.execute(
                    "UPDATE assets SET state='running',attempts=attempts+1,error=NULL,updated_at=? WHERE sha256=?",
                    (stamp, sha),
                )
                attempt_id = connection.execute(
                    "INSERT INTO attempts(sha256,config_key,started_at,state) VALUES(?,?,?,'running')",
                    (sha, key, stamp),
                ).lastrowid
                connection.commit()
                print(
                    f"[{index}/{len(pending)}] {sha[:12]} {asset['kind']} running",
                    flush=True,
                )
                try:
                    # Some converters depend on extensions; temporary files never become library copies.
                    with tempfile.TemporaryDirectory(prefix="wa-enrich-") as temporary:
                        staged = Path(temporary) / (sha + asset["suffix"])
                        shutil.copyfile(source, staged)
                        if digest.sha256(staged) != sha:
                            raise OSError("Source changed while staging; rescan it")
                        markdown, metadata = converter.convert(staged, asset["kind"])
                    metadata.update({"sha256": sha, "source_kind": asset["kind"]})
                    stamp = now()
                    connection.execute(
                        "INSERT OR REPLACE INTO results VALUES(?,?,?,?,?,?)",
                        (
                            sha,
                            key,
                            markdown,
                            json.dumps(metadata, ensure_ascii=False),
                            config_json,
                            stamp,
                        ),
                    )
                    connection.execute(
                        "UPDATE assets SET state='done',config_key=?,error=NULL,updated_at=? WHERE sha256=?",
                        (key, stamp, sha),
                    )
                    connection.execute(
                        "UPDATE attempts SET state='done',finished_at=? WHERE id=?",
                        (stamp, attempt_id),
                    )
                    connection.commit()
                    print(f"  {sha[:12]} done", flush=True)
                except KeyboardInterrupt:
                    connection.execute(
                        "UPDATE assets SET state='pending',error='Interrupted; resume with enrich run',updated_at=? WHERE sha256=?",
                        (now(), sha),
                    )
                    connection.execute(
                        "UPDATE attempts SET state='interrupted',finished_at=? WHERE id=?",
                        (now(), attempt_id),
                    )
                    connection.commit()
                    raise
                except Exception as exc:  # noqa: BLE001 - Record third-party failures per asset and continue.
                    error = f"{type(exc).__name__}: {exc}"
                    connection.execute(
                        "UPDATE assets SET state='failed',error=?,updated_at=? WHERE sha256=?",
                        (error, now(), sha),
                    )
                    connection.execute(
                        "UPDATE attempts SET state='failed',finished_at=?,error=? WHERE id=?",
                        (now(), error, attempt_id),
                    )
                    connection.commit()
                    print(f"  {sha[:12]} failed: {error}", file=sys.stderr, flush=True)
                    failed += 1
        finally:
            converter.close()
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)
    sync = sub.add_parser(
        "sync", help="Index wacrawl media without changing the archive"
    )
    sync.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    add = sub.add_parser(
        "add", help="Index an exported media file/folder without copying it"
    )
    add.add_argument("path", type=Path)
    iphone = sub.add_parser(
        "iphone", help="Index exporter snapshot media with message/reply provenance"
    )
    iphone.add_argument("snapshot", type=Path)
    sub.add_parser("status").add_argument("--json", action="store_true")
    listing = sub.add_parser("queue")
    listing.add_argument("--state", choices=STATES)
    listing.add_argument("--limit", type=int, default=30)
    listing.add_argument("--json", action="store_true")
    run = sub.add_parser(
        "run", help="Process pending assets; append existing converter options"
    )
    run.add_argument(
        "--limit", type=int, default=0, help="0 processes all pending assets"
    )
    retry = sub.add_parser(
        "retry", help="Requeue failed assets; missing files need rescan"
    )
    retry.add_argument("sha256", nargs="?")
    show = sub.add_parser("show", help="Print the most recent extracted text")
    show.add_argument("sha256")
    show.add_argument("--json", action="store_true")
    args, options = parser.parse_known_args(argv)
    if options and args.command != "run":
        parser.error("unrecognized arguments: " + " ".join(options))
    if getattr(args, "limit", 0) < 0:
        parser.error("--limit must be nonnegative")
    args.db = args.db.expanduser().resolve()
    writing = args.command in {"sync", "add", "iphone", "run", "retry"}
    if args.command == "sync" and args.db == args.archive.expanduser().resolve():
        parser.error("Enrichment database must differ from the source archive")
    with writer_lock(args.db) if writing else contextlib.nullcontext():
        if not writing and not args.db.exists():
            print("No enrichment database yet. Run enrich sync or enrich add PATH.")
            return 0
        with contextlib.closing(connect(args.db, write=writing)) as connection:
            if args.command == "sync":
                print(
                    f"Indexed {sync_wacrawl(connection, args.archive)} media references."
                )
            elif args.command == "add":
                if args.path.expanduser().resolve() in {args.db, args.db.parent}:
                    parser.error(
                        "Index source media, not the enrichment database directory"
                    )
                print(f"Indexed {add_files(connection, args.path)} file references.")
            elif args.command == "iphone":
                print(
                    f"Indexed {sync_iphone(connection, args.snapshot)} iPhone media references."
                )
            elif args.command == "run":
                return run_queue(connection, options, args.limit)
            elif args.command == "retry":
                if args.sha256:
                    sha = select_asset(connection, args.sha256)["sha256"]
                    cursor = connection.execute(
                        "UPDATE assets SET state='pending',error=NULL,updated_at=? WHERE sha256=? AND state='failed'",
                        (now(), sha),
                    )
                else:
                    cursor = connection.execute(
                        "UPDATE assets SET state='pending',error=NULL,updated_at=? WHERE state='failed'",
                        (now(),),
                    )
                connection.commit()
                print(f"Requeued {cursor.rowcount} failed assets.")
            elif args.command == "queue":
                if args.state == "missing":
                    rows = [
                        dict(row)
                        for row in connection.execute(
                            "SELECT source,ref_id,sha256,path,state,metadata_json FROM refs WHERE state='missing' ORDER BY source,ref_id LIMIT ?",
                            (args.limit or -1,),
                        )
                    ]
                    if args.json:
                        print(json.dumps(rows, ensure_ascii=False, indent=2))
                    else:
                        for row in rows:
                            metadata = json.loads(row["metadata_json"])
                            print(
                                f"{row['ref_id']} missing {metadata.get('media_type', '')} {metadata.get('index_error', '')}"
                            )
                        if not rows:
                            print("No missing media references.")
                    return 0
                rows = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT sha256,kind,state,attempts,error,updated_at FROM assets WHERE (? IS NULL OR state=?) ORDER BY updated_at,sha256 LIMIT ?",
                        (args.state, args.state, args.limit or -1),
                    )
                ]
                if args.json:
                    print(json.dumps(rows, ensure_ascii=False, indent=2))
                else:
                    for row in rows:
                        print(
                            f"{row['sha256'][:12]}  {row['state']:11} {row['kind']:9} attempts={row['attempts']} {row['error'] or ''}"
                        )
                    if not rows:
                        print(
                            "No matching assets. Missing media references are counted in status."
                        )
                return 0
            elif args.command == "show":
                asset = select_asset(connection, args.sha256)
                row = connection.execute(
                    "SELECT * FROM results WHERE sha256=? ORDER BY processed_at DESC LIMIT 1",
                    (asset["sha256"],),
                ).fetchone()
                if row is None:
                    raise ValueError(
                        f"No extracted result; current state: {asset['state']}"
                    )
                print(
                    json.dumps(dict(row), ensure_ascii=False, indent=2)
                    if args.json
                    else row["markdown"]
                )
                return 0
            stats = summary(connection)
            print(
                json.dumps(
                    {"database": str(args.db), **stats}, ensure_ascii=False, indent=2
                )
            )
    return 0
