#!/usr/bin/env python3
"""Kioku's pending list: the items that wait for the user's decision, one list per vault.

Notes still save live; this list holds only what needs the user, such as a note a
new decision replaces, or a change to a knowledge-base file. The kioku-hooks
plugin and the kioku-memory skills both use it, each through its own copy of
this file, vendored from the marketplace's shared/ folder.

  kioku_pending.py add --vault V --domain D --kind K --source S --text T [--detail-file F]
  kioku_pending.py list --vault V [--domain D] [--json]
  kioku_pending.py done --vault V ID [--resolution R]

`add` prints the new item's id. An open item with the same domain, kind and text
is updated instead, and its id printed.

Store: ~/.local/state/kioku/pending/<vault>.json, or $KIOKU_PENDING_DIR/<vault>.json,
with each item's detail in <vault>/<id>.json beside it. Writes are atomic and made
under a lock. Items done more than 90 days ago are pruned.

Standard library only; runs on Python 3.9.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import tempfile
import time
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

KINDS = ("retire", "kb", "decision", "unverified", "regression", "other")
VAULT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
DETAIL_LIMIT = 1_000_000
KEEP_DONE = timedelta(days=90)
LOCK_SECONDS = 5.0
FRACTION = re.compile(r"\.(\d+)")


class PendingError(Exception):
    """A pending command that can't be carried out, with a message for the user."""


def pending_dir() -> Path:
    return Path(os.environ.get("KIOKU_PENDING_DIR") or Path.home() / ".local" / "state" / "kioku" / "pending").expanduser()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def now() -> str:
    return utc_now().isoformat(timespec="seconds")


def parse_time(text) -> Optional[datetime]:
    """A timestamp as an aware UTC datetime, or None. A timestamp without an offset is local time."""
    if not isinstance(text, str) or not text.strip():
        return None
    value = text.strip()
    if value[-1] in "Zz":
        value = value[:-1] + "+00:00"
    # Python 3.9 reads only 3 or 6 digits of fractional seconds.
    value = FRACTION.sub(lambda m: "." + (m.group(1) + "000000")[:6], value, count=1)
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.astimezone(timezone.utc)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_bytes(path: Path, data: bytes) -> None:
    """Write atomically: a temporary file of its own, then a rename over the old file."""
    handle, temp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        os.replace(temp, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(temp)
        raise


def acquire(handle, seconds: float, poll: float = 0.05) -> bool:
    """Lock `handle` exclusively, retrying without blocking for up to `seconds`."""
    until = time.monotonic() + seconds
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            remaining = until - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(poll, remaining))


@dataclass
class PendingItem:
    id: str
    kind: str
    domain: str
    source: str
    text: str
    created: str
    status: str = "open"
    detail: str = ""
    # What made the item, when it can resolve itself, such as "review:<transcript digest>".
    ref: str = ""
    done_at: str = ""
    resolution: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "PendingItem":
        names = {item.name for item in fields(cls)}
        return cls(**{name: str(value) for name, value in data.items() if name in names and value is not None})


@dataclass
class PendingList:
    next: int = 1
    items: list = field(default_factory=list)


def in_domain(domain: str, scope: Optional[str]) -> bool:
    return not scope or domain == scope or domain.startswith(scope.rstrip("/") + "/")


class PendingStore:
    """The items of one vault that wait for the user's decision: <vault>.json, with each
    item's detail in <vault>/<id>.json. Writes are atomic and made under a lock."""

    def __init__(self, vault: str, root: Optional[Path] = None) -> None:
        if not VAULT_NAME.fullmatch(vault or ""):
            raise PendingError(f"not a vault name: {vault!r}")
        self.vault = vault
        self.root = root or pending_dir()
        self.path = self.root / f"{vault}.json"
        self.details = self.root / vault

    def load(self) -> PendingList:
        data = read_json(self.path, {})
        if not isinstance(data, dict):
            data = {}
        items = [PendingItem.from_dict(item) for item in data.get("items", [])
                 if isinstance(item, dict) and item.get("id")]
        try:
            next_id = max(int(data.get("next") or 1), 1)
        except (TypeError, ValueError):
            next_id = 1
        return PendingList(next_id, items)

    def open_items(self, domain: Optional[str] = None) -> list:
        return [item for item in self.load().items if item.status == "open" and in_domain(item.domain, domain)]

    @contextmanager
    def editing(self) -> Iterator[PendingList]:
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / f"{self.vault}.lock").open("w") as lock:
            if not acquire(lock, LOCK_SECONDS):
                raise PendingError(f"the pending list of vault {self.vault} is busy; try again")
            pending = self.load()
            yield pending
            pending.items = self.pruned(pending.items)
            data = {"version": 1, "next": pending.next, "items": [asdict(i) for i in pending.items]}
            write_bytes(self.path, json.dumps(data).encode("utf-8"))

    def pruned(self, items: list) -> list:
        """The items without those done more than 90 days ago, whose detail files go too."""
        cutoff = utc_now() - KEEP_DONE
        kept = []
        for item in items:
            done_at = parse_time(item.done_at)
            if item.status == "done" and done_at is not None and done_at < cutoff:
                with suppress(OSError):
                    (self.details / f"{item.id}.json").unlink()
                continue
            kept.append(item)
        return kept

    def write_detail(self, item: PendingItem, detail: bytes) -> None:
        self.details.mkdir(parents=True, exist_ok=True)
        target = self.details / f"{item.id}.json"
        write_bytes(target, detail)
        item.detail = str(target)

    def add(self, domain: str, kind: str, source: str, text: str, detail: Optional[bytes] = None,
            ref: str = "") -> str:
        """Add an item and return its id. An open item with the same ref, or the same domain,
        kind and text, is updated instead, and its id returned."""
        text = " ".join((text or "").split())
        if not text:
            raise PendingError("the item's text is empty")
        if kind not in KINDS:
            raise PendingError(f"unknown kind {kind!r}: use one of {', '.join(KINDS)}")
        with self.editing() as pending:
            for item in pending.items:
                same = item.ref == ref if ref else (item.domain, item.kind, item.text) == (domain, kind, text)
                if item.status == "open" and same:
                    item.text, item.source = text, source
                    if detail is not None:
                        self.write_detail(item, detail)
                    return item.id
            item = PendingItem(id=f"p{pending.next}", kind=kind, domain=domain, source=source, text=text,
                               created=now(), ref=ref)
            pending.next += 1
            if detail is not None:
                self.write_detail(item, detail)
            pending.items.append(item)
            return item.id

    def done(self, ident: str, resolution: str = "") -> PendingItem:
        with self.editing() as pending:
            for item in pending.items:
                if item.id == ident:
                    if item.status != "done":
                        item.status, item.done_at = "done", now()
                        item.resolution = " ".join((resolution or "").split())
                    return item
        raise PendingError(f"no item {ident} in vault {self.vault}")

    def resolve(self, ref: str, resolution: str) -> int:
        """Mark the open items made by `ref` done; returns how many."""
        if not any(item.ref == ref for item in self.open_items()):
            return 0
        resolved = 0
        with self.editing() as pending:
            for item in pending.items:
                if item.status == "open" and item.ref == ref:
                    item.status, item.done_at, item.resolution = "done", now(), resolution
                    resolved += 1
        return resolved


# --- command line ---------------------------------------------------------------

def read_detail(path: str) -> bytes:
    try:
        detail = Path(path).expanduser().read_bytes()
    except OSError as error:
        raise PendingError(f"can't read --detail-file: {error}") from None
    if len(detail) > DETAIL_LIMIT:
        raise PendingError(f"--detail-file is over {DETAIL_LIMIT} bytes")
    try:
        json.loads(detail.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise PendingError(f"--detail-file is not JSON: {error}") from None
    return detail


def run(store: PendingStore, options: argparse.Namespace) -> int:
    if options.command == "add":
        detail = read_detail(options.detail_file) if options.detail_file else None
        print(store.add(options.domain, options.kind, options.source, options.text, detail))
    elif options.command == "list":
        items = store.open_items(options.domain)
        if options.json:
            print(json.dumps([asdict(item) for item in items], indent=1))
        elif not items:
            print(f"No open items in vault {store.vault}.")
        for item in [] if options.json else items:
            print(f"{item.id} [{item.kind}, {item.domain}] {item.text} ({item.source}, {item.created[:10]})")
            if item.detail:
                print(f"    detail: {item.detail}")
    else:
        item = store.done(options.id, options.resolution)
        print(f"{item.id} done" + (f": {item.resolution}" if item.resolution else ""))
    return 0


def main(args: Optional[list] = None, prog: str = "kioku_pending.py") -> int:
    parser = argparse.ArgumentParser(
        prog=prog, description="Items that wait for the user's decision, one list per Kioku vault. Notes still save live.")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="add an item and print its id")
    add.add_argument("--vault", required=True)
    add.add_argument("--domain", required=True)
    add.add_argument("--kind", required=True, choices=KINDS)
    add.add_argument("--source", required=True, help='who proposed it and when, e.g. "dream 2026-11-07"')
    add.add_argument("--text", required=True, help="the item in one line")
    add.add_argument("--detail-file", help="a JSON file with the details; it is copied into the store")
    listing = commands.add_parser("list", help="show the open items")
    listing.add_argument("--vault", required=True)
    listing.add_argument("--domain", help="only items in this domain or below it")
    listing.add_argument("--json", action="store_true")
    done = commands.add_parser("done", help="mark an item done")
    done.add_argument("--vault", required=True)
    done.add_argument("id")
    done.add_argument("--resolution", default="", help="what the user decided")
    try:
        options = parser.parse_args(sys.argv[1:] if args is None else args)
    except SystemExit as exit:  # usage errors and --help: argparse has printed why
        return exit.code if isinstance(exit.code, int) else 2
    try:
        return run(PendingStore(options.vault), options)
    except (PendingError, OSError) as error:
        print(f"pending: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
