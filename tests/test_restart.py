import os
import tempfile
import unittest
from unittest.mock import patch

from lazarus import core, restart


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.mock(patch.dict(os.environ, {"XDG_STATE_HOME": self.temp.name}))
        self.item = {"id": "1", "kind": "app", "label": "Editor", "cwd": self.temp.name,
                     "argv": ["/bin/true"], "identity": "editor"}
        self.target = {"version": 1, "items": [self.item]}
        self.recovery = {"version": 1, "items": []}
        core.save("latest", self.target)
        self.mock(patch.object(core, "check_desktop"))
        self.mock(patch.object(core, "terminals", return_value=[]))
        self.mock(patch.object(core, "process_table", return_value={}))
        self.mock(patch.object(core, "capture", return_value=self.recovery))
        self.mock(patch.object(restart.apps, "capture", return_value=[dict(self.item, _pid=100)]))
        self.window = {"pid": 100, "host": os.uname().nodename, "label": "Editor window"}
        self.windows = self.mock(patch.object(restart, "windows", return_value={"0x123": self.window}))
        self.fingerprint = self.mock(patch.object(restart, "fingerprint", side_effect=lambda pid: (pid, "original")))
        self.protocol = self.mock(patch.object(restart, "supports_close", return_value=True))
        self.close_window = self.mock(patch.object(restart, "request_close"))
        self.restore = self.mock(patch.object(core, "restore_snapshot", return_value=0))

    def mock(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def prepare(self):
        job = restart.Restart("latest")
        self.addCleanup(job.close)
        return job

    def all_closed(self):
        self.windows.return_value = {}
        self.fingerprint.side_effect = lambda pid: None

    def test_preview_and_cancel_have_no_close_or_save_side_effects(self):
        job = self.prepare()
        self.assertIn("Editor window", job.preview())
        self.assertIn("Previous Session", job.preview())
        job.close()
        self.close_window.assert_not_called()
        self.restore.assert_not_called()
        self.assertFalse(core.snapshot_path(job.recovery_name).exists())
        self.assertFalse(restart.marker_path().exists())

    def test_recovery_and_pinned_target_written_before_first_close(self):
        job = self.prepare()
        # Simulate latest changing after the preview: restore must use the old target.
        core.save("latest", self.recovery)
        def on_close(wid):
            self.assertEqual(core.load(job.recovery_name), self.recovery)
            self.assertEqual(core.load(job.target_name), self.target)
            self.assertEqual(restart.interrupted()["recovery"], job.recovery_name)
        self.close_window.side_effect = on_close
        job.begin()
        self.close_window.assert_called_once_with("0x123")
        self.all_closed()
        job.finish()
        self.restore.assert_called_once_with(self.target, locked=True)
        self.assertFalse(restart.marker_path().exists())
        self.assertTrue(job.closed)

    def test_cancelled_close_or_background_process_pauses_restore(self):
        job = self.prepare()
        job.begin()
        self.assertTrue(job.wait(timeout=0))
        with self.assertRaisesRegex(ValueError, "still running"):
            job.finish()
        self.windows.return_value = {}  # Window closed; process still running.
        self.assertEqual(job.pending(), ["Editor"])
        with self.assertRaisesRegex(ValueError, "still running"):
            job.finish()
        self.restore.assert_not_called()
        self.all_closed()
        job.ask_to_close()
        job.finish()
        self.restore.assert_called_once()

    def test_pid_reuse_does_not_close_unrelated_window(self):
        job = self.prepare()
        self.fingerprint.side_effect = lambda pid: (pid, "reused")
        job.begin()
        self.close_window.assert_not_called()
        self.assertEqual(job.pending(), [])

    def test_missing_launcher_blocks_before_close(self):
        self.target["items"][0]["argv"] = ["/missing/lazarus-test-launcher"]
        core.save("latest", self.target)
        with self.assertRaisesRegex(ValueError, "launcher is unavailable"):
            self.prepare()
        self.close_window.assert_not_called()
        with core.session_lock():
            pass  # Failed preparation releases the lock.

    def test_missing_directory_blocks_before_close(self):
        self.target["items"][0]["cwd"] = "/missing/lazarus-test-directory"
        core.save("latest", self.target)
        with self.assertRaisesRegex(ValueError, "missing directory"):
            self.prepare()
        self.close_window.assert_not_called()

    def test_unsupported_close_blocks_before_any_close(self):
        self.protocol.return_value = False
        with self.assertRaisesRegex(ValueError, "graceful closing"):
            self.prepare()
        self.close_window.assert_not_called()

    def test_recovery_write_failure_does_not_close(self):
        job = self.prepare()
        with patch.object(core, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                job.begin()
        self.close_window.assert_not_called()
        self.assertFalse(job.started)

    def test_partial_cancel_keeps_recovery_and_marker(self):
        job = self.prepare()
        job.begin()
        job.close()
        self.assertTrue(restart.marker_path().exists())
        self.assertEqual(core.load(job.recovery_name), self.recovery)
        self.restore.assert_not_called()
        with core.session_lock():
            pass

    def test_failed_restore_keeps_recovery_and_cannot_repeat(self):
        job = self.prepare()
        job.begin()
        self.all_closed()
        self.restore.return_value = 1
        with self.assertRaisesRegex(ValueError, "Some launches failed"):
            job.finish()
        self.assertTrue(restart.marker_path().exists())
        with self.assertRaisesRegex(ValueError, "not ready"):
            job.finish()
        self.restore.assert_called_once()

    def test_restart_excludes_other_capture_and_restore_operations(self):
        job = self.prepare()
        with self.assertRaisesRegex(ValueError, "in progress"):
            core.capture_save()
        # Call the actual restore implementation (restore_snapshot is mocked here).
        with self.assertRaisesRegex(ValueError, "in progress"):
            with core.session_lock():
                pass
        job.close()
        self.assertEqual(core.capture_save(), self.recovery)

    def test_no_selected_profile_does_not_close(self):
        with self.assertRaisesRegex(ValueError, "Choose Previous Session"):
            restart.Restart(None)
        self.close_window.assert_not_called()

    def test_terminal_waits_for_shell_and_children_not_emulator_server(self):
        term = {"id": "1", "kind": "terminal", "label": "Terminal", "cwd": self.temp.name,
                "argv": [], "shell": "/bin/bash", "emulator": "gnome-terminal",
                "_pid": 200, "_emulator_pid": 100}
        with patch.object(core, "terminals", return_value=[term]), \
                patch.object(restart.apps, "capture", return_value=[]), \
                patch.object(core, "process_table", return_value={201: {"parent": 200}, 202: {"parent": 201}}):
            job = self.prepare()
        self.assertEqual({stamp[0] for stamp in job.processes}, {200, 201, 202})
        job.begin()
        self.windows.return_value = {}
        self.fingerprint.side_effect = lambda pid: (pid, "original") if pid in {100, 202} else None
        self.assertEqual(job.pending(), ["Terminal"])
        self.fingerprint.side_effect = lambda pid: (pid, "original") if pid == 100 else None
        self.assertEqual(job.pending(), [])

    def test_command_started_during_preview_is_also_awaited(self):
        term = {"kind": "terminal", "label": "Terminal", "_pid": 200, "_emulator_pid": 100}
        with patch.object(core, "terminals", return_value=[term]), \
                patch.object(restart.apps, "capture", return_value=[]):
            job = self.prepare()
        with patch.object(core, "process_table", return_value={201: {"parent": 200}}):
            job.begin()
        self.windows.return_value = {}
        self.fingerprint.side_effect = lambda pid: (pid, "original") if pid == 201 else None
        self.assertEqual(job.pending(), ["Terminal"])


if __name__ == "__main__":
    unittest.main()
