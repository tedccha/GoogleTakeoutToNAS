"""
config.py - Central configuration constants for GoogleTakeoutToLongviewstorage.

All tuneable parameters live here so nothing is magic-numbered throughout the codebase.
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# Supported media extensions
# ---------------------------------------------------------------------------
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp", ".dng", ".raw"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp", ".mts", ".m2ts", ".wmv", ".flv"}
MEDIA_EXTENSIONS  = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

# ---------------------------------------------------------------------------
# Filename patterns to strip / sanitize
# ---------------------------------------------------------------------------
# Suffixes that Google appends to edited copies, e.g. "IMG_1234-edited.jpg"
EDITED_SUFFIXES = ["-edited", "-效果", "-效果图"]

# Duplicate-counter patterns: "(1)", " (2)", etc.
DUPLICATE_PATTERNS = [r"\s*\(\d+\)$"]

# ---------------------------------------------------------------------------
# Directory / file naming
# ---------------------------------------------------------------------------
INCOMING_DIR_NAME   = "incoming"       # rclone pull destination
MASTER_TEMP_DIR_NAME = "master_temp"   # unified unpacked tree
HASH_MANIFEST_FILE   = ".nas_manifest.json"

# ---------------------------------------------------------------------------
# ExifTool tag mapping
# ---------------------------------------------------------------------------
# Google Takeout JSON key  →  list of EXIF tags to write (first succeeds)
EXIF_DATE_TAGS = [
    "DateTimeOriginal",
    "CreateDate",
    "TrackCreateDate",
    "MediaCreateDate",
    "QuickTime:CreateDate",
]

EXIF_GPS_TAGS = {
    "lat":  "GPSLatitude",
    "lon":  "GPSLongitude",
    "alt":  "GPSAltitude",
}

# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------
HASH_ALGORITHM   = "md5"
HASH_CHUNK_SIZE  = 65_536   # 64 KiB chunks for streaming hash

# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
MAX_WORKERS = 4   # threads for parallel hash / metadata work

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_FILE_NAME = "takeout_migration.log"
LOG_FORMAT    = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
LOG_DATE_FMT  = "%Y-%m-%d %H:%M:%S"
