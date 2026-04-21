#!/usr/bin/env python3
"""One-time macOS setup: grant the current venv's Python Bluetooth permission.

macOS requires any process that touches Bluetooth to advertise an
``NSBluetoothAlwaysUsageDescription`` string in its bundle ``Info.plist``. The
vanilla ``Python.app`` shipped by Homebrew / python.org does not declare this,
so the OS kills bleak on first scan with a TCC privacy violation (SIGABRT,
"This app has crashed because it attempted to access privacy-sensitive data
without a usage description").

This script makes a private copy of the interpreter's ``Python.app`` inside
the venv, patches its ``Info.plist``, and re-links ``.venv/bin/python`` at
the patched copy. From then on macOS prompts once and remembers the grant.

Run after ``pip install -e .`` and before the first ``bendio`` invocation:

    python scripts/mac_bluetooth_setup.py

Idempotent. Safe to re-run. Does not touch the system Python install.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import sys
from pathlib import Path

BT_USAGE = (
    "bendio uses Bluetooth Low Energy to control a paired ham radio."
)
MIC_USAGE = (
    "bendio captures microphone audio to transmit over the ham radio."
)


def find_source_app(executable: Path) -> Path:
    """Walk up from sys.executable's realpath to find a ``*.app`` bundle."""
    real = executable.resolve()
    for parent in [real, *real.parents]:
        if parent.suffix == ".app":
            return parent
    # Homebrew layout: …/Python.framework/Versions/3.x/Resources/Python.app
    for parent in real.parents:
        cand = parent / "Resources" / "Python.app"
        if cand.exists():
            return cand
    raise RuntimeError(f"Could not locate Python.app for {executable}")


def venv_root() -> Path | None:
    """Return the active venv root, or None if we're not in a venv.

    Tries three detectors in order of reliability:
      1. ``sys.prefix != sys.base_prefix`` — the canonical "am I in a venv"
         check, works regardless of symlink resolution.
      2. ``VIRTUAL_ENV`` env var — set by the venv's activate script.
      3. Walking up from ``sys.executable`` as a last resort.
    """
    if sys.prefix != sys.base_prefix:
        return Path(sys.prefix)
    venv_env = os.environ.get("VIRTUAL_ENV")
    if venv_env:
        return Path(venv_env)
    # Last resort: assume sys.executable is .venv/bin/python. This can
    # fail if a Homebrew-style install reports a resolved path instead.
    candidate = Path(sys.executable).parent.parent
    if (candidate / "pyvenv.cfg").exists():
        return candidate
    return None


BUNDLE_ID = "org.bendio.python-bt"


def patch_plist(plist_path: Path) -> bool:
    with plist_path.open("rb") as f:
        data = plistlib.load(f)
    already = (
        data.get("NSBluetoothAlwaysUsageDescription") == BT_USAGE
        and data.get("NSMicrophoneUsageDescription") == MIC_USAGE
        and data.get("CFBundleIdentifier") == BUNDLE_ID
    )
    if already:
        return False
    data["NSBluetoothAlwaysUsageDescription"] = BT_USAGE
    data["NSBluetoothPeripheralUsageDescription"] = BT_USAGE
    data["NSMicrophoneUsageDescription"] = MIC_USAGE
    # Unique bundle ID so macOS treats this as a distinct app for TCC purposes
    # and prompts fresh instead of inheriting org.python.python's state.
    data["CFBundleIdentifier"] = BUNDLE_ID
    data["CFBundleName"] = "bendio"
    data["CFBundleDisplayName"] = "bendio (Python for Bluetooth + mic)"
    with plist_path.open("wb") as f:
        plistlib.dump(data, f)
    return True


def main() -> int:
    if sys.platform != "darwin":
        print("Not macOS; nothing to do.")
        return 0

    exe = Path(sys.executable)
    venv = venv_root()
    if venv is None or not (venv / "pyvenv.cfg").exists():
        print(
            "Run this from inside an activated venv. We couldn't locate a\n"
            "venv root. Tried sys.prefix, $VIRTUAL_ENV, and walking up from\n"
            f"sys.executable ({exe}). If the venv is activated, try invoking\n"
            "this script via the venv's own python directly:\n"
            "  .venv/bin/python scripts/mac_bluetooth_setup.py"
        )
        return 2

    src_app = find_source_app(exe)
    dst_app = venv / "Python.app"

    if not dst_app.exists():
        print(f"Copying {src_app} → {dst_app}")
        shutil.copytree(src_app, dst_app, symlinks=True)
    else:
        print(f"Reusing existing {dst_app}")

    plist = dst_app / "Contents" / "Info.plist"
    changed = patch_plist(plist)
    if changed:
        print(f"Patched {plist} with NSBluetoothAlwaysUsageDescription")
    else:
        print(f"{plist} already has the usage description")

    # Re-link venv's python → patched binary
    patched_python = dst_app / "Contents" / "MacOS" / "Python"
    if not patched_python.exists():
        print(f"Patched binary missing at {patched_python}; aborting.")
        return 3

    py_short = f"python{sys.version_info.major}.{sys.version_info.minor}"
    for link_name in ("python", "python3", py_short):
        link_path = venv / "bin" / link_name
        if link_path.is_symlink() or link_path.exists():
            link_path.unlink()
        link_path.symlink_to(patched_python)
        print(f"Linked {link_path} → {patched_python}")

    # Codesign ad-hoc so the patched plist is accepted.
    import subprocess
    print("Re-signing patched Python.app ad-hoc...")
    result = subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-", str(dst_app)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print("codesign warning:", result.stderr.strip())
    else:
        print("codesign OK")

    # Clear any previously denied TCC decision for this bundle ID — both
    # Bluetooth and Microphone services, so approvals prompt fresh.
    for service in ("Bluetooth", "Microphone"):
        subprocess.run(
            ["tccutil", "reset", service, BUNDLE_ID],
            capture_output=True,
            text=True,
        )

    print("\nDone.")
    print(
        "Next BLE call should trigger a macOS permission prompt for\n"
        f"  {BUNDLE_ID}\n"
        "Approve it. If no prompt appears and the process still crashes with\n"
        "TCC SIGABRT, manually grant Bluetooth permission to whatever terminal\n"
        "or IDE you're running from:\n"
        "  System Settings → Privacy & Security → Bluetooth → [your terminal]"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
