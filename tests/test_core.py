import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from lazarus import core
from lazarus.cli import main, preferences, set_autostart, set_restore_target


def terminal(argv=None):
    return {"id": "1", "kind": "terminal", "label": "test", "cwd": "/tmp", "shell": "/bin/bash",
            "emulator": "kitty", "argv": argv or []}


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"XDG_STATE_HOME": self.temp.name, "XDG_CONFIG_HOME": self.temp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_snapshot_roundtrip_permissions_and_validation(self):
        data = {"version": 1, "items": [terminal(["printf", "it's a $test; touch /bad"])]}
        core.save("work", data)
        self.assertEqual(core.load("work"), data)
        self.assertEqual(core.snapshot_path("work").stat().st_mode & 0o777, 0o600)
        for name in ("../oops", "", "preferences", "a/b"):
            with self.assertRaises(ValueError):
                core.save(name, data)
        data["items"][0]["argv"] = "echo bad"
        with self.assertRaises(ValueError):
            core.validate(data)

    def test_resume_does_not_match_prompt(self):
        self.assertEqual(core.resume(["echo", "ask claude"]), ["echo", "ask claude"])
        self.assertEqual(core.resume(["claude", "--resume", "daily-todo", "prompt"]), ["claude", "--resume", "daily-todo"])
        self.assertEqual(core.resume(["/opt/bin/codex", "resume", "abc", "prompt"]), ["codex", "resume", "abc"])

    def test_dedupe_counts_instances(self):
        one = terminal(["claude", "--continue"])
        self.assertEqual([skip for _, skip in core.plan([one, one], [one])], [True, False])
        self.assertFalse(core.plan([one], [one], force=True)[0][1])
        other = dict(one, cwd="/elsewhere")
        self.assertFalse(core.plan([one], [other])[0][1])

    def test_shell_arguments_survive(self):
        values = ["space here", "'quote'", "$(touch /tmp/lazarus-should-not-exist)", "semi;colon"]
        item = terminal(["/usr/bin/python3", "-c", "import sys,json; print(json.dumps(sys.argv[1:]))", *values])
        command = core.launch_argv(item)[5:]
        result = subprocess.run(command, input="exit\n", capture_output=True, text=True, timeout=5)
        self.assertEqual(json.loads(result.stdout.splitlines()[0]), values)

    def test_clean_environment(self):
        with patch.dict(os.environ, {"CLAUDE_CODE_CHILD_SESSION": "1", "CODEX_THREAD_ID": "private", "DISPLAY": ":0"}):
            env = core.clean_env()
        self.assertNotIn("CLAUDE_CODE_CHILD_SESSION", env)
        self.assertNotIn("CODEX_THREAD_ID", env)
        self.assertEqual(env["DISPLAY"], ":0")

    def test_profiles_are_not_overwritten_by_latest(self):
        profile = {"version": 1, "items": [terminal(["echo", "work"])]}
        core.save("work", profile)
        set_restore_target("work")
        core.save("latest", {"version": 1, "items": []})
        self.assertEqual(core.load("work"), profile)
        self.assertEqual(preferences()["restore"], "work")
        set_restore_target(None)
        self.assertIsNone(preferences()["restore"])

    def test_autostart(self):
        set_autostart(True)
        content = (Path(self.temp.name) / "autostart/lazarus.desktop").read_text()
        self.assertIn('"tray" "--restore"', content)
        set_autostart(False)
        self.assertFalse((Path(self.temp.name) / "autostart/lazarus.desktop").exists())

    def test_preview_never_launches(self):
        core.save("latest", {"version": 1, "items": [terminal(["echo", "hello"])]})
        with patch.object(core, "check_desktop"), patch.object(core, "terminals", return_value=[]), \
                patch.object(core.apps, "capture", return_value=[]), patch.object(core.subprocess, "Popen") as launch:
            self.assertEqual(core.restore("latest", dry_run=True), 0)
            launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
