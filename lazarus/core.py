"""Capture, persist, and restore structured desktop snapshots."""
import collections
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile

from . import apps

SHELLS = {"zsh", "bash", "fish", "sh"}


def state_dir():
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "lazarus"


def snapshot_path(name):
    if name == "preferences":
        raise ValueError("The name 'preferences' is reserved")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", name):
        raise ValueError("Snapshot names must be 1–80 letters, digits, dots, underscores or hyphens, starting with a letter or digit")
    return state_dir() / f"{name}.json"


def atomic_write(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".lazarus-")
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def process_table():
    result = subprocess.run(["ps", "-u", str(os.getuid()), "-o",
                             "pid=,ppid=,tty=,stat=,comm=", "--sort=pid"],
                            capture_output=True, text=True, check=True)
    table = {}
    for line in result.stdout.splitlines():
        fields = line.split(None, 4)
        if len(fields) == 5:
            pid, parent, tty, stat, comm = fields
            table[int(pid)] = {"parent": int(parent), "tty": tty, "stat": stat, "comm": comm}
    return table


def resume(argv):
    # Match the executable, including Node's script argument, never arbitrary prompts.
    names = [Path(arg).name for arg in argv[:2]] if argv else []
    if any(name in {"claude", "claude.js"} for name in names):
        for flag in ("--resume", "-r"):
            if flag in argv:
                index = argv.index(flag) + 1
                if index < len(argv) and not argv[index].startswith("-"):
                    return ["claude", "--resume", argv[index]]
        return ["claude", "--continue"]
    if any(name in {"codex", "codex.js"} for name in names):
        if "resume" in argv:
            index = argv.index("resume") + 1
            if index < len(argv) and not argv[index].startswith("-"):
                return ["codex", "resume", argv[index]]
        return ["codex", "resume", "--last"]
    return argv


def terminals():
    table = process_table()
    # Do not snapshot Lazarus (or its invoking CLI agent) as foreground work.
    ancestors, current = set(), os.getpid()
    while current in table and current not in ancestors:
        ancestors.add(current)
        current = table[current]["parent"]
    for pid, proc in table.items():
        parent = table.get(proc["parent"], {}).get("comm", "")
        if proc["comm"] not in SHELLS or proc["tty"] == "?":
            continue
        if parent != "kitty" and not parent.startswith("gnome-terminal"):
            continue
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            continue
        foreground = next((p for p, info in table.items()
                           if p != pid and info["tty"] == proc["tty"] and "+" in info["stat"]), None)
        argv = apps.proc_argv(foreground) if foreground else []
        if foreground in ancestors and any("lazarus" in arg for arg in argv):
            argv = []
        shell = (apps.proc_argv(pid) or [proc["comm"]])[0].lstrip("-")
        command = resume(argv)
        yield {"kind": "terminal", "label": f"{Path(cwd).name}: {shlex.join(command) or shell}",
               "cwd": cwd, "argv": command, "shell": shell,
               "emulator": "kitty" if parent == "kitty" else "gnome-terminal"}


def check_desktop():
    if os.environ.get("XDG_SESSION_TYPE") == "wayland":
        raise ValueError("This version requires an X11 session; Wayland capture is incomplete")
    if not os.environ.get("DISPLAY"):
        raise ValueError("No X11 DISPLAY found; run this from your desktop session")
    missing = [name for name in ("wmctrl", "xprop", "ps") if not shutil.which(name)]
    if missing:
        raise ValueError("Missing dependencies: " + ", ".join(missing))


def capture():
    check_desktop()
    items = list(terminals()) + list(apps.capture())
    for index, item in enumerate(items, 1):
        item["id"] = str(index)
    return {"version": 1, "created": datetime.datetime.now(datetime.timezone.utc).isoformat(), "items": items}


def validate(data):
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("items"), list):
        raise ValueError("Unsupported or malformed snapshot")
    ids = set()
    for item in data["items"]:
        if not isinstance(item, dict) or item.get("kind") not in {"app", "terminal"}:
            raise ValueError("Invalid snapshot item")
        for key in ("id", "label", "cwd"):
            if not isinstance(item.get(key), str) or not item[key] or "\0" in item[key]:
                raise ValueError(f"Invalid {key} in snapshot")
        if item["id"] in ids:
            raise ValueError("Duplicate item ID")
        ids.add(item["id"])
        if not os.path.isabs(item["cwd"]):
            raise ValueError("Snapshot working directories must be absolute")
        if not isinstance(item.get("argv"), list) or any(not isinstance(arg, str) or "\0" in arg for arg in item["argv"]):
            raise ValueError("Invalid command arguments")
        if item["kind"] == "terminal":
            if item.get("emulator") not in {"kitty", "gnome-terminal"}:
                raise ValueError("Unsupported terminal emulator")
            if not isinstance(item.get("shell"), str) or Path(item["shell"]).name not in SHELLS:
                raise ValueError("Unsupported shell")
        elif not item["argv"] or not isinstance(item.get("identity"), str):
            raise ValueError("Invalid application identity or command")
    return data


def save(name, data):
    atomic_write(snapshot_path(name), json.dumps(validate(data), indent=2) + "\n")


def load(name):
    return validate(json.loads(snapshot_path(name).read_text()))


def identity(item):
    if item["kind"] == "app":
        return ("app", item["identity"])
    return ("terminal", os.path.realpath(item["cwd"]), tuple(item["argv"]))


def plan(items, running, force=False):
    counts = collections.Counter(identity(item) for item in running)
    result = []
    for item in items:
        key = identity(item)
        skip = not force and counts[key] > 0
        if skip:
            counts[key] -= 1
        result.append((item, skip))
    return result


def launch_argv(item):
    if item["kind"] == "app":
        return item["argv"]
    shell = item["shell"]
    command = [shell]
    if item["argv"]:
        # Use POSIX sh for quoting arbitrary argv, then return to the user's shell.
        script = shlex.join(item["argv"]) + "; exec " + shlex.quote(shell)
        command = [shell, "-ic", "exec /bin/sh -c " + shlex.quote(script)]
    if item["emulator"] == "kitty":
        return ["kitty", "--directory", item["cwd"], "--title", item["label"], *command]
    return ["gnome-terminal", "--working-directory", item["cwd"], "--", *command]


def clean_env():
    return {key: value for key, value in os.environ.items()
            if not key.startswith(("CLAUDE", "CODEX")) and key not in {"SHLVL", "_"}}


def restore(name, dry_run=False, force=False, only=None):
    data = load(name)
    items = data["items"]
    if only:
        unknown = set(only) - {item["id"] for item in items}
        if unknown:
            raise ValueError("Unknown item IDs: " + ", ".join(sorted(unknown)))
        items = [item for item in items if item["id"] in only]
    check_desktop()
    state_dir().mkdir(parents=True, exist_ok=True)
    # Serialize restore invocations so captures and launches cannot interleave.
    with (state_dir() / "restore.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        running = list(terminals()) + list(apps.capture(include_autostart=True))
        failures = 0
        with (state_dir() / "restore.log").open("a") as log:
            os.chmod(log.name, 0o600)
            for item, skip in plan(items, running, force):
                action = "skip" if skip else "launch"
                print(f"{action:6} {item['id']:>3} {item['label']}")
                if skip:
                    continue
                argv = launch_argv(item)
                if dry_run:
                    print("           " + shlex.join(argv))
                    continue
                try:
                    if not Path(item["cwd"]).is_dir():
                        raise ValueError(f"Working directory is missing: {item['cwd']}")
                    process = subprocess.Popen(argv, cwd=item["cwd"], env=clean_env(),
                                               stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                               start_new_session=True)
                    try:
                        status = process.wait(timeout=0.3)
                        if status:
                            raise ValueError(f"Launcher exited with status {status}; see {log.name}")
                    except subprocess.TimeoutExpired:
                        pass
                except (OSError, ValueError) as error:
                    print(f"  failed: {error}")
                    failures += 1
        return 1 if failures else 0
