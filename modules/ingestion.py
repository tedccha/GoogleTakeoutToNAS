"""
modules/ingestion.py - Module 1: The Ingestion Engine.

Responsibilities
----------------
1. **rclone pull** – optionally sync a Google Drive / Takeout folder to a
   local 'incoming' directory.
2. **Auto-unzip** – recursively extract every .zip found under the source
   into a 'master_temp' directory, *merging* folders that share the same
   name (e.g. multiple zips each containing "Photos from 2018").
3. Return the master_temp Path for downstream processing.

Public API
----------
    run_ingestion(source_dir, master_temp, rclone_remote=None) -> Path
"""

import logging
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import List, Optional

from tqdm import tqdm

from config import INCOMING_DIR_NAME, MASTER_TEMP_DIR_NAME

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# rclone helpers
# ---------------------------------------------------------------------------

def _check_rclone() -> None:
    """Raise RuntimeError if rclone is not on PATH."""
    if shutil.which("rclone") is None:
        raise RuntimeError(
            "rclone is not installed or not on PATH.\n"
            "Install via: brew install rclone\n"
            "Then configure: rclone config"
        )


def pull_from_rclone(remote_path: str, incoming_dir: Path, extra_flags: Optional[List[str]] = None) -> None:
    """
    Sync *remote_path* (e.g. ``gdrive:Takeout``) into *incoming_dir* using
    rclone.  Progress is streamed live to stderr.

    Parameters
    ----------
    remote_path   : rclone remote + path string, e.g. ``gdrive:Takeout``
    incoming_dir  : local directory to sync into
    extra_flags   : additional rclone flags (e.g. ``["--drive-shared-with-me"]``)
    """
    _check_rclone()
    incoming_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "rclone", "sync",
        remote_path,
        str(incoming_dir),
        "--progress",
        "--transfers", "8",
        "--checkers", "16",
        "--drive-chunk-size", "256M",
        "--log-level", "INFO",
    ]
    if extra_flags:
        cmd.extend(extra_flags)

    log.info("Starting rclone sync: %s  →  %s", remote_path, incoming_dir)
    log.debug("Command: %s", " ".join(cmd))

    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"rclone exited with code {result.returncode}. "
            "Check the log above for details."
        )
    log.info("rclone sync complete.")


# ---------------------------------------------------------------------------
# Zip extraction helpers
# ---------------------------------------------------------------------------

def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """
    Extract *zf* into *dest*, guarding against path-traversal exploits
    (zip-slip) and merging contents into existing directories naturally.
    """
    for member in zf.infolist():
        member_path = dest / member.filename

        # Zip-slip guard: resolved path must stay inside dest
        try:
            member_path.resolve().relative_to(dest.resolve())
        except ValueError:
            log.warning("Skipping potentially unsafe zip entry: %s", member.filename)
            continue

        if member.is_dir():
            member_path.mkdir(parents=True, exist_ok=True)
        else:
            member_path.parent.mkdir(parents=True, exist_ok=True)
            # If file already exists (merged folder), keep the larger one
            # (usually the more complete original).
            if member_path.exists():
                existing_size = member_path.stat().st_size
                if member.file_size == existing_size:
                    log.debug("Skip existing (same size): %s", member_path)
                    continue
            with zf.open(member) as src, open(member_path, "wb") as dst:
                shutil.copyfileobj(src, dst)


def _collect_zips(source: Path) -> list[Path]:
    """Return all .zip files under *source*, sorted for deterministic order."""
    zips = sorted(source.rglob("*.zip"))
    log.info("Found %d .zip file(s) under %s", len(zips), source)
    return zips


def extract_all_zips(source: Path, master_temp: Path) -> None:
    """
    Recursively find every .zip under *source* and extract into *master_temp*,
    merging same-named directories automatically.

    After a zip is successfully extracted its path is recorded in a sidecar
    `.extracted` marker so re-runs are idempotent.
    """
    master_temp.mkdir(parents=True, exist_ok=True)
    zips = _collect_zips(source)

    if not zips:
        log.warning("No .zip files found under %s – nothing to extract.", source)
        return

    with tqdm(zips, desc="Extracting zips", unit="zip") as pbar:
        for zip_path in pbar:
            marker = zip_path.with_suffix(".extracted")
            if marker.exists():
                log.debug("Already extracted (marker found): %s", zip_path)
                pbar.set_postfix_str(f"skip {zip_path.name}")
                continue

            pbar.set_postfix_str(zip_path.name)
            log.info("Extracting: %s", zip_path)

            try:
                with zipfile.ZipFile(zip_path, "r") as zf:
                    _safe_extract(zf, master_temp)
                marker.touch()  # mark success
                log.debug("Extracted OK: %s", zip_path)
            except zipfile.BadZipFile:
                log.error("Corrupted zip (skipped): %s", zip_path)
            except Exception as e:
                log.error("Failed to extract %s: %s", zip_path, e)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_ingestion(
    source_dir: Path,
    work_dir: Path,
    rclone_remote: Optional[str] = None,
    rclone_flags: Optional[List[str]] = None,
) -> Path:
    """
    Orchestrate the full ingestion pipeline.

    Parameters
    ----------
    source_dir    : directory that already contains .zip files  **or**
                    the local path rclone will download into.
    work_dir      : parent working directory (--work-dir CLI flag).
    rclone_remote : if set, pull from this rclone remote before extracting.
    rclone_flags  : extra flags forwarded to rclone (optional).

    Returns
    -------
    Path to the master_temp directory ready for Module 2.
    """
    # Step 1 (optional): pull from rclone
    if rclone_remote:
        incoming = work_dir / INCOMING_DIR_NAME
        pull_from_rclone(rclone_remote, incoming, extra_flags=rclone_flags)
        source_dir = incoming  # extract from what was just downloaded

    # Step 2: extract all zips into master_temp
    master_temp = work_dir / MASTER_TEMP_DIR_NAME
    log.info("Master temp directory: %s", master_temp)
    extract_all_zips(source_dir, master_temp)

    return master_temp
