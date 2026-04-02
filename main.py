#!/usr/bin/env python3
"""
GoogleTakeoutToLongviewstorage
==============================
Automates the migration of a Google Photos library (exported via Google
Takeout) to a Synology NAS, with metadata injection, deduplication, and
a clean Year/Month folder hierarchy.

Usage examples
--------------
# Local zips already downloaded:
python main.py \
    --source  ~/Downloads/Takeout \
    --work-dir ~/Desktop/takeout_work \
    --nas     /Volumes/photo \
    --rebuild-manifest

# Pull from Google Drive via rclone then process:
python main.py \
    --rclone-remote "gdrive:Takeout" \
    --work-dir ~/Desktop/takeout_work \
    --nas     /Volumes/photo

# Dry-run (no files moved to NAS):
python main.py \
    --source  ~/Downloads/Takeout \
    --work-dir ~/Desktop/takeout_work \
    --nas     /Volumes/photo \
    --dry-run

Run `python main.py --help` for full option reference.
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap: ensure the project root is on sys.path so relative imports work
# when running as a script (not a package).
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent))

from config import HASH_MANIFEST_FILE, MASTER_TEMP_DIR_NAME
from utils.logging_config import setup_logging
from utils.hash_utils import build_manifest, load_manifest, save_manifest
from modules.ingestion import run_ingestion
from modules.metadata_processor import process_all
from modules.organizer import organise
from modules.reporter import RunMeta, build_report, save_report

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CLI definition
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="GoogleTakeoutToLongviewstorage",
        description=(
            "Migrate Google Takeout photos/videos to a Synology NAS with "
            "metadata injection, deduplication, and Year/Month organisation."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # --- Input sources (mutually exclusive group) --------------------------
    src_grp = p.add_argument_group("Input source (choose one or both)")
    src_grp.add_argument(
        "--source", "-s",
        metavar="DIR",
        type=Path,
        help="Local directory containing already-downloaded .zip files.",
    )
    src_grp.add_argument(
        "--rclone-remote", "-r",
        metavar="REMOTE:PATH",
        help=(
            "rclone remote + path to sync before processing "
            "(e.g. 'gdrive:Takeout'). Requires rclone to be installed and configured."
        ),
    )
    src_grp.add_argument(
        "--rclone-flags",
        metavar="FLAGS",
        nargs=argparse.REMAINDER,
        default=[],
        help="Extra flags to pass verbatim to rclone (e.g. --drive-shared-with-me).",
    )

    # --- Working directory -------------------------------------------------
    p.add_argument(
        "--work-dir", "-w",
        metavar="DIR",
        type=Path,
        required=True,
        help=(
            "Scratch space for incoming downloads and the master_temp tree. "
            "Should be on a fast local disk with plenty of free space."
        ),
    )

    # --- NAS destination ---------------------------------------------------
    p.add_argument(
        "--nas", "-n",
        metavar="DIR",
        type=Path,
        required=True,
        help=(
            "Root of the Synology photo library mount point "
            "(e.g. /Volumes/photo or /Volumes/photo/PhotoLibrary)."
        ),
    )

    # --- Manifest options --------------------------------------------------
    mfst_grp = p.add_argument_group("Deduplication manifest")
    mfst_grp.add_argument(
        "--rebuild-manifest",
        action="store_true",
        help=(
            "Force a full re-scan of the NAS to rebuild the hash manifest, "
            "even if a cached .nas_manifest.json already exists."
        ),
    )
    mfst_grp.add_argument(
        "--skip-manifest",
        action="store_true",
        help="Skip NAS scanning entirely (no deduplication – faster but unsafe for re-runs).",
    )

    # --- Behaviour flags ---------------------------------------------------
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate everything but do NOT copy files to the NAS.",
    )
    p.add_argument(
        "--keep-temp",
        action="store_true",
        help="Do not delete master_temp after a successful run.",
    )
    p.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable DEBUG-level output to the console.",
    )

    # --- Phase selectors (advanced) ----------------------------------------
    phase_grp = p.add_argument_group("Phase selectors (advanced)")
    phase_grp.add_argument(
        "--skip-ingest",
        action="store_true",
        help="Skip Module 1 (assume master_temp already exists).",
    )
    phase_grp.add_argument(
        "--skip-metadata",
        action="store_true",
        help="Skip ExifTool EXIF injection (organise only).",
    )

    # --- Report options ----------------------------------------------------
    report_grp = p.add_argument_group("Archive report")
    report_grp.add_argument(
        "--report-filenames",
        action="store_true",
        help="Include every filename in the per-month section of the report (verbose).",
    )
    report_grp.add_argument(
        "--report-out",
        metavar="FILE",
        type=Path,
        default=None,
        help=(
            "Path to write the archive report. "
            "Defaults to <work-dir>/archive_report_<timestamp>.txt"
        ),
    )

    return p


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_args(args: argparse.Namespace) -> None:
    """Raise SystemExit with a helpful message on invalid argument combinations."""

    if not args.skip_ingest:
        if args.source is None and args.rclone_remote is None:
            _die(
                "Provide at least one input source: --source or --rclone-remote.\n"
                "Use --skip-ingest if master_temp already exists."
            )
        if args.source and not args.source.is_dir():
            _die(f"--source directory does not exist: {args.source}")

    if args.dry_run:
        log.warning("DRY-RUN mode enabled – no files will be moved to the NAS.")

    # NAS mount check (skip in dry-run to allow offline testing)
    if not args.dry_run and not args.nas.is_dir():
        _die(
            f"NAS directory not found: {args.nas}\n"
            "Make sure the Synology volume is mounted (Finder → Go → Connect to Server)."
        )

    # Apple Silicon path advice
    nas_str = str(args.nas)
    if nas_str.startswith("/Volumes/") and sys.platform == "darwin":
        log.info(
            "Detected macOS mount point. If you see permission errors, grant "
            "Full Disk Access to Terminal/iTerm in System Preferences → Privacy & Security."
        )


def _die(msg: str) -> None:
    print(f"\n[ERROR] {msg}\n", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Manifest handling
# ---------------------------------------------------------------------------

def _load_or_build_manifest(args: argparse.Namespace) -> dict:
    """Return the NAS hash manifest, loading from cache or building fresh."""
    manifest_path = args.nas / HASH_MANIFEST_FILE

    if args.skip_manifest:
        log.warning("--skip-manifest: deduplication is DISABLED.")
        return {}

    if not args.rebuild_manifest:
        cached = load_manifest(manifest_path)
        if cached:
            log.info("Loaded cached manifest (%d entries) from %s", len(cached), manifest_path)
            return cached
        log.info("No cached manifest found – building fresh.")

    manifest = build_manifest(args.nas)
    save_manifest(manifest, manifest_path)
    return manifest


# ---------------------------------------------------------------------------
# Report helpers
# ---------------------------------------------------------------------------

def _report_path(args: argparse.Namespace) -> Path:
    """Return the destination path for the archive report file."""
    if args.report_out:
        return args.report_out
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return args.work_dir / f"archive_report_{ts}.txt"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = build_parser()
    args   = parser.parse_args()

    # Set up logging before anything else
    setup_logging(args.work_dir, verbose=args.verbose)
    log.info("=" * 60)
    log.info("GoogleTakeoutToLongviewstorage starting")
    log.info("  work-dir : %s", args.work_dir)
    log.info("  NAS root : %s", args.nas)
    log.info("  dry-run  : %s", args.dry_run)
    log.info("=" * 60)

    _validate_args(args)
    t0 = time.perf_counter()

    # -----------------------------------------------------------------------
    # Phase 1 – Ingestion
    # -----------------------------------------------------------------------
    if args.skip_ingest:
        master_temp = args.work_dir / MASTER_TEMP_DIR_NAME
        if not master_temp.is_dir():
            _die(
                f"--skip-ingest specified but master_temp does not exist: {master_temp}\n"
                "Run without --skip-ingest first."
            )
        log.info("Skipping ingestion – using existing master_temp: %s", master_temp)
    else:
        log.info("▶ Phase 1: Ingestion")
        master_temp = run_ingestion(
            source_dir    = args.source or args.work_dir,
            work_dir      = args.work_dir,
            rclone_remote = args.rclone_remote,
            rclone_flags  = args.rclone_flags or None,
        )

    # -----------------------------------------------------------------------
    # Phase 2 – Build / load NAS hash manifest
    # -----------------------------------------------------------------------
    log.info("▶ Phase 2: NAS Manifest")
    nas_manifest = _load_or_build_manifest(args)

    # -----------------------------------------------------------------------
    # Phase 3 – Metadata processing
    # -----------------------------------------------------------------------
    if args.skip_metadata:
        log.info("Skipping metadata processing (--skip-metadata).")
        # Create stub results from whatever is in master_temp
        from modules.metadata_processor import ProcessResult
        media_files = [
            p for p in master_temp.rglob("*")
            if p.is_file() and p.suffix.lower() in __import__("config").MEDIA_EXTENSIONS
        ]
        results = [ProcessResult(path=p, status="no_sidecar") for p in media_files]
    else:
        log.info("▶ Phase 3: Metadata & Deduplication")
        results = process_all(master_temp, nas_manifest)

    if not results:
        log.warning("No media files found in master_temp – nothing to organise.")
        return 0

    # -----------------------------------------------------------------------
    # Phase 4 – Organisation
    # -----------------------------------------------------------------------
    log.info("▶ Phase 4: Library Organisation")
    report = organise(results, args.nas, dry_run=args.dry_run)

    # -----------------------------------------------------------------------
    # Phase 5 – Cleanup temp directory
    # -----------------------------------------------------------------------
    if not args.dry_run and not args.keep_temp:
        remaining = list(master_temp.rglob("*"))
        media_remaining = [
            p for p in remaining
            if p.is_file() and p.suffix.lower() in __import__("config").MEDIA_EXTENSIONS
        ]
        if media_remaining:
            log.warning(
                "%d media file(s) still in master_temp – skipping cleanup to be safe.",
                len(media_remaining),
            )
        else:
            import shutil as _shutil
            log.info("Cleaning up master_temp: %s", master_temp)
            try:
                _shutil.rmtree(master_temp)
                log.info("master_temp removed.")
            except OSError as e:
                log.warning("Could not remove master_temp: %s", e)
    elif args.keep_temp:
        log.info("--keep-temp: master_temp preserved at %s", master_temp)

    # -----------------------------------------------------------------------
    # Phase 6 – Archive Report
    # -----------------------------------------------------------------------
    elapsed = time.perf_counter() - t0
    log.info("▶ Phase 6: Generating archive report")

    run_meta = RunMeta(
        started_at    = datetime.fromtimestamp(t0, tz=timezone.utc),
        elapsed_sec   = elapsed,
        source        = args.source,
        rclone_remote = getattr(args, "rclone_remote", None),
        nas_root      = args.nas,
        work_dir      = args.work_dir,
        dry_run       = args.dry_run,
    )

    report_text = build_report(
        results            = results,
        report             = report,
        run_meta           = run_meta,
        verbose_filenames  = getattr(args, "report_filenames", False),
    )

    # Print to stdout so it appears in the terminal after the progress bars
    print(report_text)

    # Save to file
    rpt_path = _report_path(args)
    save_report(report_text, rpt_path)
    print(f"\n  📄  Full report saved → {rpt_path}\n")

    error_count = sum(1 for r in report.moved if r.status in ("error", "verify_fail"))
    return 1 if error_count else 0


if __name__ == "__main__":
    sys.exit(main())
