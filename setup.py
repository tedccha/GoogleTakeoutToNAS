#!/usr/bin/env python3
"""
setup.py - GoogleTakeoutToNAS Onboarding Wizard
------------------------------------------------------------
A setup wizard to help new users:
1. Verify ExifTool is installed.
2. Ensure python dependencies are installed.
3. Prompt the user for their exact paths.
4. Verify connections to the Target storage and Source directories.
5. Auto-generate the correct execution scripts (go.sh / go.bat).
"""

import os
import sys
import shutil
import platform
import subprocess
from pathlib import Path


def _print_header(msg: str):
    print(f"\n{'-' * 60}")
    print(f" {msg}")
    print(f"{'-' * 60}")


def check_exiftool():
    """Verify exiftool is installed on the system."""
    _print_header("1. Checking System Requirements")
    
    if shutil.which('exiftool') is None:
        print("[FAIL] ExifTool is NOT installed or not on your system PATH.")
        print("This tool fundamentally requires ExifTool to inject metadata.\n")
        
        system = platform.system()
        if system == "Darwin":
            print("To install on macOS:")
            print("  brew install exiftool")
        elif system == "Windows":
            print("To install on Windows:")
            print("  Point your browser to: https://exiftool.org/")
            print("  Download the Windows executable, unzip it, rename 'exiftool(-k).exe' to 'exiftool.exe'.")
            print("  Place it in a folder (like C:\\Windows) or add it to your system PATH.")
        elif system == "Linux":
            print("To install on Linux:")
            print("  sudo apt install libimage-exiftool-perl")
            
        print("\nPlease install ExifTool and run python setup.py again.")
        sys.exit(1)
    
    print("[SUCCESS] ExifTool is installed and ready.")


def check_dependencies():
    """Verify and automatically install pip requirements."""
    _print_header("2. Checking Python Dependencies")
    
    missing_deps = False
    try:
        import tqdm
        import exiftool
    except ImportError:
        missing_deps = True
        
    if missing_deps:
        print("Missing required python packages. Installing via pip...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])
            print("[SUCCESS] Dependencies installed.")
        except subprocess.CalledProcessError:
            print("[FAIL] Could not automatically install dependencies.")
            print("Please run manually: pip install -r requirements.txt")
            sys.exit(1)
    else:
        print("[SUCCESS] All Python requirements are met.")


def get_paths() -> tuple[Path, Path, Path]:
    """Interview the user to get their paths."""
    _print_header("3. Setup Transfer Paths")
    print("Press [Enter] to accept the defaults, or type in a new path.\n")
    
    system = platform.system()
    home = Path.home()
    
    # Defaults
    if system == "Windows":
        def_src  = home / "Downloads" / "GoogleTakeout"
        def_work = home / "Desktop" / "takeout_work"
        def_nas  = Path("Z:/photo")
    else:
        def_src  = home / "Downloads" / "GoogleTakeout"
        def_work = home / "Desktop" / "takeout_work"
        def_nas  = Path("/Volumes/photo")
        
    source_input = input(f"Source Directory (Google Takeout Zips) [{def_src}]: ").strip()
    work_input   = input(f"Temporary Working Directory [{def_work}]: ").strip()
    nas_input    = input(f"Target Destination Volume [{def_nas}]: ").strip()
    
    src  = Path(source_input).expanduser().resolve() if source_input else def_src
    work = Path(work_input).expanduser().resolve() if work_input else def_work
    nas  = Path(nas_input).expanduser().resolve() if nas_input else def_nas
    
    return src, work, nas


def pre_flight_check(src: Path, work: Path, nas: Path):
    """Actively test the paths before proceeding."""
    _print_header("4. Pre-Flight Environment Checks")
    errors = False
    
    print("Testing Source Directory...")
    if src.exists() and src.is_dir():
        print("  [✓] Source directory exists.")
    else:
        print(f"  [X] Source directory missing: {src}")
        errors = True
        
    print("Testing Temp Working Directory...")
    work.mkdir(parents=True, exist_ok=True)
    try:
        test_file = work / ".write_test"
        test_file.touch()
        test_file.unlink()
        print("  [✓] Work directory is writable.")
    except Exception as e:
        print(f"  [X] Cannot write to Work directory {work}: {e}")
        errors = True
        
    print("Testing Target Storage Connection...")
    if nas.exists() and nas.is_dir():
        try:
            test_file = nas / ".write_test"
            test_file.touch()
            test_file.unlink()
            print("  [✓] Target storage is read/write accessible.")
        except Exception as e:
            print(f"  [X] Target storage exists but cannot be written to {nas}: {e}")
            errors = True
    else:
        print(f"  [X] Target mount missing or disconnected: {nas}")
        print("      Check your file manager to ensure the drive map is mounted.")
        errors = True
        
    if errors:
        print("\n[!] Pre-Flight checks failed. Please correct the errors above and run setup.py again.")
        sys.exit(1)
        

def generate_runner(src: Path, work: Path, nas: Path):
    """Generate the user-friendly execute script."""
    _print_header("5. Generating Launcher Script")
    
    system = platform.system()
    
    if system == "Windows":
        script_name = "go.bat"
        content = f"""@echo off
echo ==============================================
echo  Google Takeout Migration - LIVE RUN
echo ==============================================

python main.py --source "{src}" --work-dir "{work}" --nas "{nas}"
pause
"""
    else:
        script_name = "go.sh"
        content = f"""#!/usr/bin/env bash
echo "=============================================="
echo " Google Takeout Migration - LIVE RUN"
echo "=============================================="

python3 main.py --source "{src}" --work-dir "{work}" --nas "{nas}" "$@"
"""

    with open(script_name, "w", encoding="utf-8") as f:
        f.write(content)
        
    if system != "Windows":
        # Make executable
        os.chmod(script_name, 0o755)
        
    print(f"[SUCCESS] Created local runner `{script_name}`.")
    print("\n" + "=" * 60)
    print(" SETUP COMPLETE")
    print("=" * 60)
    print(f"\nWhenever you are ready to begin transferring, run:")
    if system == "Windows":
        print(f"   {script_name}")
    else:
        print(f"   ./{script_name}")
    print("\nNote: Add '--dry-run' after the command if you want to test safely first.")
    print("\nHappy migrating!")


def main():
    check_exiftool()
    check_dependencies()
    src, work, nas = get_paths()
    pre_flight_check(src, work, nas)
    generate_runner(src, work, nas)

if __name__ == "__main__":
    main()
