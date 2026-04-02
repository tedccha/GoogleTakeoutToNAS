"""
modules/reporter.py - Verbose post-migration archive report.

Generates a human-readable, reconciliation-friendly report covering:
  • Run metadata (timestamp, duration, source, destination)
  • Overall totals: photos vs videos, moved vs skipped vs errored
  • Per-month breakdown: file counts, first/last timestamp, total size,
    GPS-tagged percentage, filenames list (optional)
  • Timeline coverage: gaps between months, date-range of the archive
  • Top months by volume
  • Deduplication summary
  • Error/warning digest (no-sidecar, exif errors, verify failures)
  • Reconciliation checklist ready to compare with Google Photos

The report is written to a UTF-8 plain-text file AND returned as a string
for console display.

Public API
----------
    build_report(results, report, run_meta, verbose_filenames) -> str
    save_report(text, dest_path)
"""

import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from config import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS

log = logging.getLogger(__name__)

# ── Months names ──────────────────────────────────────────────────────────────
_MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

# ── Formatting helpers ────────────────────────────────────────────────────────

def _fmt_ts(dt: Optional[datetime]) -> str:
    if dt is None:
        return "unknown"
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")

def _fmt_size(n_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n_bytes < 1024:
            return f"{n_bytes:.1f} {unit}"
        n_bytes /= 1024
    return f"{n_bytes:.1f} PB"

def _fmt_duration(seconds: float) -> str:
    td = timedelta(seconds=int(seconds))
    h, rem = divmod(td.seconds, 3600)
    m, s   = divmod(rem, 60)
    if td.days:
        return f"{td.days}d {h:02d}h {m:02d}m {s:02d}s"
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"

def _bar(count: int, total: int, width: int = 20) -> str:
    if total == 0:
        return " " * width
    filled = round(width * count / total)
    return "█" * filled + "░" * (width - filled)

def _sep(char: str = "─", width: int = 72) -> str:
    return char * width

# ── Run metadata dataclass ────────────────────────────────────────────────────

@dataclass
class RunMeta:
    started_at:   datetime
    elapsed_sec:  float
    source:       Optional[Path]
    rclone_remote: Optional[str]
    nas_root:     Path
    work_dir:     Path
    dry_run:      bool
    tool_version: str = "1.0.0"


# ── Per-month statistics ──────────────────────────────────────────────────────

@dataclass
class MonthStats:
    year:       int
    month:      int
    photos:     int = 0
    videos:     int = 0
    size_bytes: int = 0
    gps_tagged: int = 0
    first_ts:   Optional[datetime] = None
    last_ts:    Optional[datetime] = None
    filenames:  list = field(default_factory=list)   # dest filenames

    @property
    def total(self) -> int:
        return self.photos + self.videos

    @property
    def label(self) -> str:
        return f"{self.year}-{self.month:02d}  {_MONTH_NAMES[self.month]:<9}"


# ── Core data-gathering ───────────────────────────────────────────────────────

def _gather_stats(results, report) -> tuple[dict, list, list]:
    """
    Walk results + report and produce:
      monthly  : dict[(year,month) -> MonthStats]
      no_sidecar : list of ProcessResult
      errors   : list of ProcessResult | MoveRecord
    """
    from modules.metadata_processor import ProcessResult
    from modules.organizer import MoveRecord

    # Build a fast lookup: src_path -> ProcessResult
    result_map: dict[Path, "ProcessResult"] = {r.path: r for r in results}

    monthly: dict[tuple, MonthStats] = {}
    no_sidecar: list = []
    errors: list    = []

    # ── Moved files ──────────────────────────────────────────────────────────
    for rec in report.moved:
        if rec.status in ("error", "verify_fail"):
            errors.append(rec)
            continue
        if rec.status == "dry_run":
            dest = rec.dest
        else:
            dest = rec.dest

        pr = result_map.get(rec.src)
        meta = pr.metadata if pr else None

        # Determine date bucket
        dt: Optional[datetime] = None
        if meta and meta.date_taken:
            dt = meta.date_taken
        elif dest and len(dest.parts) >= 2:
            # Infer from Year/Month folder structure
            try:
                year  = int(dest.parts[-3])
                month = int(dest.parts[-2])
                dt    = datetime(year, month, 1, tzinfo=timezone.utc)
            except (ValueError, IndexError):
                pass

        if dt is None:
            # Fallback: use mtime of source if still available
            try:
                mts = rec.src.stat().st_mtime
                dt  = datetime.fromtimestamp(mts, tz=timezone.utc)
            except OSError:
                dt = datetime(1970, 1, 1, tzinfo=timezone.utc)

        key = (dt.year, dt.month)
        if key not in monthly:
            monthly[key] = MonthStats(year=dt.year, month=dt.month)
        ms = monthly[key]

        ext = rec.src.suffix.lower()
        if ext in IMAGE_EXTENSIONS:
            ms.photos += 1
        elif ext in VIDEO_EXTENSIONS:
            ms.videos += 1

        # File size (from dest if moved, src if dry-run)
        try:
            size_path = dest if dest.exists() else rec.src
            ms.size_bytes += size_path.stat().st_size
        except OSError:
            pass

        # GPS tag flag
        if meta and meta.latitude is not None:
            ms.gps_tagged += 1

        # Timestamps
        if meta and meta.date_taken:
            if ms.first_ts is None or meta.date_taken < ms.first_ts:
                ms.first_ts = meta.date_taken
            if ms.last_ts is None or meta.date_taken > ms.last_ts:
                ms.last_ts = meta.date_taken

        ms.filenames.append(dest.name if dest else rec.src.name)

    # ── Skipped files ────────────────────────────────────────────────────────
    for pr in report.skipped:
        if hasattr(pr, "status"):
            if pr.status == "no_sidecar":
                no_sidecar.append(pr)
            elif pr.status == "exif_error":
                errors.append(pr)

    return monthly, no_sidecar, errors


# ── Report builder ────────────────────────────────────────────────────────────

def build_report(
    results,
    report,
    run_meta: RunMeta,
    verbose_filenames: bool = False,
) -> str:
    """
    Assemble the full text report.

    Parameters
    ----------
    results           : list[ProcessResult]   from process_all()
    report            : OrganiseReport        from organise()
    run_meta          : RunMeta               populated by main.py
    verbose_filenames : if True, list every filename under each month

    Returns
    -------
    Formatted UTF-8 string suitable for printing or saving.
    """
    monthly, no_sidecar, move_errors = _gather_stats(results, report)

    sorted_months = sorted(monthly.items())   # [(year,month), MonthStats]

    # ── Aggregate totals ─────────────────────────────────────────────────────
    total_photos   = sum(ms.photos  for ms in monthly.values())
    total_videos   = sum(ms.videos  for ms in monthly.values())
    total_media    = total_photos + total_videos
    total_size     = sum(ms.size_bytes for ms in monthly.values())
    total_gps      = sum(ms.gps_tagged for ms in monthly.values())
    total_dupes    = sum(1 for r in report.skipped if hasattr(r, "status") and r.status == "duplicate")
    total_moved    = sum(1 for r in report.moved   if r.status in ("moved", "dry_run"))
    total_no_side  = len(no_sidecar)
    total_errors   = len(move_errors)

    archive_first: Optional[datetime] = None
    archive_last:  Optional[datetime] = None
    for ms in monthly.values():
        if ms.first_ts:
            if archive_first is None or ms.first_ts < archive_first:
                archive_first = ms.first_ts
        if ms.last_ts:
            if archive_last is None or ms.last_ts > archive_last:
                archive_last = ms.last_ts

    lines: list[str] = []
    W = 72   # report width

    def h1(title: str) -> None:
        lines.append("")
        lines.append(_sep("═", W))
        lines.append(f"  {title}")
        lines.append(_sep("═", W))

    def h2(title: str) -> None:
        lines.append("")
        lines.append(f"  {title}")
        lines.append("  " + _sep("─", W - 2))

    def row(label: str, value: str) -> None:
        lines.append(f"  {label:<28}  {value}")

    # ════════════════════════════════════════════════════════════════════════
    # Header
    # ════════════════════════════════════════════════════════════════════════
    h1("GoogleTakeoutToLongviewstorage — Archive Migration Report")
    lines.append("")
    row("Report generated",    _fmt_ts(run_meta.started_at))
    row("Migration duration",  _fmt_duration(run_meta.elapsed_sec))
    row("Mode",                "DRY-RUN (no files written)" if run_meta.dry_run else "LIVE")
    row("Tool version",        run_meta.tool_version)
    lines.append("")
    row("Source",              str(run_meta.source) if run_meta.source else "(rclone pull)")
    if run_meta.rclone_remote:
        row("rclone remote",   run_meta.rclone_remote)
    row("NAS destination",     str(run_meta.nas_root))
    row("Work directory",      str(run_meta.work_dir))

    # ════════════════════════════════════════════════════════════════════════
    # Overall Totals
    # ════════════════════════════════════════════════════════════════════════
    h1("OVERALL TOTALS")
    lines.append("")
    row("📷  Photos archived",    f"{total_photos:>7,}")
    row("🎥  Videos archived",    f"{total_videos:>7,}")
    row("📁  Total media moved",  f"{total_media:>7,}")
    row("💾  Total size",         f"{_fmt_size(total_size):>10}")
    row("🌍  GPS-tagged files",   f"{total_gps:>7,}  ({100*total_gps/max(total_media,1):.1f}%)")
    lines.append("")
    row("⏭   Duplicates skipped", f"{total_dupes:>7,}  (already on NAS)")
    row("⚠️   No sidecar found",   f"{total_no_side:>7,}  (moved without EXIF write)")
    row("❌  Errors",             f"{total_errors:>7,}")
    lines.append("")
    row("📅  Archive span",
        f"{_fmt_ts(archive_first)}  →  {_fmt_ts(archive_last)}")
    if archive_first and archive_last:
        span_days = (archive_last - archive_first).days
        row("   Span (calendar days)", f"{span_days:,} days  ≈  {span_days/365.25:.1f} years")

    # ════════════════════════════════════════════════════════════════════════
    # Per-Month Breakdown
    # ════════════════════════════════════════════════════════════════════════
    h1("PER-MONTH BREAKDOWN")
    lines.append("")

    # Column header
    col_hdr = (
        f"  {'Month':<22}  {'Photos':>7}  {'Videos':>7}  {'Total':>6}  "
        f"{'Size':>9}  {'GPS%':>5}  {'Bar (total)':20}"
    )
    lines.append(col_hdr)
    lines.append("  " + _sep("─", W - 2))

    max_total = max((ms.total for ms in monthly.values()), default=1)

    for (year, month), ms in sorted_months:
        gps_pct = f"{100*ms.gps_tagged/max(ms.total,1):.0f}%"
        bar     = _bar(ms.total, max_total, 20)
        lines.append(
            f"  {ms.label:<22}  {ms.photos:>7,}  {ms.videos:>7,}  "
            f"{ms.total:>6,}  {_fmt_size(ms.size_bytes):>9}  {gps_pct:>5}  {bar}"
        )
        if verbose_filenames and ms.filenames:
            for fn in sorted(ms.filenames):
                lines.append(f"       • {fn}")

    lines.append("  " + _sep("─", W - 2))
    lines.append(
        f"  {'TOTAL':<22}  {total_photos:>7,}  {total_videos:>7,}  "
        f"{total_media:>6,}  {_fmt_size(total_size):>9}"
    )

    # ════════════════════════════════════════════════════════════════════════
    # Timestamp Details (first & last per month)
    # ════════════════════════════════════════════════════════════════════════
    h1("FIRST & LAST PHOTO TIMESTAMP PER MONTH")
    lines.append("")
    lines.append(f"  {'Month':<22}  {'First capture':<26}  {'Last capture':<26}")
    lines.append("  " + _sep("─", W - 2))

    for (year, month), ms in sorted_months:
        first_s = _fmt_ts(ms.first_ts)
        last_s  = _fmt_ts(ms.last_ts)
        lines.append(f"  {ms.label:<22}  {first_s:<26}  {last_s:<26}")

    # ════════════════════════════════════════════════════════════════════════
    # Top 10 Months by Volume
    # ════════════════════════════════════════════════════════════════════════
    if len(sorted_months) > 5:
        h1("TOP 10 MONTHS BY FILE COUNT")
        lines.append("")
        top10 = sorted(monthly.values(), key=lambda m: m.total, reverse=True)[:10]
        for rank, ms in enumerate(top10, 1):
            bar = _bar(ms.total, top10[0].total, 30)
            lines.append(f"  {rank:>2}. {ms.label:<22}  {ms.total:>5,} files  {bar}")

    # ════════════════════════════════════════════════════════════════════════
    # Timeline Coverage & Gap Analysis
    # ════════════════════════════════════════════════════════════════════════
    h1("TIMELINE COVERAGE & GAPS")
    lines.append("")

    if sorted_months:
        present = {(y, m) for (y, m) in monthly}
        (y_start, m_start) = sorted_months[0][0]
        (y_end,   m_end)   = sorted_months[-1][0]

        # Walk every calendar month in range
        gaps: list[str] = []
        y, m = y_start, m_start
        while (y, m) <= (y_end, m_end):
            if (y, m) not in present:
                gaps.append(f"{y}-{m:02d}  {_MONTH_NAMES[m]}")
            m += 1
            if m > 12:
                m = 1
                y += 1

        total_months_in_range = (
            (y_end - y_start) * 12 + (m_end - m_start) + 1
        )
        row("Months with photos",  f"{len(present)}")
        row("Total months in span",f"{total_months_in_range}")
        row("Months with NO files",f"{len(gaps)}")
        lines.append("")

        if gaps:
            lines.append("  Months with no archived media (possible gaps):")
            # Print in rows of 4
            for i in range(0, len(gaps), 4):
                chunk = gaps[i:i+4]
                lines.append("    " + "    ".join(f"{g:<20}" for g in chunk))
        else:
            lines.append("  ✓  No gaps — continuous coverage for the entire span.")
    else:
        lines.append("  (no dated files to analyse)")

    # ════════════════════════════════════════════════════════════════════════
    # Deduplication Summary
    # ════════════════════════════════════════════════════════════════════════
    h1("DEDUPLICATION SUMMARY")
    lines.append("")
    row("Files seen in Takeout",    f"{len(results):>7,}")
    row("Already on NAS (skipped)", f"{total_dupes:>7,}")
    row("New files archived",       f"{total_moved:>7,}")
    if len(results) > 0:
        dedup_pct = 100 * total_dupes / len(results)
        row("Deduplication rate",   f"{dedup_pct:.1f}%")

    # ════════════════════════════════════════════════════════════════════════
    # Files Without Sidecars
    # ════════════════════════════════════════════════════════════════════════
    if no_sidecar:
        h1(f"FILES MOVED WITHOUT METADATA SIDECAR  ({len(no_sidecar):,})")
        lines.append("  These files were archived but EXIF date/GPS could not be injected.")
        lines.append("  They will appear undated in Synology Photos until manually tagged.")
        lines.append("")
        for pr in sorted(no_sidecar, key=lambda r: r.path.name):
            lines.append(f"    • {pr.path.name}")

    # ════════════════════════════════════════════════════════════════════════
    # Errors
    # ════════════════════════════════════════════════════════════════════════
    if move_errors:
        h1(f"ERRORS  ({len(move_errors):,})")
        lines.append("  These files were NOT successfully archived.")
        lines.append("")
        for err in move_errors:
            if hasattr(err, "src"):
                lines.append(f"    ✗  {err.src.name}")
                lines.append(f"       Status : {err.status}")
                if err.error:
                    lines.append(f"       Detail : {err.error}")
            else:
                lines.append(f"    ✗  {err.path.name}  [{err.status}]  {err.error or ''}")

    # ════════════════════════════════════════════════════════════════════════
    # Google Photos Reconciliation Checklist
    # ════════════════════════════════════════════════════════════════════════
    h1("RECONCILIATION CHECKLIST")
    lines.append("")
    lines.append("  Use this section to cross-check against your Google Photos library.")
    lines.append("")
    lines.append(f"  Archive date range  :  {_fmt_ts(archive_first)}")
    lines.append(f"                      →  {_fmt_ts(archive_last)}")
    lines.append("")
    lines.append(f"  Total files archived:  {total_media:,}  ({total_photos:,} photos + {total_videos:,} videos)")
    lines.append(f"  Total size          :  {_fmt_size(total_size)}")
    lines.append(f"  GPS-tagged          :  {total_gps:,}  ({100*total_gps/max(total_media,1):.1f}%)")
    lines.append(f"  Months covered      :  {len(monthly)}")
    lines.append("")
    lines.append("  Steps to reconcile:")
    lines.append("   1. Open Google Photos → Library → check total item count.")
    lines.append(f"      Expected:  ≥ {total_media:,}  (Takeout may exclude shared/hidden albums)")
    lines.append("   2. In Synology Photos, verify the folder count under each year")
    lines.append("      matches the Month counts above.")
    lines.append("   3. Spot-check 3–5 months from the gap list above in Google Photos")
    lines.append("      to confirm those months genuinely had no photos.")
    lines.append("   4. Search Synology Photos for photos with no date — these are")
    lines.append(f"      the {total_no_side:,} file(s) listed in the 'No Sidecar' section.")
    lines.append("   5. If counts diverge significantly, re-run with --rebuild-manifest")
    lines.append("      and compare the new report.")
    lines.append("")
    lines.append("  NAS destination layout:")
    lines.append(f"    {run_meta.nas_root}/")
    for (year, month), ms in sorted_months[:6]:
        lines.append(f"      {year}/{month:02d}/   ({ms.total} files, {_fmt_size(ms.size_bytes)})")
    if len(sorted_months) > 6:
        lines.append(f"      … and {len(sorted_months) - 6} more month-folders")

    # ════════════════════════════════════════════════════════════════════════
    # Footer
    # ════════════════════════════════════════════════════════════════════════
    lines.append("")
    lines.append(_sep("═", W))
    lines.append(f"  End of report  ·  Generated {run_meta.started_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    lines.append(_sep("═", W))
    lines.append("")

    return "\n".join(lines)


# ── File I/O ──────────────────────────────────────────────────────────────────

def save_report(text: str, dest: Path) -> None:
    """Write the report text to *dest* (UTF-8, creates parent dirs)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    log.info("Archive report saved → %s", dest)
