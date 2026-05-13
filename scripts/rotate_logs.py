"""CLI: rotate ~/Library/Logs/vw-scraper*.log files.

Runs daily via `com.colby.vw-scraper.logrotate` launchd plist at 4 AM, well
between the daily scrape (9 AM) and the weekly health check (Sun 10 AM). At
4 AM launchd is not holding a descriptor on the log files — between job
runs each StandardOutPath/StandardErrorPath file is closed — so renaming
is safe.

For each matching log file:
- If size <= MAX_BYTES, leave it.
- Otherwise rotate: file.log.3 → discard, file.log.2 → .log.3, ..., file.log → .log.1.
- A new empty file.log is recreated so launchd's next append still finds a target
  (avoids a permission surprise if the file owner ever drifted).

Caps log history at 4 rotations × 5MB = ~20MB total per stream. With current
log volume (~3KB per daily run), that's >15 years of history per stream.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024
KEEP_ROTATIONS = 4

DEFAULT_LOG_DIR = Path.home() / "Library" / "Logs"
DEFAULT_PATTERN = "vw-scraper*.log"


def rotate(path: Path, max_bytes: int, keep: int) -> bool:
    """Rotate `path` if it exceeds max_bytes. Returns True if rotation happened."""
    if not path.exists() or path.stat().st_size <= max_bytes:
        return False

    oldest = path.with_suffix(path.suffix + f".{keep}")
    if oldest.exists():
        oldest.unlink()

    for i in range(keep - 1, 0, -1):
        src = path.with_suffix(path.suffix + f".{i}")
        dst = path.with_suffix(path.suffix + f".{i + 1}")
        if src.exists():
            src.rename(dst)

    rotated = path.with_suffix(path.suffix + ".1")
    path.rename(rotated)
    path.touch()
    return True


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    p.add_argument("--pattern", default=DEFAULT_PATTERN)
    p.add_argument("--max-bytes", type=int, default=MAX_BYTES)
    p.add_argument("--keep", type=int, default=KEEP_ROTATIONS)
    args = p.parse_args(argv)

    if not args.log_dir.exists():
        print(f"log dir does not exist: {args.log_dir}", file=sys.stderr)
        return 0  # nothing to rotate; not an error

    rotated_any = False
    for path in sorted(args.log_dir.glob(args.pattern)):
        if path.suffix != ".log":
            continue
        if rotate(path, args.max_bytes, args.keep):
            print(f"rotated {path.name} (>{args.max_bytes // 1024 // 1024}MB)")
            rotated_any = True

    if not rotated_any:
        print("no rotation needed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
