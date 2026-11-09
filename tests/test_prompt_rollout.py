import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from agentic_fake import FakeAgenticClient, enable_agentic_service
from pr_harness.core.config import Settings
from pr_harness.core.service import ReviewService


DIFF = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+value = 'REQUEST'\n"
STABLE = "STABLE_PROMPT_MARKER"
CANDIDATE = "CANDIDATE_PROMPT_MARKER"


class RecordingClient(FakeAgenticClient):
    def __init__(self):
        self.calls = []
        self.fail_marker = None
        self.barrier = None
        self.lock = threading.Lock()

    def complete_json(self, role, system, user, ledger=None, max_tokens=None):
        task = json.loads(json.loads(user)["task"])
        with self.lock:
            self.calls.append((role, system, task))
        if self.barrier and role == "lead" and task.get("phase") == "delegate":
            self.barrier.wait(timeout=10)
        if self.fail_marker and self.fail_marker in system:
            raise RuntimeError("temporary model failure")
        return super().complete_json(role, system, user, ledger, max_tokens)


class PromptRolloutTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.addCleanup(os.unlink, self.path)
        settings = Settings(
            host="127.0.0.1", port=8080, db_path=self.path, max_diff_bytes=10000,
            max_steps=8, timeout_seconds=20, llm_base_url="", llm_api_key="",
            llm_model="", github_webhook_secret="", github_token="",
            auto_post_review=False, memory_enabled=False,
        )
        self.service = enable_agentic_service(ReviewService(settings))
        self.addCleanup(self.service.queue.close)
        self.client = RecordingClient()
        self.service.chat_client = self.client
        self.store = self.service.store
        self.store.save_skill_version("llm-review", STABLE, .8, True)
        self.store.save_skill_version("llm-review", CANDIDATE, .9)

    def configure(self, percent=100, tenant="default", candidate=2, **options):
        self.service.releases.configure(tenant, "llm-review", {
            "stable_version": 1, "candidate_version": candidate,
            "canary_percent": percent, **options,
        })

    def assert_prompt(self, marker):
        self.assertTrue(self.client.calls)
        self.assertTrue(all(marker in system for _, system, _ in self.client.calls))

    def test_sync_reviews_use_the_selected_prompt_and_report_its_version(self):
        # A deployment's stable version can differ from the globally active one.
        self.store.save_skill_version("llm-review", "OTHER_ACTIVE_PROMPT", .95, True)
        for percent, version, lane, marker in [
            (0, 1, "stable", STABLE), (100, 2, "canary", CANDIDATE),
        ]:
            with self.subTest(lane=lane):
                self.configure(percent)
                self.client.calls.clear()
                result = self.service.create_review("org/repo", DIFF)
                self.assert_prompt(marker)
                self.assertEqual(
                    {"lane": lane, "version": version},
                    result["report"]["execution"]["prompt_release"],
                )
        self.assertEqual("", self.service.reviewer.prompt_overlay)

    def test_concurrent_lanes_keep_their_own_prompts(self):
        self.configure(0, "stable-tenant")
        self.configure(100, "canary-tenant")
        self.client.barrier = threading.Barrier(2)

        def review(tenant, marker):
            return self.service.create_review(
                "org/repo", DIFF.replace("REQUEST", marker), tenant_id=tenant,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            stable = pool.submit(review, "stable-tenant", "STABLE_REQUEST")
            canary = pool.submit(review, "canary-tenant", "CANARY_REQUEST")
            self.assertEqual("SUCCESS", stable.result(timeout=20)["state"])
            self.assertEqual("SUCCESS", canary.result(timeout=20)["state"])
        for _, system, task in self.client.calls:
            rendered = json.dumps(task)
            if "STABLE_REQUEST" in rendered:
                self.assertIn(STABLE, system)
                self.assertNotIn(CANDIDATE, system)
            if "CANARY_REQUEST" in rendered:
                self.assertIn(CANDIDATE, system)
                self.assertNotIn(STABLE, system)

    def test_queued_task_and_resume_keep_the_original_candidate(self):
        self.configure()
        with patch.object(self.service.queue, "submit") as submit:
            result = self.service.enqueue_review("org/repo", DIFF)
        payload = submit.call_args.args[0]
        self.store.save_skill_version("llm-review", "NEXT_CANDIDATE", .95, True)
        self.configure(candidate=3)
        self.service.harness.node_retries = 0
        self.client.fail_marker = CANDIDATE
        with self.assertRaisesRegex(RuntimeError, "temporary model failure"):
            self.service._process_queued(payload)
        self.assertEqual("FAILED", self.store.get(result["task_id"])["state"])

        self.client.fail_marker = None
        self.client.calls.clear()
        self.service._process_queued(payload)
        self.assert_prompt(CANDIDATE)
        task = self.store.get(result["task_id"])
        self.assertEqual("SUCCESS", task["state"])
        self.assertEqual(2, task["report"]["execution"]["prompt_release"]["version"])
        self.assertEqual(1, sum(item["state"] == "PLANNING" for item in task["trace"]))
        # Work from a previous candidate must not consume the new release's budget.
        deployment = self.store.get_deployment("default", "llm-review")
        self.assertEqual((0, 0), (deployment["samples"], deployment["errors"]))

    def test_webhook_deferred_review_uses_the_candidate(self):
        self.configure()
        event = {
            "action": "opened", "number": 1,
            "repository": {"full_name": "org/repo"},
            "pull_request": {"diff_url": "https://github.com/org/repo/pull/1.diff"},
        }
        with patch.object(self.service.queue, "submit") as submit:
            result = self.service.handle_github_pull_request(event, "delivery", "hash")
        with patch.object(self.service.github, "ensure_repository_access"), \
                patch.object(self.service.github, "fetch_diff", return_value=DIFF):
            self.service._process_queued(submit.call_args.args[0])
        self.assert_prompt(CANDIDATE)
        task = self.store.get(result["task_id"])
        self.assertEqual(2, task["input"]["prompt_version"])
        self.assertEqual("SUCCESS", task["state"])

    def test_rollback_sends_new_tasks_back_to_the_active_prompt(self):
        self.configure(min_samples=1, max_error_rate=0)
        self.client.fail_marker = CANDIDATE
        with self.assertRaisesRegex(RuntimeError, "temporary model failure"):
            self.service.create_review("org/repo", DIFF)
        self.assertEqual(
            "rolled_back", self.store.get_deployment("default", "llm-review")["status"],
        )
        self.client.calls.clear()
        self.client.fail_marker = None
        result = self.service.create_review("org/repo", DIFF)
        self.assert_prompt(STABLE)
        self.assertEqual(
            {"lane": "stable", "version": 1},
            result["report"]["execution"]["prompt_release"],
        )

    def test_legacy_task_without_a_version_gets_a_persistent_assignment(self):
        self.configure()
        self.store.create("legacy", "org/repo", 1, {
            "mode": "agentic", "release_lane": "canary",
        })
        self.store.save_task_payload("legacy", DIFF)
        self.service._process_queued({"task_id": "legacy", "repository": "org/repo"})
        self.assert_prompt(CANDIDATE)
        self.assertEqual(2, self.store.get("legacy")["input"]["prompt_version"])


if __name__ == "__main__":
    unittest.main()
