"""
modules/metadata_processor.py - Module 2: Metadata & Deduplication Processor.

Responsibilities
----------------
1. **Sidecar discovery** – for every media file find its companion .json.
2. **JSON parsing** – extract ``photoTakenTime`` and ``geoData`` with
   graceful handling of malformed / missing sidecars.
3. **ExifTool write** – inject date-taken and GPS into EXIF / QuickTime
   headers via the ``pyexiftool`` wrapper.
4. **Deduplication** – compare the file's MD5 against the NAS manifest;
   skip files already present.

Public API
----------
    ProcessResult   – named tuple returned per file
    process_file(media_path, nas_manifest, exiftool_client) -> ProcessResult
    process_all(master_temp, nas_manifest, show_progress) -> list[ProcessResult]
"""

import json
import logging
import re
import queue
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

import exiftool
from tqdm import tqdm

from config import (
    MEDIA_EXTENSIONS,
    EXIF_DATE_TAGS,
    EXIF_GPS_TAGS,
    MAX_WORKERS,
)
from utils.hash_utils import file_md5

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class MediaMetadata:
    """Parsed fields from a Google Takeout .json sidecar."""
    date_taken: Optional[datetime] = None      # UTC-aware datetime
    latitude:   Optional[float]    = None
    longitude:  Optional[float]    = None
    altitude:   Optional[float]    = None
    title:      Optional[str]      = None      # original filename Google stored
    is_edited:  bool               = False     # detected from filename pattern


@dataclass
class ProcessResult:
    """Outcome of processing a single media file."""
    path:        Path
    status:      str           # "processed" | "duplicate" | "no_sidecar" | "exif_error" | "skipped"
    md5:         str  = ""
    metadata:    Optional[MediaMetadata] = None
    error:       Optional[str]           = None
    dest_hint:   Optional[Path]          = None   # suggested Year/Month path (set by organizer)


# ---------------------------------------------------------------------------
# Sidecar discovery
# ---------------------------------------------------------------------------

def _find_sidecar(media_path: Path) -> Optional[Path]:
    """
    Locate the .json sidecar for *media_path*.

    Google Takeout uses several naming conventions:
      1. ``photo.jpg.json``          (most common)
      2. ``photo.json``              (some exports)
      3. ``photo(1).jpg.json``       (duplicate counter before extension)
      4. For files whose name was truncated at 47 chars: the json keeps the
         full name while the media file is truncated – we try a prefix match.
    """
    candidates = [
        media_path.with_suffix(media_path.suffix + ".json"),    # IMG.jpg.json
        media_path.with_suffix(".json"),                         # IMG.json
    ]

    # Handle Google's duplicate counter: IMG(1).jpg → try IMG.jpg.json
    stem_no_counter = re.sub(r"\(\d+\)$", "", media_path.stem).rstrip()
    if stem_no_counter != media_path.stem:
        candidates.append(
            media_path.with_name(stem_no_counter + media_path.suffix + ".json")
        )

    for c in candidates:
        if c.is_file():
            return c

    # Fallback: prefix search in the same directory
    prefix = media_path.stem[:40]  # Google truncates at 47, use shorter prefix
    for sibling in media_path.parent.glob("*.json"):
        if sibling.stem.startswith(prefix):
            try:
                raw = sibling.read_text(encoding="utf-8", errors="replace")
                data = json.loads(raw)
                if data.get("title") == media_path.name:
                    log.debug("Prefix-matched and title-verified sidecar: %s  →  %s", media_path.name, sibling.name)
                    return sibling
            except Exception:
                pass

    return None


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------

_EDITED_RE = re.compile(r"(-edited|-效果|-效果图)(\.\w+)?$", re.IGNORECASE)


def _parse_sidecar(json_path: Path) -> Optional[MediaMetadata]:
    """
    Parse a Google Takeout JSON sidecar.  Returns ``None`` on fatal error.

    Google JSON schema (simplified):
    {
      "title": "original_name.jpg",
      "photoTakenTime": { "timestamp": "1609459200", "formatted": "..." },
      "geoData": { "latitude": 48.8566, "longitude": 2.3522, "altitude": 35.0, ... },
      "geoDataExif": { ... },   # alternative GPS source
    }
    """
    try:
        raw = json_path.read_text(encoding="utf-8", errors="replace")
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("Corrupted JSON %s: %s", json_path, e)
        return None
    except OSError as e:
        log.warning("Cannot read sidecar %s: %s", json_path, e)
        return None

    meta = MediaMetadata()

    # --- Date taken ---------------------------------------------------------
    photo_taken = data.get("photoTakenTime", {})
    ts_str = photo_taken.get("timestamp", "")
    if ts_str:
        try:
            ts = int(ts_str)
            if ts > 0:  # Google sometimes emits 0 for unknown
                meta.date_taken = datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, OSError):
            log.debug("Unparseable timestamp in %s: %r", json_path, ts_str)

    # If photoTakenTime is absent/zero, try creationTime
    if meta.date_taken is None:
        ct = data.get("creationTime", {})
        ts_str = ct.get("timestamp", "")
        if ts_str:
            try:
                ts = int(ts_str)
                if ts > 0:
                    meta.date_taken = datetime.fromtimestamp(ts, tz=timezone.utc)
            except (ValueError, OSError):
                pass

    # --- GPS ----------------------------------------------------------------
    for geo_key in ("geoDataExif", "geoData"):
        geo = data.get(geo_key, {})
        lat = geo.get("latitude", 0.0)
        lon = geo.get("longitude", 0.0)
        # Google stores 0.0 when GPS is absent
        if lat != 0.0 or lon != 0.0:
            meta.latitude  = float(lat)
            meta.longitude = float(lon)
            alt = geo.get("altitude")
            if alt is not None:
                meta.altitude = float(alt)
            break

    # --- Title / edited flag ------------------------------------------------
    meta.title = data.get("title", "")
    meta.is_edited = bool(_EDITED_RE.search(meta.title or ""))

    return meta


# ---------------------------------------------------------------------------
# ExifTool writing
# ---------------------------------------------------------------------------

def _write_exif(media_path: Path, meta: MediaMetadata, et: exiftool.ExifToolHelper) -> None:
    """
    Use pyexiftool to write date and GPS metadata in-place.
    Raises on failure so the caller can record the error.
    """
    tags: dict = {}

    # --- Date ---------------------------------------------------------------
    if meta.date_taken:
        # ExifTool wants "YYYY:MM:DD HH:MM:SS" (no timezone for DateTimeOriginal)
        dt_str = meta.date_taken.strftime("%Y:%m:%d %H:%M:%S")
        for tag in EXIF_DATE_TAGS:
            tags[tag] = dt_str

    # --- GPS ----------------------------------------------------------------
    if meta.latitude is not None and meta.longitude is not None:
        tags[EXIF_GPS_TAGS["lat"]] = abs(meta.latitude)
        tags["GPSLatitudeRef"]     = "N" if meta.latitude  >= 0 else "S"
        tags[EXIF_GPS_TAGS["lon"]] = abs(meta.longitude)
        tags["GPSLongitudeRef"]    = "E" if meta.longitude >= 0 else "W"

        if meta.altitude is not None:
            tags[EXIF_GPS_TAGS["alt"]] = abs(meta.altitude)
            tags["GPSAltitudeRef"]     = 0 if meta.altitude >= 0 else 1

    if not tags:
        log.debug("No metadata to write for %s", media_path)
        return

    et.set_tags(
        [str(media_path)],
        tags=tags,
        params=["-overwrite_original", "-ignoreMinorErrors"],
    )
    log.debug("EXIF written to %s (%d tags)", media_path.name, len(tags))


# ---------------------------------------------------------------------------
# Per-file processor
# ---------------------------------------------------------------------------

def process_file(
    media_path: Path,
    nas_manifest: Dict[int, Dict[str, Path]],
    et: exiftool.ExifToolHelper,
) -> ProcessResult:
    """
    Full pipeline for a single media file:
      1. Compute size. Check if size exists on NAS.
      2. If size in NAS manifest, compute MD5 and check for exact match.
      3. Verify NAS file still exists (lazy pruning).
      4. Find + parse sidecar.
      5. Write EXIF.
      6. Return ProcessResult.
    """
    try:
        size = media_path.stat().st_size
    except OSError:
        size = 0

    digest = ""

    # --- Deduplication ------------------------------------------------------
    if size in nas_manifest:
        digest = file_md5(media_path)
        if digest and digest in nas_manifest[size]:
            nas_path = nas_manifest[size][digest]
            if nas_path.exists() and nas_path.stat().st_size == size:
                log.debug("Duplicate (matches NAS): %s", media_path.name)
                return ProcessResult(path=media_path, status="duplicate", md5=digest)
            else:
                log.info("Duplicate found in manifest but missing on NAS, lazy pruning: %s", nas_path)
                del nas_manifest[size][digest]
                if not nas_manifest[size]:
                    del nas_manifest[size]

    # --- Sidecar discovery --------------------------------------------------
    sidecar = _find_sidecar(media_path)
    if sidecar is None:
        log.info("No sidecar found: %s", media_path.name)
        # Still proceed – the file might have embedded EXIF already
        return ProcessResult(path=media_path, status="no_sidecar", md5=digest)

    # --- Sidecar parsing ----------------------------------------------------
    meta = _parse_sidecar(sidecar)
    if meta is None:
        return ProcessResult(
            path=media_path, status="exif_error", md5=digest,
            error=f"Corrupted JSON: {sidecar}"
        )

    # --- ExifTool write -----------------------------------------------------
    try:
        _write_exif(media_path, meta, et)
    except Exception as e:
        log.warning("ExifTool failed for %s: %s", media_path, e)
        return ProcessResult(
            path=media_path, status="exif_error", md5=digest,
            metadata=meta, error=str(e)
        )

    return ProcessResult(path=media_path, status="processed", md5=digest, metadata=meta)


# ---------------------------------------------------------------------------
# Batch processor
# ---------------------------------------------------------------------------

def process_all(
    master_temp: Path,
    nas_manifest: Dict[int, Dict[str, Path]],
    show_progress: bool = True,
) -> list[ProcessResult]:
    """
    Walk *master_temp* and process every media file.

    Processing uses a ThreadPoolExecutor with concurrent pyexiftool instances.
    Returns a list of ProcessResult objects for Module 3 to consume.
    """
    media_files = [
        p for p in master_temp.rglob("*")
        if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS
    ]

    log.info("Processing %d media files from master_temp …", len(media_files))
    results: list[ProcessResult] = []

    et_pool = queue.Queue()
    for _ in range(MAX_WORKERS):
        et_pool.put(exiftool.ExifToolHelper(common_args=["-G", "-n"]))
        
    for et in list(et_pool.queue):
        et.run()

    def _worker(mf):
        et = et_pool.get()
        try:
            return process_file(mf, nas_manifest, et)
        finally:
            et_pool.put(et)

    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = [pool.submit(_worker, mf) for mf in media_files]
            with tqdm(
                total=len(futures),
                desc="Processing metadata",
                unit="file",
                disable=not show_progress,
            ) as pbar:
                for fut in as_completed(futures):
                    res = fut.result()
                    pbar.set_postfix_str(res.path.name[:40])
                    results.append(res)
                    pbar.update(1)
    finally:
        while not et_pool.empty():
            et = et_pool.get()
            et.terminate()

    # Summary
    counts = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    log.info("Metadata processing complete: %s", counts)

    return results
