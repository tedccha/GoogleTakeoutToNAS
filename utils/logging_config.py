"""
utils/logging_config.py - Centralised logging configuration.

Call `setup_logging(log_dir)` once at startup; every module then does:

    import logging
    log = logging.getLogger(__name__)
"""

import logging
import sys
from pathlib import Path

from config import LOG_FILE_NAME, LOG_FORMAT, LOG_DATE_FMT


def setup_logging(log_dir: Path, verbose: bool = False) -> logging.Logger:
    """
    Configure root logger with:
      - A rotating-friendly FileHandler (one plain file per run, UTF-8).
      - A StreamHandler for the console (INFO+ normally, DEBUG with --verbose).

    Returns the root logger so the caller can log immediately.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / LOG_FILE_NAME

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # capture everything; handlers filter

    fmt = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FMT)

    # --- File handler: always DEBUG level -----------------------------------
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # --- Console handler: INFO (or DEBUG with --verbose) -------------------
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    # Silence overly chatty third-party loggers
    for noisy in ("exiftool", "urllib3", "filelock"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    root.info("Logging initialised → %s", log_path)
    return root
