"""Small GTK tray interface; GTK is optional for CLI users."""
import fcntl
from pathlib import Path
import threading
import signal

from . import core, restart
from .cli import autostart_path, set_autostart, preferences, set_restore_target


def run(restore_on_login=False):
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gio, GLib, Gtk

    core.check_desktop()
    core.state_dir().mkdir(parents=True, exist_ok=True)
    lock = (core.state_dir() / "tray.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Lazarus is already in the tray")
        lock.close()
        return 0
    if not Gtk.init_check()[0]:
        raise ValueError("Could not connect to the desktop")

    class Tray:
        def __init__(self):
            self.busy = False
            self.ending = False
            self.autosave_enabled = not restart.marker_path().exists()
            self.restart_job = None
            self.menu = Gtk.Menu()
            self.status = Gtk.MenuItem(label="Lazarus — desktop sessions")
            self.status.set_sensitive(False)
            self.menu.append(self.status)
            self.menu.append(Gtk.SeparatorMenuItem())
            self.session_actions = [self.add("Save Session", self.save),
                                    self.add("Restore Session…", self.preview),
                                    self.add("Save Current Session as Profile…", self.save_profile)]
            self.restart_action = self.add("Restart into Selected Profile…", self.restart_selected)
            self.cancel_restart_action = self.add("Cancel Restart", self.cancel_restart)
            self.cancel_restart_action.set_sensitive(False)
            self.targets = Gtk.MenuItem(label="Restore at Login")
            self.menu.append(self.targets)
            self.refresh_targets()
            self.add("Open Snapshots Folder", lambda *_: Gtk.show_uri_on_window(None, core.state_dir().as_uri(), 0))
            self.menu.append(Gtk.SeparatorMenuItem())
            startup = Gtk.CheckMenuItem(label="Start Lazarus at Login")
            startup.set_active(autostart_path().exists())
            startup.connect("toggled", self.startup)
            self.menu.append(startup)
            self.quit_action = self.add("Quit", lambda *_: Gtk.main_quit())
            self.menu.show_all()
            icon_path = str(Path(__file__).resolve().parent / "assets/lazarus.png")
            # Prefer AppIndicator; Cinnamon also supports the X11 StatusIcon fallback.
            indicator_module = None
            for namespace in ("AyatanaAppIndicator3", "AppIndicator3"):
                try:
                    gi.require_version(namespace, "0.1")
                    from importlib import import_module
                    indicator_module = import_module("gi.repository." + namespace)
                    break
                except (ValueError, ImportError):
                    pass
            if indicator_module:
                self.icon = indicator_module.Indicator.new("lazarus", icon_path,
                                                           indicator_module.IndicatorCategory.APPLICATION_STATUS)
                self.icon.set_status(indicator_module.IndicatorStatus.ACTIVE)
                self.icon.set_menu(self.menu)
            else:
                self.icon = Gtk.StatusIcon.new_from_file(icon_path)
                self.icon.set_title("Lazarus")
                self.icon.set_tooltip_text("Lazarus — save and restore your desktop")
                self.icon.connect("popup-menu", lambda icon, button, time: self.menu.popup(None, None, Gtk.StatusIcon.position_menu, icon, button, time))
                self.icon.connect("activate", lambda *_: self.menu.popup(None, None, None, None, 0, Gtk.get_current_event_time()))
                self.icon.set_visible(True)

        def add(self, label, callback):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", callback)
            self.menu.append(item)
            return item

        def error(self, error):
            dialog = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE,
                                       text="Lazarus couldn't complete that action")
            dialog.format_secondary_text(str(error))
            dialog.run()
            dialog.destroy()

        def work(self, operation, success, on_success=None, on_error=None):
            if self.busy:
                return
            self.busy = True
            self.status.set_label("Lazarus — working…")
            def worker():
                try:
                    result, error = operation(), None
                except Exception as exception:
                    result, error = None, exception
                GLib.idle_add(done, result, error)
            def done(result, error):
                self.busy = False
                self.status.set_label("Lazarus — " + ("action failed" if error else success))
                if error:
                    if on_error:
                        on_error(error)
                    self.error(error)
                elif on_success:
                    on_success(result)
                return False
            threading.Thread(target=worker, daemon=True).start()

        def save(self, *_):
            if self.busy or self.restart_job:
                return
            self.autosave_enabled = True
            def operation():
                core.capture_save("latest")
                restart.marker_path().unlink(missing_ok=True)
            self.work(operation, "session saved")

        def autosave(self):
            if not self.ending and self.autosave_enabled:
                self.save()
            return True

        def refresh_targets(self):
            menu = Gtk.Menu()
            selected = preferences().get("restore")
            choices = [("Previous Session", "latest"), ("Don't Restore", None)]
            choices += [(p.stem, p.stem) for p in sorted(core.state_dir().glob("*.json"))
                        if p.stem not in {"latest", "preferences"}
                        and not p.stem.startswith(("recovery-", "restart-target-"))]
            group = None
            for label, target in choices:
                item = Gtk.RadioMenuItem.new_with_label(group, label)
                group = item.get_group()
                item.set_active(selected == target)
                item.connect("toggled", self.select_target, target)
                menu.append(item)
            self.targets.set_submenu(menu)
            menu.show_all()

        def select_target(self, item, target):
            if item.get_active():
                try:
                    set_restore_target(target)
                except (OSError, ValueError) as error:
                    self.error(error)

        def save_profile(self, *_):
            if self.busy or self.restart_job:
                return
            dialog = Gtk.Dialog(title="Save a session profile")
            dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
            dialog.add_button("Save", Gtk.ResponseType.OK)
            entry = Gtk.Entry(placeholder_text="Profile name, e.g. work")
            dialog.get_content_area().pack_start(entry, True, True, 12)
            dialog.show_all()
            response = dialog.run()
            name = entry.get_text().strip()
            dialog.destroy()
            if response != Gtk.ResponseType.OK:
                return
            try:
                if name in {"latest", "preferences"} or name.startswith(("recovery-", "restart-target-")):
                    raise ValueError("That name is reserved for automatic session state")
                path = core.snapshot_path(name)
                if path.exists():
                    confirm = Gtk.MessageDialog(message_type=Gtk.MessageType.QUESTION,
                                                buttons=Gtk.ButtonsType.OK_CANCEL,
                                                text=f"Replace profile '{name}'?")
                    accepted = confirm.run() == Gtk.ResponseType.OK
                    confirm.destroy()
                    if not accepted:
                        return
                def operation():
                    core.capture_save(name)
                    GLib.idle_add(self.refresh_targets)
                self.work(operation, "profile saved")
            except (OSError, ValueError) as error:
                self.error(error)

        def restore(self, name="latest"):
            if self.restart_job:
                return False
            def operation():
                if core.restore(name):
                    self.autosave_enabled = False
                    raise ValueError(f"Some apps failed to launch. See {core.state_dir() / 'restore.log'}")
            self.work(operation, "restore finished")
            return False

        def preview(self, *_):
            if self.busy or self.restart_job:
                return
            try:
                data = core.load("latest")
                dialog = Gtk.Dialog(title="Restore desktop session", flags=0)
                dialog.set_default_size(680, 420)
                dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
                dialog.add_button("Restore", Gtk.ResponseType.OK)
                text = "Saved " + data.get("created", "") + "\nAlready running items will be skipped.\n\n"
                text += "\n".join(item["label"] for item in data["items"])
                view = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
                view.get_buffer().set_text(text)
                scroll = Gtk.ScrolledWindow()
                scroll.add(view)
                dialog.get_content_area().pack_start(scroll, True, True, 12)
                dialog.show_all()
                response = dialog.run()
                dialog.destroy()
                if response == Gtk.ResponseType.OK:
                    self.restore()
            except (OSError, ValueError) as error:
                self.error(error)

        def startup(self, item):
            try:
                set_autostart(item.get_active())
            except OSError as error:
                self.error(error)

        def restart_controls(self, active, waiting=False):
            for item in self.session_actions:
                item.set_sensitive(not active)
            self.quit_action.set_sensitive(not active)
            self.restart_action.set_label("Continue Restart…" if active else "Restart into Selected Profile…")
            self.restart_action.set_sensitive(not active or waiting)
            self.cancel_restart_action.set_sensitive(active and waiting)

        def restart_failed(self, error):
            if self.restart_job:
                self.restart_job.close()
                self.restart_job = None
            self.restart_controls(False)
            # Keep autosave paused and the marker intact after any partial switch.

        def restart_selected(self, *_):
            if self.busy:
                return
            if self.restart_job:
                self.run_restart_step(first=False)
                return
            self._autosave_before_restart = self.autosave_enabled
            self.autosave_enabled = False
            self.restart_controls(True)
            def failed(error):
                self.autosave_enabled = self._autosave_before_restart
                self.restart_controls(False)
            self.work(lambda: restart.Restart(preferences().get("restore")), "restart preview ready",
                      on_success=self.confirm_restart, on_error=failed)

        def confirm_restart(self, job):
            self.restart_job = job
            self.busy = True  # Gtk.Dialog runs a nested event loop; keep autosave out.
            dialog = Gtk.Dialog(title="Restart into selected profile")
            dialog.set_default_size(720, 500)
            dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
            dialog.add_button("Close Windows and Restart", Gtk.ResponseType.OK)
            view = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
            view.get_buffer().set_text(job.preview())
            scroll = Gtk.ScrolledWindow()
            scroll.add(view)
            dialog.get_content_area().pack_start(scroll, True, True, 12)
            dialog.show_all()
            response = dialog.run()
            dialog.destroy()
            self.busy = False
            if response == Gtk.ResponseType.OK:
                self.run_restart_step(first=True)
            else:
                self.cancel_restart()

        def run_restart_step(self, first):
            job = self.restart_job
            self.restart_controls(True)
            def operation():
                if first:
                    job.begin()
                else:
                    job.ask_to_close()
                remaining = job.wait()
                if not remaining:
                    job.finish()
                return remaining
            def completed(remaining):
                if remaining:
                    self.restart_controls(True, waiting=True)
                    self.status.set_label("Lazarus — restart paused; waiting for apps")
                    dialog = Gtk.MessageDialog(message_type=Gtk.MessageType.INFO, buttons=Gtk.ButtonsType.CLOSE,
                                               text="Restart paused: work is still running")
                    dialog.format_secondary_text("\n".join(remaining) +
                        "\n\nSave your work and close these apps (including apps running in the background). "
                        "Then choose Continue Restart from the tray, or Cancel Restart. "
                        "Continue sends another normal close request.\n\nRecovery: " + job.recovery_name)
                    dialog.run()
                    dialog.destroy()
                else:
                    self.restart_job = None
                    self.autosave_enabled = True
                    self.restart_controls(False)
                    self.status.set_label("Lazarus — profile restarted")
            self.work(operation, "restart checked", on_success=completed, on_error=self.restart_failed)

        def cancel_restart(self, *_):
            if self.busy or not self.restart_job:
                return
            started = self.restart_job.started
            self.restart_job.close()
            self.restart_job = None
            # After a partial close, retain the recovery marker and pause autosave.
            self.autosave_enabled = False if started else self._autosave_before_restart
            self.restart_controls(False)
            self.status.set_label("Lazarus — restart cancelled" + ("; autosave paused" if started else ""))

    tray = Tray()
    target = preferences().get("restore")
    if restart.marker_path().exists():
        tray.status.set_label("Lazarus — interrupted restart; autosave paused")
    if restore_on_login and not restart.marker_path().exists() and target and core.snapshot_path(target).exists():
        GLib.timeout_add_seconds(10, tray.restore, target)
    # Keep a crash fallback even if the desktop doesn't send logout notifications.
    GLib.timeout_add_seconds(60, tray.autosave)

    def final_save():
        if tray.ending or not tray.autosave_enabled:
            return
        tray.ending = True
        try:
            core.capture_save("latest")
        except Exception as error:
            print(f"Final save failed; keeping periodic snapshot: {error}", flush=True)

    def terminate():
        final_save()
        Gtk.main_quit()
        return False

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, terminate)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, terminate)
    # Cinnamon and GNOME expose the same session client protocol. Save at the
    # query phase, before applications are asked to close; acknowledge promptly.
    session_client = None
    for service in ("org.gnome.SessionManager", "org.cinnamon.SessionManager"):
        try:
            manager = Gio.DBusProxy.new_for_bus_sync(Gio.BusType.SESSION, Gio.DBusProxyFlags.DO_NOT_AUTO_START,
                        None, service, "/org/gnome/SessionManager", "org.gnome.SessionManager", None)
            if not manager.get_name_owner():
                continue
            result = manager.call_sync("RegisterClient", GLib.Variant("(ss)", ("lazarus", "")),
                                       Gio.DBusCallFlags.NONE, 3000, None)
            session_client = Gio.DBusProxy.new_for_bus_sync(Gio.BusType.SESSION, Gio.DBusProxyFlags.NONE,
                        None, service, result.unpack()[0], "org.gnome.SessionManager.ClientPrivate", None)
            def session_signal(proxy, sender, name, parameters):
                if name == "CancelEndSession":
                    tray.ending = False
                if name == "QueryEndSession":
                    final_save()
                if name in {"QueryEndSession", "EndSession"}:
                    proxy.call("EndSessionResponse", GLib.Variant("(bs)", (True, "")),
                               Gio.DBusCallFlags.NONE, 3000, None, None, None)
                if name == "Stop":
                    Gtk.main_quit()
            session_client.connect("g-signal", session_signal)
            print(f"Logout hook registered with {service}", flush=True)
            break
        except GLib.Error as error:
            print(f"Logout hook unavailable: {error}", flush=True)
    try:
        Gtk.main()
    finally:
        lock.close()
    return 0
