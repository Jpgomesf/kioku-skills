#!/usr/bin/env python3
"""Copy each shared file into the plugins that use it.

Installing a plugin copies only its own folder, so a file two plugins need lives
once under shared/ and is vendored into each of them. Edit the file under shared/,
then run `make vendor`. `make lint` fails while a copy differs from its source.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Source under shared/ -> its copies, relative to the repository root.
VENDORED: dict = {
    "shared/kioku_pending.py": [
        "plugins/kioku-memory/skills/checkpoint/scripts/kioku_pending.py",
        "plugins/kioku-hooks/scripts/kioku_pending.py",
    ],
    "shared/note-format.md": [
        "plugins/kioku-memory/skills/checkpoint/references/note-format.md",
        "plugins/kioku-hooks/references/note-format.md",
    ],
    "shared/dream_state.py": [
        "plugins/kioku-memory/skills/dream/scripts/dream_state.py",
        "plugins/kioku-hooks/scripts/dream_state.py",
    ],
    "shared/kb_drift.py": [
        "plugins/kioku-memory/skills/memory-refresh/scripts/kb_drift.py",
        "plugins/kioku-hooks/scripts/kb_drift.py",
    ],
}


def stale_copies() -> list:
    """Each copy that is missing or differs from its source, as (source, copy) pairs."""
    stale = []
    for source, copies in VENDORED.items():
        data = (ROOT / source).read_bytes()
        for copy in copies:
            target = ROOT / copy
            if not target.is_file() or target.read_bytes() != data:
                stale.append((source, copy))
    return stale


def vendor() -> list:
    """Write every stale copy; returns the copies written."""
    written = []
    for source, copy in stale_copies():
        target = ROOT / copy
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / source, target)
        shutil.copymode(ROOT / source, target)
        written.append(copy)
    return written


def main() -> int:
    written = vendor()
    for copy in written:
        print(f"  wrote {copy}")
    print(f"{len(written)} copies updated." if written else "Every copy matches its source.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
