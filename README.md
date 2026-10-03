# Lazarus

<img src="lazarus/assets/lazarus.png" width="128" alt="Lazarus application icon">

A small Linux system-tray app that brings your desktop work back after reboot:
terminal commands, Claude/Codex sessions, and GUI applications.

[Project website](https://anolis.github.io/lazarus/) · [Report an issue](https://github.com/anolis/lazarus/issues)

## Run

Requires Python 3.10+, `wmctrl`, `xprop`, and an X11 desktop. The tray additionally
uses GTK 3 and PyGObject (Debian/Ubuntu: `python3-gi gir1.2-gtk-3.0`). AppIndicator
is used when available, with a legacy system-tray fallback for Cinnamon.

From this checkout:

```sh
./bin/lazarus
```

The tray saves your session every minute. On Cinnamon/GNOME it registers for
logout notifications and saves before apps close. SIGTERM also triggers a final
save; periodic snapshots provide a fallback for crashes and other desktops.
Keep Lazarus running for automatic capture. Clicking Quit stops automatic saving.

Choose **Start Lazarus at Login** to keep it running across logins. Under
**Restore at Login**, select **Previous Session**, a named profile, or
**Don't Restore**. Restore starts ten seconds after login, skipping detected
running items. Automatic saving starts after one minute.

**Save Current Session as Profile…** creates a fixed snapshot. Autosave only
updates `latest`; profiles change only when you explicitly save over them.
**Restore Session…** previews the previous session before launching it.

## CLI

```sh
./bin/lazarus doctor
./bin/lazarus save
./bin/lazarus list
./bin/lazarus show latest
./bin/lazarus restore --dry-run
./bin/lazarus restore --only 1 3
./bin/lazarus profile save work
./bin/lazarus profile select work
./bin/lazarus profile previous
./bin/lazarus profile off
./bin/lazarus autostart enable
./bin/lazarus autostart disable
```

`save NAME` and `restore NAME` also support named snapshots. `restore --force`
bypasses duplicate detection. For an installed CLI, `pip install .` provides
`lazarus`; use a Python environment with system GTK bindings for the tray.

Snapshots and preferences live in `$XDG_STATE_HOME/lazarus` (default
`~/.local/state/lazarus`). Snapshots are JSON written atomically with mode 0600.
Launcher output goes to `restore.log` there. Treat snapshots as executable
configuration: review files received from others before restoring them.

If a restore launcher fails, automatic saving pauses to preserve the saved
session. Use **Save Session** after resolving the failure to resume saving.

## Current scope

- Kitty and GNOME Terminal, preserving the emulator, shell, cwd, and foreground
  argv. Shell startup files load before resumed commands.
- Claude named resumes and explicit Codex resume IDs are preserved. Other AI
  sessions use latest-in-directory; multiple sessions in the same directory
  cannot yet be pinned reliably.
- Desktop launchers, Flatpak, moved AppImages, Firefox, Nemo, and VLC are
  recognized using the prototype's rules. Firefox's own session restore must
  be enabled to recover tabs. Nemo folder detection is based on window titles.
- Terminal dedupe matches cwd and resume command, accounting for multiplicity.
  App dedupe uses WM_CLASS. It is conservative about existing app windows;
  workspace/geometry, browser-tab-level and multi-window restoration are pending.
- A launch success means the process started, not that its application session
  fully recovered. Very slow window creation can defeat immediate repeat-restore
  deduplication. Check `restore.log` for application errors.
- Automatic saving requires the tray process. Logout hooks and reboot recovery
  need a real logout/reboot test on your desktop; SIGKILL/power loss can lose up
  to a minute of changes. Wayland is not supported yet.

The original scripts remain in `prototype/`; background restore does not execute
their generated shell scripts. Restored processes have Claude/Codex session
environment variables removed.

## Development

```sh
python3 -m unittest discover -s tests -v
```
