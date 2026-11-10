import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import Mock

from scripts import run_github_webhook as launcher


class WebhookLauncherTests(unittest.TestCase):
    def test_repository_origin_wins_over_stale_session(self):
        for remote in (
            "https://github.com/owner/new-repo.git",
            "git@github.com:owner/new-repo.git",
            "ssh://git@github.com/owner/new-repo.git",
        ):
            with self.subTest(remote=remote), \
                    patch.dict(os.environ, {"PR_HARNESS_GITHUB_REPOSITORY": ""}), \
                    patch.object(launcher.subprocess, "run", return_value=subprocess.CompletedProcess(
                        [], 0, remote, "",
                    )):
                self.assertEqual(
                    "owner/new-repo", launcher.resolve_repository(None, {"repository": "owner/old"}),
                )

    def test_explicit_repository_and_environment_override_origin(self):
        with patch.dict(os.environ, {"PR_HARNESS_GITHUB_REPOSITORY": "owner/configured"}), \
                patch.object(launcher.subprocess, "run") as run:
            self.assertEqual("owner/requested", launcher.resolve_repository("owner/requested", {}))
            self.assertEqual("owner/configured", launcher.resolve_repository(None, {}))
            run.assert_not_called()

    def test_saved_repository_is_used_without_a_github_origin(self):
        with patch.dict(os.environ, {"PR_HARNESS_GITHUB_REPOSITORY": ""}), \
                patch.object(launcher.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 1, "", "no origin",
                )):
            self.assertEqual("owner/saved", launcher.resolve_repository(None, {"repository": "owner/saved"}))

    def test_get_retries_a_transient_connection_failure(self):
        responses = [
            subprocess.CompletedProcess([], 1, "", "unexpected EOF"),
            subprocess.CompletedProcess([], 0, "[]", ""),
        ]
        with patch.object(launcher.subprocess, "run", side_effect=responses) as run, \
                patch.object(launcher.time, "sleep"):
            self.assertEqual([], launcher.gh_api("repos/owner/repo/hooks"))
            self.assertEqual(2, run.call_count)

    def test_create_does_not_retry_and_risk_duplicate_webhooks(self):
        with patch.object(launcher.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 1, "", "unexpected EOF",
        )) as run:
            with self.assertRaisesRegex(RuntimeError, "EOF"):
                launcher.gh_api("repos/owner/repo/hooks", "POST", {"name": "web"})
            self.assertEqual(1, run.call_count)

    def test_debugger_cleanup_only_stops_children_with_matching_creation_time(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "status.json"
            stop_path = Path(directory) / "stop.request"
            state = {
                "repository": "owner/repo", "running": True, "phase": "running",
                "launcher_pid": 10, "app_pid": 11, "app_pid_started": "original-app",
                "tunnel_pid": 12, "tunnel_pid_started": "original-tunnel", "hook_id": 123,
            }
            state_path.write_text(json.dumps(state), encoding="utf-8")
            births = {11: "unrelated-reused-pid", 12: "original-tunnel"}
            with patch.object(launcher, "STATE", state_path), \
                    patch.object(launcher, "STOP", stop_path), \
                    patch.object(launcher, "process_running", return_value=False), \
                    patch.object(launcher, "process_started", side_effect=births.get), \
                    patch.object(launcher.subprocess, "run") as run, \
                    patch.object(launcher.os, "kill") as kill, \
                    patch.object(launcher, "gh_api") as gh_api:
                launcher.stop_session(state)
                if os.name == "nt":
                    self.assertEqual(["taskkill", "/PID", "12", "/T", "/F"], run.call_args.args[0])
                    self.assertEqual(1, run.call_count)
                else:
                    self.assertEqual(12, kill.call_args.args[0])
                    self.assertEqual(1, kill.call_count)
                gh_api.assert_called_once_with("repos/owner/repo/hooks/123", "PATCH", {"active": False})
            self.assertFalse(json.loads(state_path.read_text(encoding="utf-8"))["running"])
            self.assertTrue(stop_path.exists())

    def test_current_process_has_a_stable_creation_identity(self):
        first = launcher.process_started(os.getpid())
        self.assertTrue(first)
        self.assertEqual(first, launcher.process_started(os.getpid()))

    def test_webhook_readiness_requires_a_new_successful_github_delivery(self):
        replies = [
            [{"id": 1, "event": "ping", "status_code": 202}],
            None,
            [{"id": 2, "event": "ping", "status_code": 202}],
        ]
        with patch.object(launcher, "gh_api", side_effect=replies) as api, \
                patch.object(launcher, "STOP", Mock(exists=Mock(return_value=False))):
            launcher.wait_for_webhook("repos/owner/repo/hooks/123", Mock(poll=Mock(return_value=None)))
            self.assertIn(
                (("repos/owner/repo/hooks/123/pings", "POST"), {}),
                [(call.args, call.kwargs) for call in api.call_args_list],
            )

    def test_webhook_readiness_rejects_a_signature_failure(self):
        replies = [[], None, [{"id": 2, "event": "ping", "status_code": 401}]]
        with patch.object(launcher, "gh_api", side_effect=replies), \
                patch.object(launcher, "STOP", Mock(exists=Mock(return_value=False))):
            with self.assertRaisesRegex(RuntimeError, "signature"):
                launcher.wait_for_webhook("repos/owner/repo/hooks/123", Mock(poll=Mock(return_value=None)))


if __name__ == "__main__":
    unittest.main()
