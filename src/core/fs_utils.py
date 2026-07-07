from __future__ import annotations

from pathlib import Path
import sys


def _is_network_filesystem_path(path: Path) -> bool:
    if sys.platform != "win32":
        return False
    raw_path = str(path)
    if raw_path.startswith("\\\\"):
        return True
    drive = path.drive.upper()
    if not drive:
        return False
    try:
        import ctypes

        drive_type = ctypes.windll.kernel32.GetDriveTypeW(f"{drive}\\")
    except Exception:
        return False
    return int(drive_type) == 4
