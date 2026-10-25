"""Create completed, single-file SQLite snapshots for file synchronization."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import sqlite3
import uuid
from pathlib import Path

import digest
from enrichment import LIBRARY, readonly, writer_lock


def database_snapshot(source: Path, destination: Path) -> dict:
    with (
        contextlib.closing(readonly(source)) as reader,
        contextlib.closing(sqlite3.connect(destination)) as writer,
    ):
        reader.backup(writer, pages=256, sleep=0.01)
        writer.execute("PRAGMA journal_mode=DELETE")
        check = writer.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError(
                f"Snapshot integrity check failed for {source.name}: {check}"
            )
    os.chmod(destination, 0o600)
    return {
        "file": destination.name,
        "sha256": digest.sha256(destination),
        "bytes": destination.stat().st_size,
        "quick_check": "ok",
    }


def snapshot(library: Path) -> Path:
    library = library.expanduser().resolve()
    sources = [
        library / name
        for name in ("wacrawl.db", "enrichment.sqlite")
        if (library / name).is_file()
    ]
    if not sources:
        raise ValueError("No archive databases exist at this library location")
    stamp = (
        dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    )
    staging = library / ".snapshot-staging" / stamp
    target = library / "snapshots/sqlite" / stamp
    with writer_lock(library / "enrichment.sqlite"):
        staging.mkdir(parents=True, mode=0o700)
        records = [
            database_snapshot(source, staging / source.name) for source in sources
        ]
        manifest = {
            "format": 1,
            "created_at": dt.datetime.now(dt.UTC).isoformat(),
            "source_library": str(library),
            "databases": records,
            "scope": "Consistent per-database snapshots; media and iPhone exports sync separately.",
        }
        digest.atomic_write(
            staging / "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging.rename(target)
    return target


def verify(folder: Path) -> dict:
    folder = folder.expanduser().resolve()
    with (folder / "manifest.json").open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    if manifest.get("format") != 1:
        raise ValueError("Unsupported snapshot format")
    for record in manifest["databases"]:
        name = record["file"]
        if name not in {"wacrawl.db", "enrichment.sqlite"}:
            raise ValueError("Unexpected snapshot database filename")
        path = folder / name
        if path.is_symlink() or digest.sha256(path) != record["sha256"]:
            raise ValueError(f"Snapshot checksum mismatch: {name}")
        with contextlib.closing(readonly(path)) as connection:
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError(f"Snapshot integrity check failed: {name}")
    return manifest


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=LIBRARY)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "snapshot", help="Publish a verified SQLite generation before Unison"
    )
    check = sub.add_parser(
        "verify",
        help="Verify a published generation, including one on external storage",
    )
    check.add_argument("folder", type=Path)
    args = parser.parse_args(argv)
    if args.command == "snapshot":
        folder = snapshot(args.root)
        print(
            json.dumps(
                {"snapshot": str(folder), "verified": verify(folder)},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(json.dumps(verify(args.folder), ensure_ascii=False, indent=2))
    return 0
