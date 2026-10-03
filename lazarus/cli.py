import argparse
import os
from pathlib import Path
import shlex
import subprocess
import sys

from . import core


def preferences():
    path = core.state_dir() / "preferences.json"
    if not path.exists():
        return {"restore": "latest"}
    import json
    return json.loads(path.read_text())


def set_restore_target(target):
    import json
    if target is not None:
        core.snapshot_path(target)
    core.atomic_write(core.state_dir() / "preferences.json", json.dumps({"restore": target}) + "\n")


def autostart_path():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart/lazarus.desktop"


def set_autostart(enabled):
    path = autostart_path()
    if not enabled:
        path.unlink(missing_ok=True)
        return
    launcher = Path(__file__).resolve().parent.parent / "bin/lazarus"
    if launcher.exists():
        argv = ["/usr/bin/python3", str(launcher), "tray", "--restore"]
    else:
        argv = [sys.executable, "-m", "lazarus", "tray", "--restore"]
    # Desktop Exec uses double-quoted arguments, not shell quoting.
    def quote(value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$").replace("%", "%%") + '"'
    core.atomic_write(path, "[Desktop Entry]\nType=Application\nName=Lazarus\n"
                      "Comment=Save and restore your desktop session\n"
                      "Icon=" + str(Path(__file__).resolve().parent / "assets/lazarus.png") + "\n"
                      "Exec=" + " ".join(quote(arg) for arg in argv) + "\n"
                      "Terminal=false\nX-GNOME-Autostart-enabled=true\n", 0o644)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Save and restore your Linux/X11 desktop")
    sub = parser.add_subparsers(dest="action")
    for action in ("save", "show", "restore"):
        command = sub.add_parser(action)
        command.add_argument("name", nargs="?", default="latest")
        if action == "restore":
            command.add_argument("--dry-run", action="store_true")
            command.add_argument("--force", action="store_true", help="Also launch items already running")
            command.add_argument("--only", nargs="+", help="Restore only these item IDs")
    sub.add_parser("list")
    sub.add_parser("doctor")
    profile = sub.add_parser("profile")
    profile.add_argument("operation", choices=("save", "select", "previous", "off"))
    profile.add_argument("name", nargs="?")
    tray = sub.add_parser("tray")
    tray.add_argument("--restore", action="store_true", help="Restore latest after login")
    auto = sub.add_parser("autostart")
    auto.add_argument("setting", choices=("enable", "disable", "status"))
    args = parser.parse_args(argv)
    try:
        if args.action in (None, "tray"):
            from .tray import run
            return run(getattr(args, "restore", False))
        if args.action == "save":
            data = core.capture_save(args.name)
            print(f"Saved {len(data['items'])} items to {core.snapshot_path(args.name)}")
        elif args.action == "restore":
            return core.restore(args.name, args.dry_run, args.force, args.only)
        elif args.action == "show":
            for item in core.load(args.name)["items"]:
                print(f"{item['id']:>3} {item['kind']:8} {item['cwd']}\n    {shlex.join(item['argv']) or item.get('shell', '')}")
        elif args.action == "profile":
            if args.operation in {"save", "select"}:
                if not args.name or args.name == "latest":
                    raise ValueError("Give the profile a name other than 'latest'")
                if args.operation == "save":
                    core.capture_save(args.name)
                else:
                    core.load(args.name)
                    set_restore_target(args.name)
            else:
                set_restore_target("latest" if args.operation == "previous" else None)
        elif args.action == "list":
            for path in sorted(core.state_dir().glob("*.json")):
                if path.name == "preferences.json":
                    continue
                try:
                    data = core.load(path.stem)
                    print(f"{path.stem:20} {data.get('created', '?')}  {len(data['items'])} items")
                except (ValueError, OSError) as error:
                    print(f"{path.stem}: {error}")
        elif args.action == "doctor":
            core.check_desktop()
            print("X11 capture dependencies available")
            print(f"Snapshots: {core.state_dir()}")
            print(f"Login startup: {'enabled' if autostart_path().exists() else 'disabled'}")
        elif args.action == "autostart":
            if args.setting != "status":
                set_autostart(args.setting == "enable")
            print("Login startup " + ("enabled" if autostart_path().exists() else "disabled"))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError, ImportError) as error:
        print(f"lazarus: {error}", file=sys.stderr)
        return 1
