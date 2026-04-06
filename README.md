# GoogleTakeoutToNAS

Automate the process of migrating a large Google Photos library (exported via Google Takeout) to a NAS (e.g., Synology, QNAP, TrueNAS), local drive, or other network storage.

Google Takeout exports can be messy: it strips essential EXIF metadata (dates, GPS) from your media, separates it into `.json` sidecar files, arbitrarily splits files into 50GB `.zip` folders, and leaves you to handle deduplication.

This tool reconstructs your library by fixing metadata, handling deduplication, and exporting to a clean, browsable folder structure.

---

## Key Features

- **Sidecar Re-Injection**: Parses disconnected `.json` sidecars to discover original `"photoTakenTime"` and GPS coordinates, injecting them safely back into the EXIF/QuickTime headers of `.jpg`, `.heic`, and `.mp4` files using a multi-threaded ExifTool pool.
- **Smart Deduplication**: Scans your target destination and builds an efficient `{size: hash}` manifest. When re-running an ingestion, it knows which files have already been transferred to prevent redundant copies.
- **Auto-Unzipping & Folder Merging**: Recursively extracts `.zip` archives from Takeout, safely stitching together collision folders without skipping healthy files.
- **Clean Library Structure**: Evaluates true UTC dates and moves your media to an organized `{NAS_ROOT}/YYYY/MM/final_image.jpg` hierarchy.
- **NAS SMB Safe-Copying**: Bypasses classic macOS `shutil.copy2` extended attribute bugs (Errno 22) during network transfers, manually preserving `mtime` strictly via safe `os.utime()` injections.
- **rclone Support**: If your Takeout exports were sent directly to Google Drive, this script can use `rclone` to automatically stream the download chunks locally.

---

## Getting Started

### 1. Prerequisites

Before installing the script, ensure you have the following ready:
1. **Google Takeout Files**: Request and download your Google Photos export from [Google Takeout](https://takeout.google.com/) to a local folder on your computer (e.g., `~/Downloads/GoogleTakeout`).
2. **Target Storage**: Make sure your NAS or external storage is mounted and accessible from your machine (e.g., `/Volumes/photo`).
3. **ExifTool**: Ensure [ExifTool](https://exiftool.org/) is installed on your system. *(If you don't have it installed yet, don't worry—the `setup.py` wizard will explicitly provide the correct terminal command or download link to install it based on your specific operating system).*

### 2. Installation

Clone the repository and run the setup wizard to configure your environment:

```bash
git clone https://github.com/yourusername/GoogleTakeoutToNAS.git
cd GoogleTakeoutToNAS
python3 setup.py
```

The `setup.py` wizard will help you configure your deployment:
1. **Dependency Check**: Verifies ExifTool is available.
2. **Package Install**: Installs required Python wrappers (`pyexiftool`, `tqdm`).
3. **Environment setup**: Prompts you for your Local and NAS filesystem paths.
4. **Pre-Flight Check**: Verifies read/write connections to your paths.
5. **Launcher Generation**: Generates a local `go.sh` (Mac/Linux) or `go.bat` (Windows) wrapper script that abstracts away the command-line arguments.

*(Note: If you plan to pull archives using `rclone` instead of local downloads, ensure it is installed on your OS as well).*

---

## 2. Running your Migration

If you passed the `setup.py` wizard successfully, all you have to do is run your personalized helper file.

*(Pro-Tip for macOS users: Long network transfers drop if your laptop falls asleep. Prefix your command with `caffeinate -i` to force the screen to stay awake).*

```bash
caffeinate -i ./go.sh
```
*(Windows users should just run `go.bat`)*

### Resuming after an interruption
If the migration is interrupted (crash, laptop shuts, SMB timeout, etc.), **don't start over**. Resume exactly from where it left off! 
Just edit your `go.sh` script or run the raw terminal command with these flags:

```bash
caffeinate -i python3 main.py \
  --source ~/Downloads/GoogleTakeout \
  --work-dir ~/Desktop/takeout_work \
  --nas /Volumes/photo \
  --skip-ingest \
  --rebuild-manifest
```
- `--skip-ingest` safely tells it not to re-unzip your huge folders since `master_temp` still holds them.
- `--rebuild-manifest` re-scans the NAS so it acknowledges already-copied files as duplicates and safely ignores them!

---

## CLI Reference & Configuration

If you want to manually run the engine without the `go` scripts, this is available:

```bash
python3 main.py --source ~/Downloads/Takeout --work-dir ~/Desktop/temp --nas /Volumes/photo
```

| Flag | Description |
|---|---|
| `--source DIR` | Local folder containing Takeout `.zip` files |
| `--rclone-remote REMOTE:PATH` | Pull from Google Drive via rclone instead (e.g. `gdrive:Takeout`) |
| `--work-dir DIR` | Temporary scratch space for unzipping (required) |
| `--nas DIR` | NAS destination root, e.g. `/Volumes/photo` (required) |
| `--dry-run` | Simulate everything without copying files to NAS |
| `--skip-ingest` | Skip unzipping — use existing staging folder |
| `--skip-metadata` | Skip ExifTool EXIF injection |
| `--rebuild-manifest` | Force full re-scan of NAS (use when resuming) |
| `--skip-manifest` | Disable deduplication entirely (not recommended) |

### `config.py` Properties
You can natively adjust the behavior using `config.py`. 
- **`MAX_WORKERS`**: Defaults to 4. Tune this higher to heavily speed up the multi-threaded ExifTool injection if you have a powerful multicore CPU.
- **`EDITED_SUFFIXES`**: Custom tags (like `["-edited"]`) that instruct the pipeline to favor edited files over raw original duplicates.

---

## Troubleshooting

**`execute returned a non-zero exit status: 1`**
If ExifTool reports errors on certain files (like older `.avi` files, or some `.png` formats), the script will catch the error, log it, and still migrate the files to the target using sidecar metadata dates.

**SMB Drops / Notebook Sleeping**
To circumvent your Mac sleeping and dropping the NAS mount mid-transfer, either run using `caffeinate -i ./go.sh`, or use a tool like Amphetamine to keep your Mac awake!

**macOS permission errors on `/Volumes/`**
Go to **System Settings → Privacy & Security → Full Disk Access** and explicitly add your Terminal desktop app to it so it can traverse networked filesystems freely.

---

## Workspace & Receipts

When you run the script, you specify a `--work-dir` (e.g. `~/Desktop/takeout_work`) to act as a temporary playground. 

**Auto-Cleaning Workspace**: Once your extraction is entirely migrated to your target storage, the script verifies your remaining local duplicates, unlinks them, and removes the empty `takeout_work` folder to clean up local space.

**Run Reports**: Every time you run the engine, it will generate a summary receipt of everything it processed, deduplicated, and analyzed (including timeline gaps). These are saved in your codebase folder under `TakeouttoNAS/`.
