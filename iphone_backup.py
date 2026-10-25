"""Inspect Finder backups and export WhatsApp to a new preserved snapshot."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import plistlib
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import digest
from enrichment import LIBRARY, readonly

BACKUP_ROOT = Path.home() / "Library/Application Support/MobileSync/Backup"
EXPORTER = "whatsapp-chat-exporter==0.13.1"
DECRYPTOR = "git+https://github.com/KnugiHK/iphone_backup_decrypt.git@190d61c849045aded1863aa4bd1466e4d265f53d"


def inspect_backup(path: Path) -> dict:
    path = path.expanduser().resolve()
    result = {"path": str(path), "usable": False, "coverage": "unverified"}
    required = ("Manifest.plist", "Manifest.db", "Info.plist", "Status.plist")
    missing = [name for name in required if not (path / name).is_file()]
    result["missing_files"] = missing
    if missing:
        return result
    manifest = plistlib.loads((path / "Manifest.plist").read_bytes())
    info = plistlib.loads((path / "Info.plist").read_bytes())
    status = plistlib.loads((path / "Status.plist").read_bytes())
    result.update(
        {
            "encrypted": bool(manifest.get("IsEncrypted")),
            "backup_date": str(
                info.get("Last Backup Date", status.get("Date", "unknown"))
            ),
            "ios_version": info.get("Product Version"),
            "snapshot_state": status.get("SnapshotState"),
        }
    )
    if status.get("SnapshotState") != "finished":
        result["note"] = "Backup is not marked finished. Wait for Finder to finish."
        return result
    if result["encrypted"]:
        result["usable"] = True
        result["note"] = (
            "Candidate only: password needed to inspect encrypted Manifest.db; WhatsApp coverage not yet verified."
        )
        return result
    with contextlib.closing(readonly(path / "Manifest.db")) as connection:
        rows = connection.execute(
            "SELECT fileID,domain,relativePath FROM Files WHERE lower(domain) LIKE '%whatsapp%' AND flags=1"
        ).fetchall()
    databases = [
        dict(row)
        for row in rows
        if Path(row["relativePath"]).name in {"ChatStorage.sqlite", "ContactsV2.sqlite"}
    ]
    absent = 0
    for row in rows:
        file_id = row["fileID"]
        if (
            not isinstance(file_id, str)
            or len(file_id) != 40
            or any(c not in "0123456789abcdefABCDEF" for c in file_id)
        ):
            absent += 1
            continue
        if (
            not (path / file_id[:2] / file_id).is_file()
            and not (path / file_id).is_file()
        ):
            absent += 1
    result.update(
        {
            "whatsapp_files": len(rows),
            "absent_payload_files": absent,
            "databases": databases,
        }
    )
    result["usable"] = (
        any(Path(row["relativePath"]).name == "ChatStorage.sqlite" for row in databases)
        and absent == 0
    )
    result["note"] = (
        "Manifest checked; message/media completeness still requires comparison with iPhone."
    )
    return result


def inspect_all(root: Path) -> list[dict]:
    if not root.exists():
        return []
    return [inspect_backup(path) for path in sorted(root.iterdir()) if path.is_dir()]


def export_backup(
    path: Path, output_root: Path, *, business: bool = False
) -> tuple[int, Path]:
    path = path.expanduser().resolve()
    report = inspect_backup(path)
    if not report["usable"]:
        raise ValueError(
            "No usable finished backup at this path. Run iphone inspect first; no export started."
        )
    if report["encrypted"] and not sys.stdin.isatty():
        raise ValueError(
            "Encrypted backup: run this command in Terminal. Enter the Finder backup password at the local hidden prompt; never put it in chat or arguments."
        )
    output_root = output_root.expanduser().resolve()
    if (
        output_root == path
        or output_root.is_relative_to(path)
        or path.is_relative_to(output_root)
    ):
        raise ValueError("Snapshot output and source backup must not overlap")
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required; use ./run.sh iphone export")
    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder = output_root / (
        dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    )
    folder.mkdir(mode=0o700)
    command = [uv, "tool", "run", "--python", "3.12", "--from", EXPORTER]
    if report["encrypted"]:
        command.extend(["--with", DECRYPTOR])
    command.extend(
        [
            "wtsexporter",
            "-i",
            "-b",
            str(path),
            "-j",
            "result.json",
            "--no-html",
            "-o",
            ".",
            "--avoid-encoding-json",
        ]
    )
    if business:
        command.append("--business")
    record = {
        "source": report,
        "exporter": EXPORTER,
        "decryptor": DECRYPTOR if report["encrypted"] else None,
        "state": "running",
        "started_at": dt.datetime.now(dt.UTC).isoformat(),
        "coverage": "unverified",
        "command": command,
        "source_manifest_sha256": digest.sha256(path / "Manifest.db"),
    }
    manifest = folder / "snapshot.json"
    digest.atomic_write(
        manifest, json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    )
    print(f"Export snapshot: {folder}", flush=True)
    print(
        "First use downloads the isolated exporter. Source backup is not modified.",
        flush=True,
    )
    try:
        code = subprocess.call(command, cwd=folder)
        result = folder / "result.json"
        if code == 0 and result.is_file():
            with result.open(encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict) or not data:
                raise ValueError("Exporter produced no conversation dictionary")
            messages = []
            for chat in data.values():
                if not isinstance(chat, dict) or not isinstance(
                    chat.get("messages", {}), dict
                ):
                    raise TypeError(
                        "Exporter produced an unsupported conversation schema"
                    )
                messages.extend(chat.get("messages", {}).values())
            if not messages or not all(
                isinstance(message, dict) for message in messages
            ):
                raise ValueError("Exporter produced no readable message records")
            if digest.sha256(path / "Manifest.db") != record["source_manifest_sha256"]:
                raise RuntimeError(
                    "Source backup changed during export; keep this snapshot for diagnosis and export again after Finder finishes"
                )
            record.update(
                {
                    "state": "exported",
                    "result_sha256": digest.sha256(result),
                    "conversation_records": len(data),
                    "message_records": len(messages),
                    "media_references": sum(
                        bool(message.get("media")) for message in messages
                    ),
                    "missing_media_reported_by_exporter": sum(
                        bool(message.get("media")) and message.get("mime") == "media"
                        for message in messages
                    ),
                    "reply_records": sum(
                        bool(message.get("reply")) for message in messages
                    ),
                }
            )
        else:
            record.update({"state": "failed", "exit_code": code})
            code = code or 1
    except BaseException as exc:
        record.update(
            {
                "state": "interrupted"
                if isinstance(exc, KeyboardInterrupt)
                else "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        raise
    finally:
        record["finished_at"] = dt.datetime.now(dt.UTC).isoformat()
        digest.atomic_write(
            manifest, json.dumps(record, ensure_ascii=False, indent=2) + "\n"
        )
    return code, folder


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser(
        "inspect", help="Read only: list backup candidates and missing manifests"
    )
    inspect.add_argument("--backup", type=Path)
    inspect.add_argument("--root", type=Path, default=BACKUP_ROOT)
    export = sub.add_parser(
        "export", help="Export to a NEW snapshot; password is entered only in Terminal"
    )
    export.add_argument("--backup", type=Path, required=True)
    export.add_argument(
        "--output-root", type=Path, default=LIBRARY / "snapshots/iphone"
    )
    export.add_argument("--business", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "inspect":
        records = (
            [inspect_backup(args.backup)] if args.backup else inspect_all(args.root)
        )
        print(
            json.dumps(
                {
                    "backups": records,
                    "usable_candidates": sum(bool(r["usable"]) for r in records),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    code, folder = export_backup(args.backup, args.output_root, business=args.business)
    print(
        f"Export {'finished' if code == 0 else 'failed'}. Report: {folder / 'snapshot.json'}"
    )
    return code
