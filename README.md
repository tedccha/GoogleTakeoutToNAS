# GoogleTakeoutToLongviewstorage

Automate the complex, tedious process of migrating a massive Google Photos library (exported via Google Takeout) to a Synology NAS (or any standard local/network storage). 

Google Takeout is notoriously messy: it strips essential EXIF metadata (dates, GPS) from your media, strands it in separate `.json` sidecar files, arbitrarily splits files into 50GB `.zip` folders, and leaves you to figure out deduplication. 

This tool intelligently reconstructs your exact library from the ground up, fixing metadata, deduplicating exactly, and exporting to a clean, highly browsable folder schema.

---

## Key Features

- **Sidecar Re-Injection**: Parses Google's disconnected `.json` sidecars to discover the original `"photoTakenTime"` and GPS coordinates, injecting them strictly and safely back into the EXIF/QuickTime headers of your `.jpg`, `.heic`, and `.mp4` files using a multi-threaded ExifTool pool. 
- **Smart Deduplication**: Aggressively scans your NAS destination and builds a highly efficient `{size: hash}` manifest chunk. If you re-run an ingestion later, it knows *exactly* which files have already been pushed, saving you gigabytes of redundant transfers.
- **Auto-Unzipping & Folder Merging**: Safely and recursively extracts every `.zip` archive you get from Takeout, brilliantly stitching together collision folders without skipping healthy files.
- **Pristine Library Structure**: Automatically evaluates the true UTC dates and moves your media to an elegant, tidy `{NAS_ROOT}/YYYY/MM/final_image.jpg` hierarchy.
- **NAS SMB Safe-Copying**: Bypasses classic macOS `shutil.copy2` extended attribute bugs (Errno 22) during network transfers, manually preserving `mtime` strictly via safe `os.utime()` injections.
- **rclone Support**: If your Takeout exports were sent directly to Google Drive, this script can use `rclone` to automatically stream the download chunks locally.

---

## Getting Started

### 1. Installation

Clone the repository and let the friendly onboarding wizard configure your environment!

```bash
git clone https://github.com/yourusername/GoogleTakeoutToLongviewstorage.git
cd GoogleTakeoutToLongviewstorage
python3 setup.py
```

The interactive `setup.py` wizard will completely automate your deployment:
1. **Dependency Check**: It will verify you have ExifTool installed (and tell you exactly how to get it if you don't).
2. **Package Install**: It will automatically install the required Python wrappers (`pyexiftool`, `tqdm`).
3. **Environment Interview**: It will cleanly prompt you for your Local and NAS filesystem paths.
4. **Pre-Flight Check**: It will actively verify read/write connections to your drives before you start.
5. **Launcher Generation**: It will generate a localized `go.sh` (Mac/Linux) or `go.bat` (Windows) file that completely abstracts away the complex command-line arguments.

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
If ExifTool spits out errors on certain files (like older `.avi` files, or odd `.png` screenshots), don't panic! Some files structurally cannot accept EXIF tags. The library Organizer logic has been heavily fortified to safely catch these errors, log them, and still migrate the files cleanly to the NAS using sidecar metadata dates instead!

**SMB Drops / Notebook Sleeping**
To circumvent your Mac sleeping and dropping the NAS mount mid-transfer, either run using `caffeinate -i ./go.sh`, or use a tool like Amphetamine to keep your Mac awake!

**macOS permission errors on `/Volumes/`**
Go to **System Settings → Privacy & Security → Full Disk Access** and explicitly add your Terminal desktop app to it so it can traverse networked filesystems freely.
