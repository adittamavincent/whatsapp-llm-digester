"""Project workspaces and incremental WhatsApp ZIP checkpoints."""

from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as dt
import fcntl
import hashlib
import json
import re
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

import digest
import whatsapp
from project_docs import (
    initialize_project_docs,
    render_project,
    update_readme,
    write_if_changed,
)

ROOT = digest.PROJECT_DIR.parents[1] / "whatsapp-conversation"
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


def project_path(name: str) -> Path:
    if not SLUG.fullmatch(name):
        raise ValueError(
            "Project names must use lowercase letters, numbers, and hyphens"
        )
    path = ROOT / name
    if path.is_symlink():
        raise ValueError("Project directory must not be a symlink")
    return path


def migrate_input(path: Path) -> None:
    """Rename the legacy ZIP inbox, resuming safely if a prior migration was interrupted."""
    legacy, inbox = path / "zips", path / "input"
    journal = path / ".digester" / "input-migration.json"
    if legacy.is_symlink() or inbox.is_symlink():
        raise ValueError("Input directory must not be a symlink")
    if not legacy.exists() and not journal.exists():
        inbox.mkdir(exist_ok=True)
        return
    if journal.exists():
        plan = json.loads(journal.read_text())
        destination = path / plan["destination"]
        if destination != inbox and destination.parent != inbox:
            raise ValueError("Invalid input migration destination")
    else:
        if not legacy.is_dir():
            raise ValueError("Legacy zips path must be a directory")
        destination = inbox
        if inbox.exists():
            destination = inbox / "imported-from-zips"
            suffix = 2
            while destination.exists():
                destination = inbox / f"imported-from-zips-{suffix}"
                suffix += 1
        plan = {"destination": destination.relative_to(path).as_posix()}
        digest.atomic_write(journal, json.dumps(plan) + "\n")
    if legacy.exists():
        if destination.exists():
            raise RuntimeError(
                "Input migration destination already exists; originals are intact"
            )
        legacy.rename(destination)
    elif not destination.is_dir():
        raise RuntimeError("Input migration cannot locate its moved ZIP directory")
    state_path = path / ".digester" / "state.json"
    if state_path.exists():
        state = load_state(path)
        aliases = {}
        for checkpoint in state["checkpoints"].values():
            for alias in checkpoint["archives"]:
                if alias.startswith("zips/"):
                    aliases[alias] = plan["destination"] + "/" + alias[len("zips/") :]
            checkpoint["archives"] = [
                aliases.get(alias, alias) for alias in checkpoint["archives"]
            ]
        # Update the display first; repeating this step after interruption is harmless.
        report = path / "target" / "checkpoints.md"
        if report.exists():
            text = report.read_text(encoding="utf-8")
            for old, new in aliases.items():
                text = text.replace(old, new)
            write_if_changed(report, text)
        save_state(path, state)
    journal.unlink()
    print(f"Migrated legacy ZIP inbox to {destination}", flush=True)


def initialize(name: str, context: str | None = None) -> Path:
    path = project_path(name)
    for directory in ("target", "target/todo", "target/markdown", ".digester"):
        destination = path / directory
        if destination.is_symlink():
            raise ValueError(
                f"Workspace directory must not be a symlink: {destination}"
            )
        destination.mkdir(parents=True, exist_ok=True)
    with project_lock(path):
        migrate_input(path)
    initialize_project_docs(path, context)
    ignore = path / ".gitignore"
    entries = ignore.read_text() if ignore.exists() else ""
    additions = [
        entry
        for entry in ("input/", "target/", ".digester/", ".DS_Store")
        if entry not in entries.splitlines()
    ]
    if additions:
        digest.atomic_write(
            ignore,
            entries.rstrip()
            + ("\n" if entries.strip() else "")
            + "\n".join(additions)
            + "\n",
        )
    update_readme(path, load_state(path))
    return path


def load_state(path: Path) -> dict:
    location = path / ".digester" / "state.json"
    if not location.exists():
        return {
            "version": 1,
            "date_order": None,
            "checkpoints": {},
            "messages": {},
            "media": {},
        }
    state = json.loads(location.read_text(encoding="utf-8"))
    if state.get("version") != 1:
        raise RuntimeError(
            "Unknown project state version; keep the state and ZIPs intact"
        )
    return state


def save_state(path: Path, state: dict) -> None:
    digest.atomic_write(
        path / ".digester" / "state.json",
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
    )


@contextlib.contextmanager
def project_lock(path: Path):
    with (path / ".digester" / "lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"Another conversion is using {path.name}") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def safe_member(info: zipfile.ZipInfo) -> PurePosixPath:
    name = PurePosixPath(info.filename)
    if (
        name.is_absolute()
        or ".." in name.parts
        or "\\" in info.filename
        or re.match(r"^[A-Za-z]:", info.filename)
    ):
        raise ValueError(f"Unsafe ZIP member: {info.filename}")
    if stat.S_ISLNK(info.external_attr >> 16):
        raise ValueError(f"ZIP symlinks are not supported: {info.filename}")
    return name


def decode_chat(raw: bytes) -> str:
    return raw.decode(
        "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    )


def read_chat_member(
    zipped: zipfile.ZipFile, info: zipfile.ZipInfo, order: str
) -> list[dict] | None:
    name = PurePosixPath(info.filename)
    if name.suffix.lower() != ".txt":
        return None
    named_chat = name.name.lower() == "_chat.txt" or name.stem.lower().startswith(
        "whatsapp chat"
    )
    try:
        return whatsapp.parse_chat(decode_chat(zipped.read(info)), order)
    except (whatsapp.NoChatMessages, UnicodeError) as exc:
        if named_chat:
            raise ValueError(f"Cannot read chat {name}: {exc}") from exc
        return None
    except ValueError as exc:
        raise ValueError(f"Cannot read chat {name}: {exc}") from exc


def merge_chat_messages(
    state: dict, chats: list, members: dict, checkpoint: str
) -> tuple[list[str], int]:
    lookup = {}
    for name, media_id in members.items():
        lookup.setdefault(PurePosixPath(name).name, set()).add(media_id)
    dates = []
    added = 0
    for name, messages in chats:
        conversation = PurePosixPath(name).stem
        for message_id, message in whatsapp.message_ids(messages, conversation):
            if message["timestamp"]:
                dates.append(message["timestamp"])
            if message_id not in state["messages"]:
                state["messages"][message_id] = {
                    **message,
                    "conversation": conversation,
                    "checkpoints": [],
                    "attachments": [],
                }
                added += 1
            record = state["messages"][message_id]
            if checkpoint not in record["checkpoints"]:
                record["checkpoints"].append(checkpoint)
            for attachment in whatsapp.attachment_names(message["body"]):
                for media_id in sorted(
                    lookup.get(PurePosixPath(attachment).name, set())
                ):
                    if media_id not in record["attachments"]:
                        record["attachments"].append(media_id)
    return dates, added


def import_checkpoint(
    path: Path, archive: Path, checksum: str, state: dict, order: str
) -> tuple[int, int]:
    # Commit one checkpoint only after all its members have been read and checked.
    updated = copy.deepcopy(state)
    chat_files, members, staged = [], {}, {}
    before_messages, before_media = len(state["messages"]), len(state["media"])
    with (
        tempfile.TemporaryDirectory(
            prefix="import-", dir=path / ".digester"
        ) as temporary,
        zipfile.ZipFile(archive) as zipped,
    ):
        stage = Path(temporary)
        for info in zipped.infolist():
            name = safe_member(info)
            if (
                info.is_dir()
                or name.name == ".DS_Store"
                or "__MACOSX" in name.parts
                or name.name.startswith("._")
            ):
                continue
            messages = read_chat_member(zipped, info, order)
            if messages is not None:
                chat_files.append((name.as_posix(), messages))
                continue
            with zipped.open(info) as source, (stage / "member").open("wb") as output:
                content_hash = hashlib.sha256()
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    content_hash.update(chunk)
                    output.write(chunk)
            media_id = content_hash.hexdigest()
            members[name.as_posix()] = media_id
            if media_id not in updated["media"]:
                filename = re.sub(r"[^\w.() -]", "_", name.name)[:180] or "file"
                # Preserve the extension for the existing converter's dispatch.
                if not filename.endswith(name.suffix):
                    filename = filename[:150] + name.suffix
                source_path = f"media/{media_id}/{filename}"
                updated["media"][media_id] = {
                    "source": source_path,
                    "aliases": [],
                    "checkpoints": [],
                }
                staged_file = stage / media_id
                (stage / "member").replace(staged_file)
                staged[media_id] = staged_file
            record = updated["media"][media_id]
            if (
                media_id not in staged
                and not (path / "target" / "todo" / record["source"]).is_file()
            ):
                staged_file = stage / media_id
                (stage / "member").replace(staged_file)
                staged[media_id] = staged_file
            if name.as_posix() not in record["aliases"]:
                record["aliases"].append(name.as_posix())
            if checksum not in record["checkpoints"]:
                record["checkpoints"].append(checksum)
        if not chat_files:
            raise ValueError("ZIP contains no recognized WhatsApp chat transcript")
        dates, added_messages = merge_chat_messages(
            updated, chat_files, members, checksum
        )
        updated["date_order"] = order
        updated["checkpoints"][checksum] = updated["checkpoints"].get(checksum) or {
            "archives": [archive.relative_to(path).as_posix()],
            "chats": [name for name, _ in chat_files],
            "from": min(dates) if dates else None,
            "through": max(dates) if dates else None,
            "imported_utc": dt.datetime.now(dt.UTC).isoformat(),
            "messages_added": added_messages,
            "media_added": len(updated["media"]) - before_media,
            "members": members,
        }
        for media_id, source in staged.items():
            destination = (
                path / "target" / "todo" / updated["media"][media_id]["source"]
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.replace(destination)
        save_state(path, updated)
        state.clear()
        state.update(updated)
    return len(state["messages"]) - before_messages, len(state["media"]) - before_media


def ingest(path: Path, state: dict, order: str) -> list[str]:
    if state["date_order"] and state["date_order"] != order:
        raise ValueError(
            f"This project was imported with --date-order {state['date_order']}; use the same order"
        )
    errors = []
    archives = sorted(
        p
        for p in (path / "input").rglob("*")
        if p.is_file() and p.suffix.lower() == ".zip" and not p.is_symlink()
    )
    for archive in archives:
        checksum = digest.sha256(archive)
        previous = state["checkpoints"].get(checksum)
        missing_media = previous and any(
            not (
                path / "target" / "todo" / state["media"][media_id]["source"]
            ).is_file()
            for media_id in previous["members"].values()
        )
        if previous and not missing_media:
            alias = archive.relative_to(path).as_posix()
            if alias not in previous["archives"]:
                previous["archives"].append(alias)
                save_state(path, state)
            print(f"Checkpoint unchanged: {archive.name}", flush=True)
            continue
        try:
            messages, media = import_checkpoint(path, archive, checksum, state, order)
            print(
                f"Imported {archive.name}: {messages} new messages, {media} new media",
                flush=True,
            )
        except (ValueError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
            error = f"{archive.name}: {exc}"
            errors.append(error)
            print(f"ZIP ERROR: {error}", flush=True)
    render_project(path, state, errors)
    return errors


def show_projects() -> int:
    print(f"Projects: {ROOT}")
    for path in sorted(ROOT.iterdir()) if ROOT.exists() else []:
        if path.is_dir() and not path.is_symlink() and SLUG.fullmatch(path.name):
            zips = sum(
                1
                for p in (path / "input").rglob("*")
                if p.is_file() and p.suffix.lower() == ".zip"
            )
            print(f"  {path.name}: {zips} ZIP(s)")
    print(
        "\nCreate: ./run.sh new PROJECT\nDigest: ./run.sh convert PROJECT\nLegacy target: ./run.sh run"
    )
    return 0


def main(arguments: list[str]) -> int:
    if not arguments or arguments == ["projects"]:
        return show_projects()
    parser = argparse.ArgumentParser(
        description="Create and digest WhatsApp project checkpoints. Conversion options are forwarded."
    )
    parser.add_argument("command", choices=("new", "convert"))
    parser.add_argument("project")
    parser.add_argument(
        "--context",
        help="Initial project background, used only if CONTEXT.md does not exist",
    )
    parser.add_argument(
        "--date-order",
        choices=("day-first", "month-first"),
        help="First import's date order; then remembered per project (default day-first)",
    )
    args, conversion_options = parser.parse_known_args(arguments)
    if args.command == "new" and conversion_options:
        parser.error(f"Unknown options: {' '.join(conversion_options)}")
    path = project_path(args.project)
    if args.command == "convert" and not path.is_dir():
        raise ValueError(
            f"Project does not exist. Run ./run.sh new {args.project} first"
        )
    path = initialize(args.project, args.context)
    if args.command == "new":
        print(
            f"Project ready: {path}\nDrop export ZIPs into: {path / 'input'}\nThen: ./run.sh convert {args.project}"
        )
        return 0
    # Validate converter flags before importing any input.
    digest.parse_args(["run", *conversion_options])
    with project_lock(path):
        state = load_state(path)
        errors = ingest(
            path, state, args.date_order or state["date_order"] or "day-first"
        )
        if not state["checkpoints"]:
            print(
                f"No checkpoints imported. Put WhatsApp export ZIPs in {path / 'input'}"
            )
            return 1 if errors else 0
        import launch

        with digest.target_directory(path / "target"):
            result = launch.run_legacy(["run", *conversion_options])
        return 1 if errors or result else 0
