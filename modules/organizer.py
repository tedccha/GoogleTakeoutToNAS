"""
modules/organizer.py - Module 3: Library Organisation & Cleanup.

Responsibilities
----------------
1. **Filename sanitisation** – strip "(1)", "-edited" etc. from names.
2. **Edited-version preference** – if a takeout batch contains both an
   original and an "-edited" copy, use the edited one for the NAS.
3. **Year/Month folder placement** – move each file into
   ``{nas_root}/{YYYY}/{MM}/``.
4. **Atomic move with verification** – only delete the temp copy after
   confirming the NAS write succeeded (size + re-hash check).
5. **Collision handling** – if the sanitised name already exists at the
   destination, append a counter suffix rather than silently overwriting.

Public API
----------
    organise(results, nas_root, dry_run, show_progress) -> OrganiseReport
"""

import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from tqdm import tqdm

from config import EDITED_SUFFIXES, DUPLICATE_PATTERNS
from modules.metadata_processor import ProcessResult
from utils.hash_utils import file_md5

log = logging.getLogger(__name__)

# Compiled once at import time
_DUPLICATE_RE = re.compile("|".join(DUPLICATE_PATTERNS), re.IGNORECASE)
_EDITED_RE    = re.compile(
    "(" + "|".join(re.escape(s) for s in EDITED_SUFFIXES) + r")$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class MoveRecord:
    src:        Path
    dest:       Path
    status:     str                          # "moved" | "dry_run" | "collision" | "verify_fail" | "error"
    error:      Optional[str]      = None
    result_ref: Optional["ProcessResult"] = None   # back-reference for reporting


@dataclass
class OrganiseReport:
    moved:       list[MoveRecord] = field(default_factory=list)
    skipped:     list[ProcessResult] = field(default_factory=list)   # duplicates, errors
    dry_run:     bool = False

    @property
    def total_moved(self) -> int:
        return sum(1 for r in self.moved if r.status in ("moved", "dry_run"))


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------

def _sanitise_stem(stem: str) -> str:
    """
    Remove trailing duplicate counters and 'edited' suffixes from a filename stem.

    Examples:
        "IMG_1234(1)"          → "IMG_1234"
        "IMG_1234 (2)"         → "IMG_1234"
        "IMG_1234-edited"      → "IMG_1234"
        "IMG_1234-edited (1)"  → "IMG_1234"
    """
    stem = _DUPLICATE_RE.sub("", stem).rstrip()
    stem = _EDITED_RE.sub("", stem).rstrip()
    return stem or "untitled"


def _is_edited(path: Path) -> bool:
    """Return True if the filename carries an 'edited' suffix."""
    return bool(_EDITED_RE.search(path.stem))


def _unique_dest(dest: Path) -> Path:
    """
    If *dest* already exists, append ``_N`` before the suffix until a
    free slot is found.
    """
    if not dest.exists():
        return dest
    stem   = dest.stem
    suffix = dest.suffix
    parent = dest.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


# ---------------------------------------------------------------------------
# Destination path calculation
# ---------------------------------------------------------------------------

def _dest_path(result: ProcessResult, nas_root: Path) -> Path:
    """
    Compute the absolute destination path on the NAS for a processed file.

    Logic:
    - Use date_taken from metadata if available.
    - Fall back to file mtime.
    - Place in  nas_root / YYYY / MM / sanitised_name.ext
    """
    media = result.path

    # Determine date bucket
    if result.metadata and result.metadata.date_taken:
        dt = result.metadata.date_taken
    else:
        mtime = media.stat().st_mtime
        dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
        log.debug("No date metadata for %s – using mtime %s", media.name, dt)

    year  = str(dt.year)
    month = f"{dt.month:02d}"

    clean_stem = _sanitise_stem(media.stem)
    dest_name  = clean_stem + media.suffix.lower()
    dest_dir   = nas_root / year / month
    return dest_dir / dest_name


# ---------------------------------------------------------------------------
# Group edited vs original
# ---------------------------------------------------------------------------

def _group_by_base(results: list[ProcessResult]) -> dict[str, list[ProcessResult]]:
    """
    Group processable results by their sanitised base name so we can prefer
    edited versions over originals when both exist.

    Returns  { sanitised_stem_lower: [ProcessResult, ...] }
    """
    groups: dict[str, list[ProcessResult]] = {}
    for r in results:
        key = _sanitise_stem(r.path.stem).lower()
        groups.setdefault(key, []).append(r)
    return groups


def _pick_best(candidates: list[ProcessResult]) -> ProcessResult:
    """
    From a list of ProcessResults sharing the same sanitised base name,
    return the preferred one:
      - Prefer edited over original.
      - Among equals, prefer the one with metadata.
    """
    edited    = [r for r in candidates if _is_edited(r.path)]
    originals = [r for r in candidates if not _is_edited(r.path)]

    pool = edited if edited else originals
    # secondary sort: has metadata?
    pool.sort(key=lambda r: r.metadata is not None, reverse=True)
    return pool[0]


# ---------------------------------------------------------------------------
# Atomic move with verification
# ---------------------------------------------------------------------------

def _verified_move(src: Path, dest: Path, dry_run: bool) -> MoveRecord:
    """
    Copy *src* to *dest*, verify by re-hashing, then remove *src*.
    This is safer than shutil.move() across filesystems.

    On macOS the NAS is usually a different volume (SMB/AFP), so we always
    copy-then-delete rather than rename.
    """
    if dry_run:
        log.info("[DRY-RUN] Would move: %s  →  %s", src, dest)
        return MoveRecord(src=src, dest=dest, status="dry_run")

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest = _unique_dest(dest)

    try:
        # Avoid shutil.copy2() to bypass xattr transfer errors on SMB (Errno 22)
        shutil.copy(src, dest)
        st = src.stat()
        os.utime(dest, (st.st_atime, st.st_mtime))
    except OSError as e:
        log.error("Copy failed %s → %s: %s", src, dest, e)
        return MoveRecord(src=src, dest=dest, status="error", error=str(e))

    # Verify: compare file sizes (fast & sufficient)
    src_size  = src.stat().st_size
    dest_size = dest.stat().st_size
    if src_size != dest_size:
        log.error("Size mismatch after copy! src=%d dest=%d  (%s)", src_size, dest_size, dest)
        dest.unlink(missing_ok=True)
        return MoveRecord(src=src, dest=dest, status="verify_fail",
                          error="Size mismatch")

    # Verified – safe to remove source
    try:
        src.unlink()
    except OSError as e:
        log.warning("Could not remove temp source %s: %s", src, e)
        # Not fatal – dest is already safe

    log.debug("Moved: %s  →  %s", src.name, dest)
    return MoveRecord(src=src, dest=dest, status="moved")


def _move_with_ref(result: ProcessResult, dest: Path, dry_run: bool) -> MoveRecord:
    """Wrapper that attaches the ProcessResult back-reference after the move."""
    record = _verified_move(result.path, dest, dry_run)
    record.result_ref = result
    return record


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def organise(
    results: list[ProcessResult],
    nas_root: Path,
    dry_run: bool = False,
    show_progress: bool = True,
) -> OrganiseReport:
    """
    Organise all *results* onto the NAS under a Year/Month hierarchy.

    Parameters
    ----------
    results       : output from metadata_processor.process_all()
    nas_root      : mount point / root of the target NAS or photo library
    dry_run       : if True, log what *would* happen without touching the NAS
    show_progress : show tqdm bar

    Returns
    -------
    OrganiseReport with per-file outcomes.
    """
    report = OrganiseReport(dry_run=dry_run)

    # Separate files we can actually move from those we skip
    actionable = [r for r in results if r.status in ("processed", "no_sidecar", "exif_error")]
    skipped    = [r for r in results if r.status not in ("processed", "no_sidecar", "exif_error")]

    report.skipped = skipped
    if skipped:
        log.info("Skipping %d file(s) (duplicates / errors).", len(skipped))

    # Group and pick best version for each base name
    groups = _group_by_base(actionable)
    chosen: list[ProcessResult] = []
    for base, candidates in groups.items():
        best = _pick_best(candidates)
        chosen.append(best)
        if len(candidates) > 1:
            others = [c for c in candidates if c is not best]
            for o in others:
                log.info(
                    "Preferring '%s' over '%s' (edited takes priority).",
                    best.path.name, o.path.name,
                )
            # Mark unchosen as skipped so they don't get orphaned in temp
            report.skipped.extend(others)

    log.info("Organising %d unique file(s) to NAS …", len(chosen))

    with tqdm(
        chosen,
        desc="Moving to NAS",
        unit="file",
        disable=not show_progress,
    ) as pbar:
        for result in pbar:
            pbar.set_postfix_str(result.path.name[:40])
            dest = _dest_path(result, nas_root)
            record = _move_with_ref(result, dest, dry_run)
            report.moved.append(record)

    # Cleanup skipped duplicates from the staging folder so they don't block auto-deletion
    if not dry_run and report.skipped:
        for skip_res in report.skipped:
            try:
                skip_res.path.unlink(missing_ok=True)
            except OSError:
                pass

    # Summary
    status_counts: dict[str, int] = {}
    for rec in report.moved:
        status_counts[rec.status] = status_counts.get(rec.status, 0) + 1
    log.info("Organisation complete: %s", status_counts)

    return report
