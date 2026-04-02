"""
utils/hash_utils.py - MD5 hashing utilities for deduplication.

Public API
----------
file_md5(path)        → hex string
build_manifest(root)  → dict[int, dict[str, Path]]   # size → hash → absolute path
save_manifest(manifest, path)
load_manifest(path)   → dict[int, dict[str, Path]]
"""

import hashlib
import json
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict

from tqdm import tqdm

from config import HASH_CHUNK_SIZE, HASH_ALGORITHM, MEDIA_EXTENSIONS, MAX_WORKERS

log = logging.getLogger(__name__)


def file_md5(path: Path) -> str:
    """Return the lower-case hex MD5 digest of a file, reading in chunks."""
    h = hashlib.new(HASH_ALGORITHM)
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(HASH_CHUNK_SIZE), b""):
                h.update(chunk)
    except OSError as e:
        log.warning("Cannot hash %s: %s", path, e)
        return ""
    return h.hexdigest()


def _hash_one(path: Path):
    """Worker function: return (size, hash, path) tuple."""
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return size, file_md5(path), path


def build_manifest(root: Path, show_progress: bool = True) -> Dict[int, Dict[str, Path]]:
    """
    Recursively scan *root* for all media files and build a
    ``{size: {md5_hash: absolute_path}}`` mapping.

    Duplicate hashes on the NAS keep the *last* path seen (arbitrary but
    deterministic within a single run).
    """
    media_files = [
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS
    ]

    log.info("Scanning %d existing media files in %s …", len(media_files), root)

    manifest: Dict[int, Dict[str, Path]] = {}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_hash_one, p): p for p in media_files}
        it = tqdm(
            as_completed(futures),
            total=len(futures),
            desc="Building NAS manifest",
            unit="file",
            leave=False,
            disable=not show_progress,
        )
        for fut in it:
            size, digest, path = fut.result()
            if digest:
                if size not in manifest:
                    manifest[size] = {}
                if digest in manifest[size]:
                    log.debug("Duplicate on NAS (keeping first): %s", path)
                else:
                    manifest[size][digest] = path

    total = sum(len(d) for d in manifest.values())
    log.info("Manifest complete: %d unique files indexed.", total)
    return manifest


def save_manifest(manifest: Dict[int, Dict[str, Path]], dest: Path) -> None:
    """Persist manifest as JSON so subsequent runs can reuse it."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    serialisable = {
        str(size): {k: str(v) for k, v in hashes.items()}
        for size, hashes in manifest.items()
    }
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(serialisable, f, indent=2)
    total = sum(len(d) for d in manifest.values())
    log.debug("Manifest saved → %s (%d entries)", dest, total)


def load_manifest(src: Path) -> Dict[int, Dict[str, Path]]:
    """Load a previously saved manifest JSON. Returns empty dict on failure."""
    if not src.is_file():
        return {}
    try:
        with open(src, encoding="utf-8") as f:
            raw = json.load(f)
        return {
            int(size): {k: Path(v) for k, v in hashes.items()}
            for size, hashes in raw.items()
        }
    except Exception as e:
        log.warning("Could not load manifest %s: %s – rebuilding.", src, e)
        return {}
