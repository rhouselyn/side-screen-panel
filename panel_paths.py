"""Read-only app files and per-user writable state."""

import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("PANEL_DATA_DIR") or Path(os.environ["LOCALAPPDATA"]) / "SideScreenPanel")
STOP_FILE = DATA_DIR / "watchdog-stop"


def prepare_data():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if "PANEL_DATA_DIR" in os.environ:
        return
    # Migrate the original checkout without overwriting an existing profile.
    for name in ("brightness.json", "codex-exe-path.json", "deepseek.json",
                 "quota-day.json", "seen-sessions.json", "traffic-state.json", "tun-state.json"):
        destination = DATA_DIR / name
        if destination.exists() or not (ROOT / name).is_file():
            continue
        temp = None
        try:
            contents = (ROOT / name).read_bytes()
            with tempfile.NamedTemporaryFile(dir=DATA_DIR, suffix=".tmp", delete=False) as output:
                temp = Path(output.name)
                output.write(contents)
            temp.rename(destination)  # Windows refuses to replace an existing destination.
        except (FileNotFoundError, FileExistsError):
            pass
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
