"""A staged, recoverable workspace switch. Never kill applications by PID."""
import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

from . import apps, core


def marker_path():
    return core.state_dir() / ".restart-pending"


def interrupted():
    return json.loads(marker_path().read_text()) if marker_path().exists() else None


def fingerprint(pid):
    """PID plus Linux start time protects against PID reuse; only our processes."""
    try:
        path = Path(f"/proc/{pid}")
        if path.stat().st_uid != os.getuid():
            return None
        fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return (pid, fields[19])
    except (OSError, IndexError):
        return None


def windows():
    result = subprocess.run(["wmctrl", "-lp"], capture_output=True, text=True,
                            check=True, timeout=10)
    found = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) >= 4 and parts[2].isdigit():
            found[parts[0]] = {"pid": int(parts[2]), "host": parts[3],
                               "label": parts[4] if len(parts) > 4 else parts[0]}
    return found


def supports_close(wid):
    result = subprocess.run(["xprop", "-id", wid, "WM_PROTOCOLS"],
                            capture_output=True, text=True, check=True, timeout=3)
    return "WM_DELETE_WINDOW" in result.stdout


def request_close(wid):
    # Ask the window manager to perform a normal close, preserving app prompts.
    subprocess.run(["wmctrl", "-ic", wid], check=True, capture_output=True, text=True, timeout=3)


def preflight(data):
    core.validate(data)
    for item in data["items"]:
        if not Path(item["cwd"]).is_dir():
            raise ValueError(f"Cannot restart: missing directory {item['cwd']}")
        commands = [item["emulator"], item["shell"]] if item["kind"] == "terminal" else item["argv"][:1]
        for command in commands:
            candidate = str(Path(item["cwd"]) / command) if "/" in command else command
            if not shutil.which(candidate):
                raise ValueError(f"Cannot restart: launcher is unavailable: {command}")


class Restart:
    def __init__(self, name):
        if name is None:
            raise ValueError("Choose Previous Session or a profile under Restore at Login first")
        self.name = name
        self._lock = core.session_lock()
        self._lock.__enter__()
        self.closed = False
        self.started = False
        self.restored = False
        try:
            core.check_desktop()
            # Pin latest before capturing the recovery session or showing any UI.
            self.target = core.load(name)
            preflight(self.target)
            runtime = list(core.terminals(include_runtime=True)) + list(apps.capture(include_runtime=True))
            runtime = [item for item in runtime if item["_pid"] != os.getpid()]
            self.processes = {}
            self.terminal_roots = {}
            owners = set()
            table = core.process_table()
            for item in runtime:
                pid = item["_pid"]
                owners.add(item.get("_emulator_pid", pid))
                tracked = {pid}
                if item["kind"] == "terminal":
                    if stamp := fingerprint(pid):
                        self.terminal_roots[stamp] = item["label"]
                    while True:
                        children = {p for p, info in table.items() if info["parent"] in tracked}
                        if children <= tracked:
                            break
                        tracked |= children
                for process in tracked:
                    if stamp := fingerprint(process):
                        self.processes[stamp] = item["label"]
            self.windows = {}
            hostnames = {os.uname().nodename, os.uname().nodename.split(".")[0], "localhost"}
            for wid, window in windows().items():
                if window["pid"] not in owners:
                    continue
                if window["host"] not in hostnames:
                    raise ValueError(f"Cannot safely identify the host of window: {window['label']}")
                stamp = fingerprint(window["pid"])
                if not stamp:
                    raise ValueError(f"Cannot identify the owner of window: {window['label']}")
                if not supports_close(wid):
                    raise ValueError(f"Window does not support graceful closing: {window['label']}")
                self.windows[wid] = dict(window, stamp=stamp)
            represented = {window["pid"] for window in self.windows.values()}
            if owners - represented:
                raise ValueError("Some tracked work has no identifiable window to close; close it manually and try again")
            suffix = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
            self.recovery_name = "recovery-" + suffix
            self.target_name = "restart-target-" + suffix
        except Exception:
            self.close()
            raise

    def preview(self):
        target_label = "Previous Session" if self.name == "latest" else self.name
        closing = "\n".join("  • " + w["label"] for w in self.windows.values()) or "  (none)"
        launching = "\n".join("  • " + i["label"] for i in self.target["items"]) or "  (empty profile)"
        return (f"Restart into: {target_label}\n\n"
                "These windows will receive a normal close request. Save your work and respond to any app prompts.\n\n"
                f"CLOSE\n{closing}\n\nRESTORE\n{launching}\n\n"
                f"Recovery snapshot: {self.recovery_name}\n"
                "If work remains running, the switch pauses. Nothing will be force-killed. "
                "Cancelling after closing starts does not reopen closed windows.")

    def _ensure_open(self):
        if self.closed:
            raise ValueError("This restart has ended; preview a new restart")

    def begin(self):
        self._ensure_open()
        if self.started:
            raise ValueError("Restart already started")
        preflight(self.target)
        # Both snapshots and the interruption marker must reach disk before closing.
        core.save(self.target_name, self.target)
        core.save(self.recovery_name, core.capture())
        core.atomic_write(marker_path(), json.dumps({"target": self.target_name, "recovery": self.recovery_name}) + "\n")
        self.started = True
        self.ask_to_close()

    def ask_to_close(self):
        self._ensure_open()
        if not self.started:
            raise ValueError("Save recovery before closing windows")
        # The user may start a new command while reviewing the preview. Include
        # new descendants of the original shells before asking windows to close.
        table = core.process_table()
        for stamp, label in self.terminal_roots.items():
            if fingerprint(stamp[0]) != stamp:
                continue
            tracked = {stamp[0]}
            while True:
                children = {p for p, info in table.items() if info["parent"] in tracked}
                if children <= tracked:
                    break
                tracked |= children
            for pid in tracked:
                if child_stamp := fingerprint(pid):
                    self.processes[child_stamp] = label
        current = windows()
        for wid, window in self.windows.items():
            live = current.get(wid)
            if live and live["pid"] == window["pid"] and fingerprint(live["pid"]) == window["stamp"]:
                if not supports_close(wid):
                    raise ValueError(f"Window no longer supports graceful closing: {window['label']}")
                request_close(wid)

    def pending(self):
        self._ensure_open()
        current = windows()
        remaining = []
        for wid, window in self.windows.items():
            live = current.get(wid)
            if live and live["pid"] == window["pid"] and fingerprint(live["pid"]) == window["stamp"]:
                remaining.append(window["label"])
        for stamp, label in self.processes.items():
            if fingerprint(stamp[0]) == stamp:
                remaining.append(label)
        return sorted(set(remaining))

    def wait(self, timeout=15):
        deadline = time.monotonic() + timeout
        while True:
            pending = self.pending()
            if not pending or time.monotonic() >= deadline:
                return pending
            time.sleep(0.25)

    def finish(self):
        self._ensure_open()
        if not self.started or self.restored:
            raise ValueError("Restart is not ready to restore")
        if self.pending():
            raise ValueError("Tracked work is still running; finish closing it before continuing")
        self.restored = True
        if core.restore_snapshot(self.target, locked=True):
            raise ValueError(f"Some launches failed. Recover with: lazarus restore {self.recovery_name}")
        marker_path().unlink(missing_ok=True)
        self.close()

    def close(self):
        """Release the lock; retain recovery and interruption metadata on failure."""
        if not self.closed:
            self.closed = True
            self._lock.__exit__(None, None, None)
