"""
Live download monitor for the MnDOT bridge inspection PDF scraper.

Run this in any terminal while the scraper is working:

    python -m backend.scripts.monitor_scrape

Or with a custom total bridge count:

    python -m backend.scripts.monitor_scrape --total 2630

Refreshes every 5 seconds. Press Ctrl+C to exit.
Pure stdlib — no extra dependencies needed.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import time
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from collections import deque

from backend.config import settings


# ─── ANSI colour helpers ───────────────────────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
GREEN  = "\033[92m"
CYAN   = "\033[96m"
YELLOW = "\033[93m"
RED    = "\033[91m"
BLUE   = "\033[94m"
WHITE  = "\033[97m"
BG_DARK = "\033[48;5;235m"


def clr(text: str, *codes: str) -> str:
    return "".join(codes) + text + RESET


def clear_screen() -> None:
    # Move cursor to top-left and clear screen
    print("\033[H\033[J", end="", flush=True)


def bar(filled: int, total: int, width: int = 40) -> str:
    """Render a Unicode progress bar."""
    if total == 0:
        pct = 0.0
    else:
        pct = filled / total
    n_filled = int(width * pct)
    n_empty  = width - n_filled

    if pct < 0.33:
        colour = RED
    elif pct < 0.66:
        colour = YELLOW
    else:
        colour = GREEN

    filled_str = clr("█" * n_filled, colour)
    empty_str  = clr("░" * n_empty, DIM)
    return f"[{filled_str}{empty_str}]"


def human_size(n_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n_bytes < 1024:
            return f"{n_bytes:.1f} {unit}"
        n_bytes /= 1024
    return f"{n_bytes:.1f} TB"


def human_duration(seconds: float) -> str:
    if seconds < 0 or seconds != seconds:  # negative or NaN
        return "—"
    td = timedelta(seconds=int(seconds))
    h, rem = divmod(td.seconds, 3600)
    m, s   = divmod(rem, 60)
    if td.days:
        return f"{td.days}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


# ─── Scan helpers ─────────────────────────────────────────────────────────────

def scan_pdfs(pdf_dir: Path) -> dict:
    """
    Walk pdf_dir and collect stats on all downloaded PDFs.

    Returns:
        count       : int   — number of valid PDFs
        total_bytes : int   — total size in bytes
        latest_file : Path  — most recently modified PDF (or None)
        latest_mtime: float — mtime of latest_file
        files       : list[tuple[float, Path]]  — (mtime, path) sorted newest-first
    """
    files = []
    total_bytes = 0

    if not pdf_dir.exists():
        return {
            "count": 0, "total_bytes": 0,
            "latest_file": None, "latest_mtime": 0,
            "files": [],
        }

    for p in pdf_dir.rglob("*.pdf"):
        try:
            st = p.stat()
            # Basic sanity: skip empty / tiny files (partial downloads)
            if st.st_size > 1024:
                files.append((st.st_mtime, p, st.st_size))
                total_bytes += st.st_size
        except OSError:
            pass

    files.sort(key=lambda x: x[0], reverse=True)

    return {
        "count"       : len(files),
        "total_bytes" : total_bytes,
        "latest_file" : files[0][1] if files else None,
        "latest_mtime": files[0][0] if files else 0,
        "files"       : files,
    }


# ─── Dashboard renderer ───────────────────────────────────────────────────────

def render(
    stats: dict,
    prev_count: int,
    total: int,
    start_time: float,
    rate_window: deque,   # deque of (timestamp, count) snapshots
    refresh_sec: int,
    iteration: int,
) -> None:
    """Render one full-screen dashboard frame."""

    now      = time.time()
    count    = stats["count"]
    elapsed  = now - start_time
    W        = 56   # dashboard width

    # ── Speed calculation ─────────────────────────────────────────────────────
    # Use a sliding window of the last ~2 minutes of snapshots for a stable rate
    rate_window.append((now, count))
    while rate_window and (now - rate_window[0][0]) > 120:
        rate_window.popleft()

    if len(rate_window) >= 2:
        t0, c0 = rate_window[0]
        t1, c1 = rate_window[-1]
        window_secs = t1 - t0
        files_per_sec = (c1 - c0) / window_secs if window_secs > 0 else 0
    else:
        files_per_sec = count / elapsed if elapsed > 0 else 0

    remaining    = max(0, total - count)
    eta_secs     = remaining / files_per_sec if files_per_sec > 0 else float("inf")
    files_per_min = files_per_sec * 60

    # ── Header ────────────────────────────────────────────────────────────────
    spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[iteration % 10]
    header_line = f" {spinner}  MnDOT Bridge PDF Monitor "
    pad = W - len(header_line)
    print(clr("━" * W, CYAN, BOLD))
    print(clr(header_line + " " * pad, CYAN, BOLD))
    print(clr("━" * W, CYAN, BOLD))

    # ── Progress bar ──────────────────────────────────────────────────────────
    pct = (count / total * 100) if total else 0
    progress_bar = bar(count, total, width=W - 2)
    print(f"\n  {progress_bar}")
    count_str  = clr(f"{count:,}", GREEN, BOLD)
    total_str  = clr(f"{total:,}", WHITE)
    pct_str    = clr(f"{pct:.1f}%", YELLOW, BOLD)
    print(f"  {count_str} / {total_str} bridges  {pct_str}\n")

    # ── Stats grid ────────────────────────────────────────────────────────────
    def stat_row(label: str, value: str) -> str:
        label_col = clr(f"  {label:<18}", DIM)
        value_col = clr(value, WHITE, BOLD)
        return label_col + value_col

    size_str   = human_size(stats["total_bytes"])
    speed_str  = (f"{files_per_min:.1f} files/min  "
                  f"({human_size(int(stats['total_bytes'] / elapsed if elapsed else 0))}/s)")
    eta_str    = human_duration(eta_secs) if eta_secs != float("inf") else "calculating..."
    elapsed_str = human_duration(elapsed)

    print(stat_row("Disk used", size_str))
    print(stat_row("Speed", speed_str))
    print(stat_row("ETA", eta_str))
    print(stat_row("Elapsed", elapsed_str))

    # ── Latest files ──────────────────────────────────────────────────────────
    print(f"\n  {clr('Recent downloads:', CYAN, BOLD)}")
    recent = stats["files"][:6]
    if recent:
        for mtime, path, size in recent:
            age = now - mtime
            age_str = clr(f"{human_duration(age)} ago", DIM)
            name    = clr(path.name, GREEN)
            sz      = clr(f"({human_size(size)})", DIM)
            print(f"    {name} {sz}  {age_str}")
    else:
        print(clr("    No PDFs downloaded yet — waiting for scraper...", DIM))

    # ── Footer ────────────────────────────────────────────────────────────────
    print(f"\n{clr('━' * W, CYAN, BOLD)}")
    now_str = datetime.now().strftime("%H:%M:%S")
    hint    = clr(f"  Refreshing every {refresh_sec}s  •  {now_str}  •  Ctrl+C to exit", DIM)
    print(hint)

    if count == total and total > 0:
        print(f"\n  {clr('✅  All bridges downloaded!', GREEN, BOLD)}\n")


# ─── Main loop ────────────────────────────────────────────────────────────────

def main(total: int, refresh_sec: int) -> None:
    pdf_dir    = settings.raw_pdf_dir
    start_time = time.time()
    prev_count = 0
    rate_window: deque = deque()
    iteration  = 0

    # Hide cursor for cleaner rendering
    print("\033[?25l", end="", flush=True)

    try:
        while True:
            stats = scan_pdfs(pdf_dir)
            clear_screen()
            render(stats, prev_count, total, start_time, rate_window, refresh_sec, iteration)
            sys.stdout.flush()

            prev_count = stats["count"]
            iteration += 1

            # Stop automatically when all done
            if stats["count"] >= total:
                time.sleep(2)
                break

            time.sleep(refresh_sec)

    except KeyboardInterrupt:
        clear_screen()
        print(clr("\n  Monitor stopped. Scraper continues in background.\n", YELLOW))
    finally:
        # Restore cursor
        print("\033[?25h", end="", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Live dashboard for the MnDOT bridge PDF scraper"
    )
    parser.add_argument(
        "--total",
        type=int,
        default=2630,
        help="Total number of bridges to download (default: 2630)",
    )
    parser.add_argument(
        "--refresh",
        type=int,
        default=5,
        help="Refresh interval in seconds (default: 5)",
    )
    args = parser.parse_args()
    main(total=args.total, refresh_sec=args.refresh)
