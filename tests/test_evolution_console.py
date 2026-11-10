import json
import time
import unittest
from unittest.mock import patch

import test_prompt_rollout
from test_prompt_rollout import CANDIDATE, DIFF, STABLE


class EvolutionConsoleTests(unittest.TestCase):
    def setUp(self):
        test_prompt_rollout.PromptRolloutTests.setUp(self)
        self.console = self.service.evolution_console
        self.store.save_evolution_run({
            "id": "candidate-2", "skill_name": "llm-review", "candidate_version": 2,
            "baseline_version": 1, "decision": "shadow_ready", "candidate_score": 1.,
            "baseline_score": .8, "metrics": {"gates": {"safety": True, "evaluation_success": True},
                                                "reason": "passed"}, "created_at": "2026-11-10",
        })

    def action(self, action, **kwargs):
        data = self.console.status("default")
        return self.console.action("default", {"action": action, "version": 2, "confirmed": True,
            "expected_current_version": data["current_version"],
            "expected_release_id": (data["deployment"] or {}).get("release_id"), **kwargs})

    def wait_observation(self):
        for _ in range(150):
            rows = self.store.list_release_observations("default", "llm-review")
            if rows:
                return rows
            time.sleep(.02)
        self.fail("shadow observation was not persisted")

    def test_requires_confirmation_and_rejects_failed_candidate(self):
        with self.assertRaisesRegex(ValueError, "确认"):
            self.console.action("default", {"action": "activate", "version": 2})
        self.store.update_evolution_run("candidate-2", "rejected", {"gates": {"safety": False}})
        with self.assertRaisesRegex(ValueError, "未通过"):
            self.action("activate")
        self.assertEqual(1, self.store.get_active_skill_version("llm-review")["version"])

    def test_activate_pins_new_tasks_and_rollback_restores_previous(self):
        result = self.action("activate")
        self.assertEqual(2, result["current_version"])
        task = self.service.create_review("org/repo", DIFF)
        self.assertEqual(2, task["report"]["execution"]["prompt_release"]["version"])
        self.assertEqual(1, self.action("rollback")["current_version"])
        # Historical evaluation stays intact; live state is a different field.
        self.assertEqual("shadow_ready", self.store.list_evolution_runs()[0]["decision"])

    def test_default_baseline_can_be_restored_without_activating_a_rejected_version(self):
        with self.store._connect() as conn:
            conn.execute("UPDATE skill_versions SET active=0")
            conn.execute("UPDATE evolution_runs SET baseline_version=NULL")
        self.action("activate")
        result = self.action("rollback")
        self.assertIsNone(result["current_version"])
        self.assertIsNone(self.store.get_active_skill_version("llm-review"))
        task = self.service.create_review("org/repo", DIFF)
        self.assertIsNone(task["report"]["execution"]["prompt_release"]["version"])

    def test_stale_page_and_changed_evaluation_baseline_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "正式版本已变化"):
            self.console.action("default", {"action": "activate", "version": 2,
                "confirmed": True, "expected_current_version": None})
        self.store.save_skill_version("llm-review", "next stable", .9, True)
        with self.assertRaisesRegex(ValueError, "基准"):
            self.action("shadow")

    def test_shadow_runs_full_candidate_without_replacing_primary_report(self):
        self.action("shadow", min_samples=1)
        primary = self.service.create_review("org/repo", DIFF)
        rows = self.wait_observation()
        self.assertEqual(1, primary["report"]["execution"]["prompt_release"]["version"])
        self.assertEqual(2, rows[0]["candidate"]["prompt_version"])
        self.assertEqual("SUCCESS", rows[0]["candidate"]["state"])
        self.assertGreater(rows[0]["candidate"]["total_tokens"], 0)
        self.assertEqual(1, self.console.status("default")["current_version"])
        self.assertTrue(any(CANDIDATE in system for _, system, _ in self.client.calls))
        self.assertTrue(any(STABLE in system for _, system, _ in self.client.calls))
        task = self.store.get(rows[0]["candidate"]["task_id"])
        self.assertEqual(primary["task_id"], task["input"]["primary_task_id"])

    def test_automatic_promotion_really_changes_the_next_task_version(self):
        self.action("shadow", min_samples=1, auto_promote=True)
        self.service.create_review("org/repo", DIFF)
        self.wait_observation()
        for _ in range(50):
            if self.store.get_deployment("default", "llm-review")["status"] == "promoted":
                break
            time.sleep(.01)
        self.assertEqual(2, self.store.get_active_skill_version("llm-review")["version"])
        next_task = self.service.create_review("org/repo", DIFF)
        self.assertEqual(2, next_task["report"]["execution"]["prompt_release"]["version"])

    def test_stopping_shadow_prevents_queued_work_and_late_observations(self):
        self.action("shadow")
        with patch.object(self.service.queue, "submit") as submit:
            result = self.service.create_review("org/repo", DIFF)
        payload = submit.call_args.args[0]
        self.action("stop")
        self.service._process_queued(payload)
        self.assertEqual("CANCELLED", self.store.get(payload["task_id"])["state"])
        self.assertEqual([], self.store.list_release_observations("default", "llm-review"))
        self.assertEqual("SUCCESS", self.store.get(result["task_id"])["state"])

    def test_duplicate_observation_does_not_advance_progress_twice(self):
        self.action("shadow")
        self.service.create_review("org/repo", DIFF)
        rows = self.wait_observation()
        self.service._record_shadow(rows[0]["candidate"]["task_id"], "default")
        release = self.store.get_deployment("default", "llm-review")
        self.assertEqual(1, release["shadow_samples"])
        self.assertEqual(1, len(self.store.list_release_observations("default", "llm-review")))

    def test_prior_trial_cannot_count_toward_a_new_trial_of_the_same_version(self):
        self.action("shadow")
        with patch.object(self.service.queue, "submit") as submit:
            self.service.create_review("org/repo", DIFF)
        payload = submit.call_args.args[0]
        self.action("stop")
        self.action("shadow")
        self.service._process_queued(payload)
        self.assertEqual(0, self.store.get_deployment("default", "llm-review")["shadow_samples"])

    def test_error_budget_stops_shadow_and_preserves_stable(self):
        self.action("shadow", min_samples=2, max_error_rate=0, auto_promote=True)
        dep = self.store.get_deployment("default", "llm-review")
        for number, failed in [(1, True), (2, False)]:
            self.store.record_shadow_observation("default", "llm-review", str(number), "shadow", {},
                {"prompt_version": 2, "release_id": dep["release_id"]}, 0, failed)
        dep = self.store.get_deployment("default", "llm-review")
        self.assertEqual("rolled_back", dep["status"])
        self.assertEqual(1, dep["shadow_errors"])
        self.assertEqual(1, self.console.status("default")["current_version"])

    def test_queue_submission_failure_does_not_fail_primary(self):
        self.action("shadow")
        with patch.object(self.service.queue, "submit", side_effect=RuntimeError("queue unavailable")):
            result = self.service.create_review("org/repo", DIFF)
        self.assertEqual("SUCCESS", result["state"])
        rows = self.wait_observation()
        self.assertTrue(rows[0]["candidate_failed"])

    def test_invalid_shadow_limits_and_non_boolean_auto_promote(self):
        for fields in ({"min_samples": 0}, {"max_error_rate": float("nan")},
                       {"max_disagreement_rate": 2}, {"auto_promote": "false"}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.action("shadow", **fields)

    def test_worker_failure_counts_as_shadow_error_even_when_task_succeeds(self):
        self.action("shadow")
        original = self.client.complete_json

        def fail_worker(role, system, *args, **kwargs):
            if role == "security" and CANDIDATE in system:
                raise RuntimeError("candidate worker failed")
            return original(role, system, *args, **kwargs)

        self.client.complete_json = fail_worker
        self.service.create_review("org/repo", DIFF)
        row = self.wait_observation()[0]
        self.assertEqual("SUCCESS", row["candidate"]["state"])
        self.assertTrue(row["candidate_failed"])
        self.assertEqual(1, row["candidate"]["failed_workers"])

    def test_disagreement_prevents_automatic_activation(self):
        self.action("shadow", min_samples=1, auto_promote=True, max_disagreement_rate=0)
        dep = self.store.get_deployment("default", "llm-review")
        self.store.record_shadow_observation("default", "llm-review", "different", "shadow", {},
            {"prompt_version": 2, "release_id": dep["release_id"]}, 1, False)
        self.assertEqual("running", self.store.get_deployment("default", "llm-review")["status"])
        self.assertEqual(1, self.console.current_version("default"))

    def test_publishing_another_candidate_resets_the_previous_trial_metadata(self):
        self.action("shadow")
        self.service.create_review("org/repo", DIFF)
        self.wait_observation()
        self.action("activate")
        old_release = self.store.get_deployment("default", "llm-review")
        self.store.save_skill_version("llm-review", "next candidate", 1.)
        self.store.activate_prompt_release("default", "llm-review", 3, 2)
        release = self.store.get_deployment("default", "llm-review")
        self.assertEqual((3, 3, 2), (release["stable_version"], release["candidate_version"], release["previous_version"]))
        self.assertEqual(0, release["shadow_samples"])
        self.assertNotEqual(old_release["release_id"], release["release_id"])
        self.assertEqual([], self.console.status("default")["observations"])
        # Retrying activation must preserve the real rollback target.
        self.store.activate_prompt_release("default", "llm-review", 3, 3)
        self.assertEqual(2, self.store.get_deployment("default", "llm-review")["previous_version"])


if __name__ == "__main__":
    unittest.main()
