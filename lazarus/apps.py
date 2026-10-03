#!/usr/bin/env python3
"""Print shell lines that relaunch every open GUI app (X11 via wmctrl).

Used by termsave. Terminals and VLC are handled there; apps that already
autostart at login are skipped so they don't open twice.
"""
import glob
import os
import re
import shlex
import subprocess
import shutil

HOME = os.path.expanduser("~")
SKIP_CLASSES = {"kitty", "gnome-terminal", "nemo-desktop"}
DESKTOP_DIRS = [
    f"{HOME}/.local/share/applications",
    "/usr/share/applications",
    "/usr/local/share/applications",
    "/var/lib/flatpak/exports/share/applications",
    f"{HOME}/.local/share/flatpak/exports/share/applications",
]


def read_desktop(path):
    entry, in_main = {}, False
    try:
        with open(path, errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("["):
                    in_main = line == "[Desktop Entry]"
                elif in_main and "=" in line:
                    k, v = line.split("=", 1)
                    entry.setdefault(k, v)
    except OSError:
        pass
    return entry


def clean_exec(cmd):
    """Desktop Exec -> argv with field codes (%U, %f, ...) and dangling --url -- removed."""
    argv = [a for a in shlex.split(cmd) if not re.fullmatch(r"%[a-zA-Z]", a)]
    while argv and argv[-1] in ("--", "--url"):
        argv.pop()
    return argv


def exec_name(cmd):
    try:
        return os.path.basename(shlex.split(cmd)[0]).lower()
    except (ValueError, IndexError):
        return ""


def load_desktops():
    out = []
    for d in DESKTOP_DIRS:
        for p in glob.glob(f"{d}/*.desktop"):
            e = read_desktop(p)
            e["_id"] = os.path.basename(p)[: -len(".desktop")].lower()
            out.append(e)
    return out


def wm_class_of(wid):
    """(instance, class) from WM_CLASS; wmctrl's dotted form is ambiguous."""
    out = subprocess.run(["xprop", "-id", wid, "WM_CLASS"], capture_output=True, text=True, timeout=3).stdout
    vals = re.findall(r'"([^"]*)"', out)
    return (vals + ["", ""])[:2]


def autostart_names():
    names = set()
    for p in glob.glob(f"{HOME}/.config/autostart/*.desktop"):
        e = read_desktop(p)
        if e.get("Hidden") == "true" or e.get("X-GNOME-Autostart-enabled") == "false":
            continue
        if e.get("Exec"):
            names.add(exec_name(e["Exec"]))
    return names


def proc_argv(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return []


def proc_env(pid, key):
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            for kv in f.read().split(b"\0"):
                k, _, v = kv.decode(errors="replace").partition("=")
                if k == key:
                    return v
    except OSError:
        pass
    return None


def flatpak_id(pid):
    try:
        with open(f"/proc/{pid}/root/.flatpak-info") as f:
            m = re.search(r"^name=(.+)$", f.read(), re.M)
            return m.group(1) if m else None
    except OSError:
        return None


def resolve(pid, wm_instance, wm_class, title, desktops):
    if fid := flatpak_id(pid):
        return ["flatpak", "run", fid]
    if appimage := proc_env(pid, "APPIMAGE"):
        # The env keeps the launch-time path; the file may have been moved since.
        if not os.path.exists(appimage):
            name = os.path.basename(appimage)
            for d in ("Apps", "Applications", "AppImages", ".local/bin", "Downloads", "bin", "Desktop"):
                if os.path.exists(cand := os.path.join(HOME, d, name)):
                    return [cand]
        return [appimage]
    argv = proc_argv(pid)
    exe = os.path.basename(argv[0]).lower() if argv else ""
    if exe == "vlc":
        result = subprocess.run(
            ["dbus-send", "--session", "--print-reply", "--dest=org.mpris.MediaPlayer2.vlc",
             "/org/mpris/MediaPlayer2", "org.freedesktop.DBus.Properties.Get",
             "string:org.mpris.MediaPlayer2.Player", "string:Metadata"],
            capture_output=True, text=True, timeout=5,
        ) if shutil.which("dbus-send") else None
        match = re.search(r'"xesam:url"\s+variant\s+string "([^"]+)"', result.stdout) if result else None
        return ["vlc", match.group(1)] if match else argv
    if wm_class.lower() == "firefox" or exe.startswith("firefox"):
        return ["firefox"]  # tabs come back via Firefox's own session restore
    if wm_instance == "nemo":
        folder = HOME if title == "Home" else os.path.join(HOME, title)
        return ["nemo", folder] if os.path.isdir(folder) else ["nemo"]
    keys = {wm_instance.lower(), wm_class.lower()} - {""}
    for e in desktops:
        if e.get("Exec") and e.get("StartupWMClass", "").lower() in keys:
            return clean_exec(e["Exec"])
    for e in desktops:
        if e.get("Exec") and e["_id"] in keys:
            return clean_exec(e["Exec"])
    for e in desktops:
        if e.get("Exec") and exec_name(e["Exec"]) in (keys | {exe}) - {""}:
            return clean_exec(e["Exec"])
    if len(argv) == 1 and " " in argv[0] and not os.path.exists(argv[0]):
        argv = shlex.split(argv[0])  # electron rewrites its cmdline into one string
    return argv[:1]


def capture(include_autostart=False):
    out = subprocess.run(["wmctrl", "-lpx"], capture_output=True, text=True, check=True, timeout=10).stdout
    desktops, autostart = load_desktops(), autostart_names()
    seen_pids, seen_cmds = set(), set()
    for line in out.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 5:
            continue
        wid, pid = parts[0], parts[2]
        title = parts[5] if len(parts) > 5 else ""
        wm_instance, wm_class = wm_class_of(wid)
        if pid == "0" or wm_instance.lower() in SKIP_CLASSES or wm_class.lower() in SKIP_CLASSES \
                or "gnome-terminal" in (wm_instance + wm_class).lower():
            continue
        if pid in seen_pids and wm_instance != "nemo":
            continue
        seen_pids.add(pid)
        exe = os.path.basename((proc_argv(pid) or [""])[0]).lower()
        if not include_autostart and (exe in autostart or wm_instance.lower() in autostart):
            continue
        argv = resolve(pid, wm_instance, wm_class, title, desktops)
        cmd = shlex.join(argv)
        if not argv or cmd in seen_cmds:
            continue
        seen_cmds.add(cmd)
        yield {"kind": "app", "label": f"{wm_class or wm_instance}: {title[:60]}",
               "argv": argv, "cwd": HOME, "identity": (wm_class or wm_instance).lower() or exe}


if __name__ == "__main__":
    for item in capture():
        print(shlex.join(item["argv"]))
