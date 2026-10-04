"""Project workspaces and incremental WhatsApp ZIP checkpoints."""

from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as dt
import fcntl
import hashlib
import io
import json
import re
import stat
import tarfile
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


ARCHIVE_EXTENSIONS = (
    ".zip",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz2",
    ".tar.xz",
    ".txz",
)


def is_archive_file(path: Path) -> bool:
    lower = path.name.lower()
    return any(lower.endswith(ext) for ext in ARCHIVE_EXTENSIONS)


def check_safe_path(filename: str, is_symlink: bool = False) -> PurePosixPath:
    name = PurePosixPath(filename)
    if (
        name.is_absolute()
        or ".." in name.parts
        or "\\" in filename
        or re.match(r"^[A-Za-z]:", filename)
    ):
        raise ValueError(f"Unsafe member path: {filename}")
    if is_symlink:
        raise ValueError(f"Symlinks are not supported: {filename}")
    return name


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


def safe_tar_member(info: tarfile.TarInfo) -> PurePosixPath:
    name = PurePosixPath(info.name)
    if (
        name.is_absolute()
        or ".." in name.parts
        or "\\" in info.name
        or re.match(r"^[A-Za-z]:", info.name)
    ):
        raise ValueError(f"Unsafe TAR member: {info.name}")
    if info.issym() or info.islnk() or info.isdev():
        raise ValueError(f"TAR symlinks are not supported: {info.name}")
    return name


def safe_fs_path(base: Path, path: Path) -> PurePosixPath:
    if path.is_symlink():
        raise ValueError(f"Symlinks are not supported: {path.name}")
    try:
        rel = path.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"Path outside base: {path.name}") from exc
    name = PurePosixPath(rel.as_posix())
    if (
        name.is_absolute()
        or ".." in name.parts
        or "\\" in rel.as_posix()
        or re.match(r"^[A-Za-z]:", rel.as_posix())
    ):
        raise ValueError(f"Unsafe member: {path.name}")
    return name


def decode_chat(raw: bytes) -> str:
    return raw.decode(
        "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    )


def read_chat_data(name: PurePosixPath, raw: bytes, order: str) -> list[dict] | None:
    if name.suffix.lower() != ".txt":
        return None
    named_chat = name.name.lower() == "_chat.txt" or name.stem.lower().startswith(
        "whatsapp chat"
    )
    try:
        return whatsapp.parse_chat(decode_chat(raw), order)
    except (whatsapp.NoChatMessages, UnicodeError) as exc:
        if named_chat:
            raise ValueError(f"Cannot read chat {name}: {exc}") from exc
        return None
    except ValueError as exc:
        raise ValueError(f"Cannot read chat {name}: {exc}") from exc


def read_chat_member(
    zipped: zipfile.ZipFile, info: zipfile.ZipInfo, order: str
) -> list[dict] | None:
    return read_chat_data(PurePosixPath(info.filename), zipped.read(info), order)


def is_chat_file(path: Path, order: str = "day-first") -> bool:
    if path.suffix.lower() != ".txt":
        return False
    name_lower = path.name.lower()
    if name_lower == "_chat.txt" or name_lower.startswith("whatsapp chat"):
        return True
    try:
        raw = path.read_bytes()[:65536]
        text = decode_chat(raw)
        messages = whatsapp.parse_chat(text, order)
        return len(messages) > 0
    except Exception:
        return False


def dir_contains_archives(dir_path: Path) -> bool:
    for p in dir_path.rglob("*"):
        if p.is_file() and not p.is_symlink():
            if is_archive_file(p):
                return True
    return False


def is_export_dir(dir_path: Path, inbox: Path) -> bool:
    if dir_path == inbox or not dir_path.is_dir() or dir_path.is_symlink():
        return False
    if dir_path.name.startswith(".") or dir_path.name == "__MACOSX":
        return False
    if dir_contains_archives(dir_path):
        return False
    for child in dir_path.iterdir():
        if child.is_file() and not child.is_symlink() and not child.name.startswith("."):
            if is_chat_file(child):
                return True
    return False


def directory_checksum(dir_path: Path) -> str:
    hasher = hashlib.sha256()
    for child in sorted(dir_path.rglob("*")):
        if child.is_file() and not child.is_symlink():
            if (
                child.name == ".DS_Store"
                or child.name.startswith("._")
                or "__MACOSX" in child.parts
            ):
                continue
            rel = child.relative_to(dir_path).as_posix()
            file_hash = digest.sha256(child)
            hasher.update(f"{rel}:{file_hash}\n".encode("utf-8"))
    return hasher.hexdigest()


class InputMember:
    def __init__(self, name: PurePosixPath, is_dir: bool, open_func):
        self.name = name
        self.is_dir = is_dir
        self.open = open_func


class ZipInputSource:
    def __init__(self, path: Path, project_root: Path):
        self.path = path
        self.name = path.name
        self.relative_path = path.relative_to(project_root).as_posix()
        self.checksum = digest.sha256(path)

    @contextlib.contextmanager
    def iter_members(self):
        with zipfile.ZipFile(self.path) as zipped:
            members = []
            for info in zipped.infolist():
                name = safe_member(info)
                members.append(
                    InputMember(
                        name,
                        info.is_dir(),
                        lambda inf=info: zipped.open(inf),
                    )
                )
            yield members


class TarInputSource:
    def __init__(self, path: Path, project_root: Path):
        self.path = path
        self.name = path.name
        self.relative_path = path.relative_to(project_root).as_posix()
        self.checksum = digest.sha256(path)

    @contextlib.contextmanager
    def iter_members(self):
        with tarfile.open(self.path, "r:*") as tar:
            members = []
            for info in tar.getmembers():
                name = safe_tar_member(info)
                members.append(
                    InputMember(
                        name,
                        info.isdir(),
                        lambda inf=info: tar.extractfile(inf) or io.BytesIO(),
                    )
                )
            yield members


class DirectoryInputSource:
    def __init__(self, path: Path, project_root: Path):
        self.path = path
        self.name = path.name
        self.relative_path = path.relative_to(project_root).as_posix()
        self.checksum = directory_checksum(path)

    @contextlib.contextmanager
    def iter_members(self):
        members = []
        for child in sorted(self.path.rglob("*")):
            if child.is_file() and not child.is_symlink():
                if (
                    child.name == ".DS_Store"
                    or child.name.startswith("._")
                    or "__MACOSX" in child.parts
                ):
                    continue
                name = safe_fs_path(self.path, child)
                members.append(
                    InputMember(
                        name,
                        False,
                        lambda c=child: c.open("rb"),
                    )
                )
        yield members


class LooseFileInputSource:
    def __init__(self, path: Path, project_root: Path):
        self.path = path
        self.name = path.name
        self.relative_path = path.relative_to(project_root).as_posix()
        self.checksum = digest.sha256(path)

    @contextlib.contextmanager
    def iter_members(self):
        name = safe_fs_path(self.path.parent, self.path)
        yield [
            InputMember(
                PurePosixPath(name.name),
                False,
                lambda: self.path.open("rb"),
            )
        ]


def create_input_source(path: Path, project_root: Path):
    if path.is_dir():
        return DirectoryInputSource(path, project_root)
    if is_archive_file(path):
        if path.name.lower().endswith(".zip"):
            return ZipInputSource(path, project_root)
        return TarInputSource(path, project_root)
    return LooseFileInputSource(path, project_root)


def discover_inputs(project_dir: Path) -> list:
    inbox = project_dir / "input"
    if not inbox.is_dir() or inbox.is_symlink():
        return []

    export_dirs = set()
    for item in sorted(inbox.rglob("*")):
        if item.is_dir() and is_export_dir(item, inbox):
            if not any(d in item.parents for d in export_dirs):
                export_dirs.add(item)

    sources = [
        DirectoryInputSource(d, project_dir) for d in sorted(export_dirs)
    ]

    for p in sorted(inbox.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        if p.name == ".DS_Store" or p.name.startswith("._") or "__MACOSX" in p.parts:
            continue
        if any(d in p.parents for d in export_dirs):
            continue
        sources.append(create_input_source(p, project_dir))

    def sort_key(s):
        is_chat = isinstance(s, LooseFileInputSource) and is_chat_file(s.path)
        return (is_chat, s.relative_path)

    sources.sort(key=sort_key)
    return sources


def count_inputs(inbox: Path) -> int:
    if not inbox.is_dir() or inbox.is_symlink():
        return 0
    return len(discover_inputs(inbox.parent))


def merge_chat_messages(
    state: dict, chats: list, members: dict, checkpoint: str
) -> tuple[list[str], int]:
    lookup = {}
    for media_id, record in state.get("media", {}).items():
        for alias in record.get("aliases", []):
            lookup.setdefault(PurePosixPath(alias).name, set()).add(media_id)
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


def link_attachments(state: dict) -> bool:
    lookup = {}
    for media_id, record in state.get("media", {}).items():
        for alias in record.get("aliases", []):
            lookup.setdefault(PurePosixPath(alias).name, set()).add(media_id)
    changed = False
    for message in state.get("messages", {}).values():
        for attachment in whatsapp.attachment_names(message.get("body", "")):
            for media_id in sorted(lookup.get(PurePosixPath(attachment).name, set())):
                if media_id not in message["attachments"]:
                    message["attachments"].append(media_id)
                    changed = True
    return changed


def import_checkpoint(
    path: Path,
    source_or_archive: Any,
    checksum: str,
    state: dict,
    order: str,
) -> tuple[int, int]:
    if isinstance(source_or_archive, Path):
        source = create_input_source(source_or_archive, path)
    else:
        source = source_or_archive

    updated = copy.deepcopy(state)
    chat_files, members, staged = [], {}, {}
    before_messages, before_media = len(state["messages"]), len(state["media"])
    with (
        tempfile.TemporaryDirectory(
            prefix="import-", dir=path / ".digester"
        ) as temporary,
        source.iter_members() as member_list,
    ):
        stage = Path(temporary)
        for member in member_list:
            name = member.name
            if (
                member.is_dir
                or name.name == ".DS_Store"
                or "__MACOSX" in name.parts
                or name.name.startswith("._")
            ):
                continue
            if name.suffix.lower() == ".txt":
                with member.open() as stream:
                    raw = stream.read()
                messages = read_chat_data(name, raw, order)
                if messages is not None:
                    chat_files.append((name.as_posix(), messages))
                    continue
                content_hash = hashlib.sha256(raw)
                with (stage / "member").open("wb") as output:
                    output.write(raw)
            else:
                content_hash = hashlib.sha256()
                with member.open() as stream, (stage / "member").open("wb") as output:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
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
            if (stage / "member").exists():
                (stage / "member").unlink()
            if name.as_posix() not in record["aliases"]:
                record["aliases"].append(name.as_posix())
            if checksum not in record["checkpoints"]:
                record["checkpoints"].append(checksum)
        if not chat_files and not members:
            raise ValueError(
                f"Input contains no recognized WhatsApp chat transcript or media files: {source.name}"
            )
        dates, added_messages = merge_chat_messages(
            updated, chat_files, members, checksum
        )
        if dates:
            updated["date_order"] = order
        updated["checkpoints"][checksum] = updated["checkpoints"].get(checksum) or {
            "archives": [source.relative_path],
            "chats": [name for name, _ in chat_files],
            "from": min(dates) if dates else None,
            "through": max(dates) if dates else None,
            "imported_utc": dt.datetime.now(dt.UTC).isoformat(),
            "messages_added": added_messages,
            "media_added": len(updated["media"]) - before_media,
            "members": members,
        }
        for media_id, source_staged in staged.items():
            destination = (
                path / "target" / "todo" / updated["media"][media_id]["source"]
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            source_staged.replace(destination)
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
    sources = discover_inputs(path)
    for source in sources:
        checksum = source.checksum
        previous = state["checkpoints"].get(checksum)
        missing_media = previous and any(
            not (
                path / "target" / "todo" / state["media"][media_id]["source"]
            ).is_file()
            for media_id in previous["members"].values()
        )
        if previous and not missing_media:
            alias = source.relative_path
            if alias not in previous["archives"]:
                previous["archives"].append(alias)
                save_state(path, state)
            print(f"Checkpoint unchanged: {source.name}", flush=True)
            continue
        try:
            messages, media = import_checkpoint(path, source, checksum, state, order)
            print(
                f"Imported {source.name}: {messages} new messages, {media} new media",
                flush=True,
            )
        except (
            ValueError,
            OSError,
            RuntimeError,
            zipfile.BadZipFile,
            tarfile.TarError,
        ) as exc:
            error = f"{source.name}: {exc}"
            errors.append(error)
            prefix = "ZIP ERROR" if source.name.lower().endswith(".zip") else "ERROR"
            print(f"{prefix}: {error}", flush=True)
    if link_attachments(state):
        save_state(path, state)
    render_project(path, state, errors)
    return errors


def show_projects() -> int:
    print(f"Projects: {ROOT}")
    for path in sorted(ROOT.iterdir()) if ROOT.exists() else []:
        if path.is_dir() and not path.is_symlink() and SLUG.fullmatch(path.name):
            inputs = count_inputs(path / "input")
            print(f"  {path.name}: {inputs} input(s)")
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
            f"Project ready: {path}\nDrop exports, archives, or media files into: {path / 'input'}\nThen: ./run.sh convert {args.project}"
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
                f"No checkpoints imported. Put WhatsApp exports, archives, or media files in {path / 'input'}"
            )
            return 1 if errors else 0
        import launch

        with digest.target_directory(path / "target"):
            result = launch.run_legacy(["run", *conversion_options])
        return 1 if errors or result else 0
