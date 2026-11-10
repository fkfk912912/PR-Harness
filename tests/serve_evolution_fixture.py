"""Isolated browser fixture. Uses a temporary database and fake model calls only."""
import json
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentic_fake import enable_agentic_service
from pr_harness.api.main import ApiHandler
from pr_harness.core.config import Settings
from pr_harness.core.service import ReviewService
from pr_harness.evolution.prompt_evolution import DEFAULT_PROMPT


def main():
    with tempfile.TemporaryDirectory(prefix="pr-harness-ui-") as directory:
        settings = Settings(host="127.0.0.1", port=0, db_path=str(Path(directory) / "fixture.db"),
            max_diff_bytes=10000, max_steps=8, timeout_seconds=20, llm_base_url="", llm_api_key="",
            llm_model="", github_webhook_secret="", github_token="", auto_post_review=False,
            memory_enabled=False)
        service = enable_agentic_service(ReviewService(settings))
        for version in range(1, 8):
            prompt = DEFAULT_PROMPT + "\nRequire added-line evidence. Never fabricate findings. " + str(version)
            service.store.save_skill_version("llm-review", prompt, 1. if version == 7 else .8)
            metric = {"score": 1., "precision": 1., "recall": 1., "f1": 1., "severity_accuracy": 1.,
                      "high_severity_recall": 1., "clean_accuracy": 1., "cases": 5}
            gates = {key: True for key in ("safety", "validation_dataset_ready", "holdout_dataset_ready",
                "evaluation_success", "validation_improvement", "validation_non_regression", "holdout_non_regression")}
            if version != 7:
                gates["validation_improvement"] = False
            service.store.save_evolution_run({"id": "fixture-%s" % version, "skill_name": "llm-review",
                "candidate_version": version, "baseline_version": None,
                "decision": "shadow_ready" if version == 7 else "rejected", "candidate_score": 1.,
                "baseline_score": .95, "metrics": {"candidate": metric, "baseline": {**metric, "score": .95},
                "candidate_holdout": metric, "baseline_holdout": metric, "gates": gates,
                "reason": "passed" if version == 7 else "score improvement was below the configured threshold"},
                "created_at": "2026-11-10T12:00:0%s+08:00" % version})
        service.store.create("fixture-feedback", "example/repo", None, {})
        service.store.record_failure_case("fixture-feedback", "false_positive",
            {"note": "正常代码误报；文本 <img src=x onerror=alert(1)> 应原样显示。"})

        class Handler(ApiHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                if self.path == "/__fixture_shutdown":
                    self._send_json(200, {"stopping": True})
                    threading.Thread(target=server.shutdown, daemon=True).start()
                else:
                    super().do_POST()

        Handler.settings = settings
        Handler.service = service
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        print("READY_URL=http://127.0.0.1:%s" % server.server_port, flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()
            service.queue.close()


if __name__ == "__main__":
    main()
