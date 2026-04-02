# GoogleTakeoutToLongviewstorage

Automate the complex, tedious process of migrating a massive Google Photos library (exported via Google Takeout) to a Synology NAS (or any standard local/network storage). 

Google Takeout is notoriously messy: it strips essential EXIF metadata (dates, GPS) from your media, strands it in separate `.json` sidecar files, arbitrarily splits files into 50GB `.zip` folders, and leaves you to figure out deduplication. 

This tool intelligently reconstructs your exact library from the ground up, fixing metadata, deduplicating exactly, and exporting to a clean, highly browsable folder schema.

---

## Key Features

- **Sidecar Re-Injection**: Parses Google's disconnected `.json` sidecars to discover the original `"photoTakenTime"` and GPS coordinates, injecting them strictly and safely back into the EXIF/QuickTime headers of your `.jpg`, `.heic`, and `.mp4` files using a multi-threaded ExifTool pool. 
- **Smart Deduplication**: Aggressively scans your NAS destination and builds a highly efficient `{size: hash}` manifest chunk. If you re-run an ingestion later, it knows *exactly* which files have already been pushed, saving you gigabytes of redundant transfers.
- **Auto-Unzipping & Folder Merging**: Safely and recursively extracts every `.zip` archive you get from Takeout, brilliantly stitching together collision folders without skipping healthy files.
- **Curated Filtering**: Uses Regex sanitization to handle Google's duplicate file tags (e.g., `(1).jpg`) and intelligently prefers `-edited` image variants over raw originals so your final library represents your active, edited Google Photos space.
- **Pristine Library Structure**: Automatically evaluates the true UTC dates and moves your media to an elegant, tidy `{NAS_ROOT}/YYYY/MM/final_image.jpg` hierarchy.
- **rclone Support**: If your Takeout exports were sent directly to Google Drive, this script can use `rclone` to automatically stream the download chunks locally.

---

## Getting Started

### Prerequisites

You need a few system-level dependencies for the script to analyze and inject headers natively:

1. **Python 3.8+**
2. **ExifTool**: Ensure this is installed on your system PATH.
   - **macOS**: `brew install exiftool`
   - **Linux**: `sudo apt install libimage-exiftool-perl`
3. **rclone** *(Optional, but required if you want to pull zips directly from a remote like Google Drive)*:
   - **macOS**: `brew install rclone`
   - Then run `rclone config` to link your remote.

### Installation

Clone the repository and install the Python wrappers required:

```bash
git clone https://github.com/yourusername/GoogleTakeoutToLongviewstorage.git
cd GoogleTakeoutToLongviewstorage
pip install -r requirements.txt
```

---

## How it Works / Usage

The pipeline executes through a rigid, safe sequence: **Ingestion -> Manifest -> Metadata Processing -> Organization -> Reporter**. 

It builds a temporary staging space (`master_temp`) where all unzipping and staging occurs before issuing an atomic `shutil.copy2` to your NAS. Your existing NAS files are never unexpectedly overwritten.

### 1. Local Downloads (Standard Usage)
If you've manually downloaded the Google Takeout `.zip` files to your computer, point the script at that folder:

```bash
python main.py \
    --source ~/Downloads/Takeout \
    --work-dir ~/Desktop/takeout_staging \
    --nas /Volumes/photo \
    --rebuild-manifest
```
*(Note: `--rebuild-manifest` forces the system to scan your NAS folder completely the first time. For subsequent runs weeks later, you can drop this flag so it utilizes the fast, cached index).*

### 2. Auto-Pull from Google Drive via rclone
If you routed your Takeout dumps directly to Google Drive, skip downloading manually.

```bash
python main.py \
    --rclone-remote "gdrive:Takeout" \
    --work-dir ~/Desktop/takeout_staging \
    --nas /Volumes/photo
```

### 3. Dry-Run (Test Execution)
Evaluate how many files are new vs duplicates, but don't transfer anything or touch your NAS.

```bash
python main.py \
    --source ~/Downloads/Takeout \
    --work-dir ~/Desktop/takeout_staging \
    --nas /Volumes/photo \
    --dry-run
```

---

## Configuration Details

You can modify internal logic via the lightweight `config.py` file:
- **`MAX_WORKERS`**: Defaults to 4. Controls how many threads are actively writing EXIF tags. If you have an M-series Mac or robust Linux desktop, you can raise this value to heavily speed up injection. 
- **`MEDIA_EXTENSIONS`**: Contains exactly what formats are ingested (`.jpeg`, `.dng`, `.heic`, `.mkv`, etc). 
- **`EDITED_SUFFIXES`**: Custom suffix tags that flag a photo as an edited priority (defaults to `["-edited", "-效果图"]`).

## Important Notes on macOS Paths

If your NAS is mapped natively via macOS SMB/AFP, it will usually sit at `/Volumes/[DriveName]`. Ensure you grant **Full Disk Access** to your Terminal application (System Settings -> Privacy & Security -> Full Disk Access) so the script can smoothly read file sizes and orchestrate folder placements on the remote drive!
