"""Volume bootstrap refuses unsafe paths and drops privileges before serving."""

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


PATH = Path(__file__).resolve().parents[1] / "deploy/linear-webhook/start.py"
SPEC = importlib.util.spec_from_file_location("webhook_start", PATH)
START = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(START)


class WebhookDeploymentTests(unittest.TestCase):
    def test_root_prepares_mount_and_drops_all_privileges_before_exec(self):
        events = []
        with tempfile.TemporaryDirectory() as volume:
            def record(name):
                return lambda *args: events.append((name, args))
            with patch.object(START.os, "geteuid", side_effect=[0, 10001]), \
                    patch.object(START.os, "getegid", return_value=10001), \
                    patch.object(START.os, "fchown", side_effect=record("chown")), \
                    patch.object(START.os, "fchmod", side_effect=record("chmod")), \
                    patch.object(START.os, "setgroups", side_effect=record("groups"), create=True), \
                    patch.object(START.os, "setgid", side_effect=record("gid")), \
                    patch.object(START.os, "setuid", side_effect=record("uid")), \
                    patch.object(START.os, "execvp", side_effect=record("exec")):
                START.start(["python3", "-m", "dotfactory.agent_webhooks"], volume=volume)
        self.assertEqual([name for name, _ in events], ["chown", "chmod", "groups", "gid", "uid", "exec"])
        self.assertEqual(events[0][1][1:], (10001, 10001))
        self.assertEqual(events[1][1][1:], (0o700,))
        self.assertEqual(events[2][1], ([],))
        self.assertEqual(events[3][1], (10001,))
        self.assertEqual(events[4][1], (10001,))

    def test_mount_symlink_is_refused_before_privilege_changes(self):
        with tempfile.TemporaryDirectory() as root:
            volume = Path(root) / "data"
            volume.symlink_to(root, target_is_directory=True)
            with patch.object(START.os, "geteuid", return_value=0), \
                    patch.object(START.os, "fchown") as chown, \
                    patch.object(START.os, "execvp") as execute:
                with self.assertRaises(OSError):
                    START.start(["python3"], volume=str(volume))
                chown.assert_not_called()
                execute.assert_not_called()

    def test_existing_unprivileged_user_does_not_modify_volume(self):
        with patch.object(START.os, "geteuid", return_value=10001), \
                patch.object(START.os, "getegid", return_value=10001), \
                patch.object(START.os, "open") as open_volume, \
                patch.object(START.os, "execvp") as execute:
            START.start(["python3", "-m", "dotfactory.agent_webhooks"])
            open_volume.assert_not_called()
            execute.assert_called_once_with("python3", ["python3", "-m", "dotfactory.agent_webhooks"])

    def test_failed_privilege_drop_never_executes(self):
        with patch.object(START.os, "geteuid", return_value=0), \
                patch.object(START.os, "open", return_value=7), \
                patch.object(START.os, "close"), \
                patch.object(START.os, "fchown"), \
                patch.object(START.os, "fchmod"), \
                patch.object(START.os, "setgroups", create=True), \
                patch.object(START.os, "setgid"), \
                patch.object(START.os, "setuid", side_effect=PermissionError), \
                patch.object(START.os, "execvp") as execute:
            with self.assertRaises(PermissionError):
                START.start(["python3"])
            execute.assert_not_called()

    def test_wrong_unprivileged_identity_is_refused(self):
        with patch.object(START.os, "geteuid", return_value=os.getuid() + 20000), \
                patch.object(START.os, "execvp") as execute:
            with self.assertRaises(ValueError):
                START.start(["python3"])
            execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
