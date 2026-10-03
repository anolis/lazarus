"""Small GTK tray interface; GTK is optional for CLI users."""
import fcntl
from pathlib import Path
import threading
import signal

from . import core
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
            self.autosave_enabled = True
            self.menu = Gtk.Menu()
            self.status = Gtk.MenuItem(label="Lazarus — desktop sessions")
            self.status.set_sensitive(False)
            self.menu.append(self.status)
            self.menu.append(Gtk.SeparatorMenuItem())
            self.add("Save Session", self.save)
            self.add("Restore Session…", self.preview)
            self.add("Save Current Session as Profile…", self.save_profile)
            self.targets = Gtk.MenuItem(label="Restore at Login")
            self.menu.append(self.targets)
            self.refresh_targets()
            self.add("Open Snapshots Folder", lambda *_: Gtk.show_uri_on_window(None, core.state_dir().as_uri(), 0))
            self.menu.append(Gtk.SeparatorMenuItem())
            startup = Gtk.CheckMenuItem(label="Start Lazarus at Login")
            startup.set_active(autostart_path().exists())
            startup.connect("toggled", self.startup)
            self.menu.append(startup)
            self.add("Quit", lambda *_: Gtk.main_quit())
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

        def error(self, error):
            dialog = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE,
                                       text="Lazarus couldn't complete that action")
            dialog.format_secondary_text(str(error))
            dialog.run()
            dialog.destroy()

        def work(self, operation, success):
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
                    self.error(error)
                return False
            threading.Thread(target=worker, daemon=True).start()

        def save(self, *_):
            self.autosave_enabled = True
            self.work(lambda: core.save("latest", core.capture()), "session saved")

        def autosave(self):
            if not self.ending and self.autosave_enabled:
                self.save()
            return True

        def refresh_targets(self):
            menu = Gtk.Menu()
            selected = preferences().get("restore")
            choices = [("Previous Session", "latest"), ("Don't Restore", None)]
            choices += [(p.stem, p.stem) for p in sorted(core.state_dir().glob("*.json"))
                        if p.stem not in {"latest", "preferences"}]
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
            if self.busy:
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
                if name in {"latest", "preferences"}:
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
                    core.save(name, core.capture())
                    GLib.idle_add(self.refresh_targets)
                self.work(operation, "profile saved")
            except (OSError, ValueError) as error:
                self.error(error)

        def restore(self, name="latest"):
            def operation():
                if core.restore(name):
                    self.autosave_enabled = False
                    raise ValueError(f"Some apps failed to launch. See {core.state_dir() / 'restore.log'}")
            self.work(operation, "restore finished")
            return False

        def preview(self, *_):
            if self.busy:
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

    tray = Tray()
    target = preferences().get("restore")
    if restore_on_login and target and core.snapshot_path(target).exists():
        GLib.timeout_add_seconds(10, tray.restore, target)
    # Keep a crash fallback even if the desktop doesn't send logout notifications.
    GLib.timeout_add_seconds(60, tray.autosave)

    def final_save():
        if tray.ending or not tray.autosave_enabled:
            return
        tray.ending = True
        try:
            core.save("latest", core.capture())
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
