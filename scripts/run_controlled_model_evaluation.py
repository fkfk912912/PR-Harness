"""Replay labelled controlled diffs with real models and resumable case records."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pr_harness.agents.reviewer import CompositeReviewer, LocalRuleReviewer
from pr_harness.core.config import Settings
from pr_harness.core.llm import JsonChatClient
from pr_harness.evaluation.benchmark import (
    ProductionEvaluationHarness,
    experiment_reviewer_factories,
    paired_bootstrap_comparison,
    validate_real_dataset,
)
from pr_harness.evaluation.harness import (
    FixtureRepairer, dataset_fingerprint, load_jsonl,
)
from pr_harness.review.rules import ContextRuleReviewer


ARMS = ("single-llm", "single-llm-scanner", "multi-llm-no-critic", "full-agentic")
REQUEST_SLOTS = threading.BoundedSemaphore(4)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(".tmp")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pending.replace(path)


class AuditedClient(JsonChatClient):
    """Use the original JSON request contract and save every actual API attempt."""

    def __init__(self, profile, directory, timeout, thinking_mode, retries):
        super().__init__(
            profile["base_url"], profile["api_key"], profile["model"],
            profile["provider"], timeout, profile.get("headers"),
        )
        self.directory = directory
        self.thinking_mode = thinking_mode
        self.retries = retries
        self.calls = []
        self.lock = threading.Lock()
        self.sequence = 0

    def complete_json(self, role, system, user, ledger=None, max_tokens=None):
        payload = {
            "model": self.model, "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
        }
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        if self.provider == "deepseek" and self.thinking_mode == "disabled":
            payload["thinking"] = {"type": "disabled"}
        headers = {
            "Authorization": "Bearer " + self.api_key,
            "Content-Type": "application/json", "Accept": "application/json",
            **self.extra_headers,
        }
        for attempt in range(self.retries + 1):
            with self.lock:
                self.sequence += 1
                sequence = self.sequence
            started = time.monotonic()
            body, usage, error, result = {}, {}, "", None
            retryable = True
            try:
                request = urllib.request.Request(
                    self.base_url + "/chat/completions",
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers=headers, method="POST",
                )
                with REQUEST_SLOTS:
                    with urllib.request.urlopen(request, timeout=self.timeout) as response:
                        body = json.loads(response.read().decode("utf-8"))
                usage = body.get("usage") or {}
                content = body["choices"][0]["message"]["content"]
                result = json.loads(content)
                if not isinstance(result, dict):
                    raise ValueError("model JSON root is not an object")
            except urllib.error.HTTPError as exc:
                detail = exc.read(1000).decode("utf-8", errors="replace")
                error = "HTTP %d %s" % (exc.code, detail)
                retryable = exc.code == 429 or exc.code >= 500
            except (urllib.error.URLError, socket.timeout, OSError, ValueError,
                    KeyError, IndexError, TypeError) as exc:
                error = "%s %s" % (type(exc).__name__, exc)
            error = error.replace(self.api_key, "[redacted]")
            duration = int((time.monotonic() - started) * 1000)
            record = {
                "sequence": sequence, "role": role, "attempt": attempt + 1,
                "ok": not error, "error": error, "duration_ms": duration,
                "request": payload, "response": body, "usage": usage,
            }
            write_json(self.directory / ("%04d.json" % sequence), record)
            with self.lock:
                self.calls.append(record)
            if ledger:
                ledger.record_model(role, self.provider, self.model, usage, duration, not error, error)
            if not error:
                return result
            if not retryable or attempt == self.retries:
                raise RuntimeError(error)
            time.sleep(2 ** attempt)


class CapturingReviewer:
    def __init__(self, delegate):
        self.delegate = delegate
        self.name = delegate.name
        self.findings = []

    def review_case(self, case, parsed):
        self.findings = self.delegate.review_case(case, parsed)
        return self.findings

    def evaluation_execution(self):
        return self.delegate.evaluation_execution()

    def evaluation_collaboration(self):
        return self.delegate.evaluation_collaboration()


def review_case(arm, case, args, profile):
    client = AuditedClient(
        profile, args.output / "calls" / arm / case["id"] / uuid.uuid4().hex,
        args.timeout, args.thinking_mode, args.retries,
    )
    factory = experiment_reviewer_factories(client, args.time_budget)[arm]
    reviewer = CapturingReviewer(factory(profile["model"], args.token_budget))
    harness = ProductionEvaluationHarness(repairer=FixtureRepairer())
    result = harness.run(reviewer, [case], arm)["case_results"][0]
    # The original harness reads usage only after a successful review. Include
    # failed/retried requests here so their tokens and latency remain visible.
    calls = sorted(client.calls, key=lambda item: item["sequence"])
    result["llm_calls"] = len(calls)
    result["model_roles"] = dict(Counter(item["role"] for item in calls))
    for field, usage_field in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens")):
        result[field] = sum(int(item["usage"].get(usage_field, 0)) for item in calls)
    result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    result["failed_api_attempts"] = sum(not item["ok"] for item in calls)
    result["findings"] = [item.to_dict() for item in reviewer.findings]
    result["collaboration"] = reviewer.evaluation_collaboration()
    result["execution"] = reviewer.evaluation_execution()
    result["api_errors"] = [item["error"] for item in calls if item["error"]]
    result["api_call_files"] = [
        str((client.directory / ("%04d.json" % item["sequence"])).relative_to(args.output))
        for item in calls
    ]
    write_json(args.output / "cases" / arm / (case["id"] + ".json"), result)
    return result


def aggregate(results):
    totals = ProductionEvaluationHarness._empty_totals()
    for result in results:
        ProductionEvaluationHarness._accumulate(totals, result)
    metrics = ProductionEvaluationHarness._metrics(totals)
    # No price is configured for the account. Zero dollars would imply free use.
    metrics["average_cost_usd_per_pr"] = None
    return metrics


def arm_report(name, results):
    return {
        "name": name, "metrics": aggregate(results), "case_results": results,
        "by_split": {
            split: aggregate([item for item in results if item["split"] == split])
            for split in ("validation", "holdout")
        },
    }


def render_summary(report):
    protocol = report["protocol"]
    lines = [
        "# 100 条受控 PR Diff 模型评测", "",
        "模型 %s。数据共 %d 条，风险样本 %d 条，无问题标注样本 %d 条。" % (
            protocol["model"], protocol["cases"], protocol["risk_cases"], protocol["clean_cases"]),
        "Validation / Holdout 为 %s，按仓库隔离。" % protocol["split_counts"],
        "原数据 SHA-256 为 %s。" % protocol["original_file_sha256"], "",
        "沿用项目的一对一匹配、CWE 身份映射和默认 ±2 行容差。已有标注补充 should_comment=true。",
        "四组使用相同模型、temperature=0、JSON 模式、Token 配置和请求重试规则。",
        "DeepSeek 额外思考模式为 %s，每次请求失败后最多重试 %d 次。" % (
            protocol["thinking_mode"], protocol["retries"]),
        "API 调用记录包含失败与重试的 Token。账号价格未配置，美元费用记为未估算。", "",
        "数据来源为 synthetic-controlled。结果适用于这份合成受控集。",
        "修复采用原项目 FixtureRepairer，检查风险模式、编译和要求保留的代码模式。",
        "safe_fix_rate = 修复验证通过数 / 已尝试修复数。",
        "e2e_security_fix_rate = 成功发现且修复的风险样本数 / 全部风险样本数。", "",
    ]
    for split, title in ((None, "全部样本"), ("validation", "Validation"), ("holdout", "Holdout")):
        lines.extend([
            "**%s**" % title, "",
            "| 组别 | Precision | Recall | F1 | 高风险召回 | 干净样本准确率 | 修复验证通过率 | 端到端修复率 | 执行成功率 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ])
        for name, arm in report["arms"].items():
            m = arm["metrics"] if split is None else arm["by_split"][split]
            values = [m[key] for key in (
                "precision", "recall", "f1", "high_risk_recall", "clean_accuracy",
                "safe_fix_rate", "e2e_security_fix_rate", "execution_success_rate",
            )]
            lines.append("| %s | %s |" % (name, " | ".join("%.2f%%" % (100 * v) for v in values)))
        lines.append("")
    lines.extend([
        "**实际计数与用量**", "",
        "| 组别 | TP | FP | FN | 高风险命中/总数 | 干净命中/总数 | 修复通过/尝试 | 模型请求 | Token |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for name, arm in report["arms"].items():
        m = arm["metrics"]
        lines.append("| %s | %d | %d | %d | %d/%d | %d/%d | %d/%d | %d | %d |" % (
            name, m["tp"], m["fp"], m["fn"], m["high_hits"], m["high_total"],
            m["clean_hits"], m["clean_cases"], m["repair_passed"], m["repair_attempted"],
            m["llm_calls"], m["total_tokens"],
        ))
    lines.extend(["", "**执行失败**", ""])
    for name, arm in report["arms"].items():
        failures = [item for item in arm["case_results"] if not item["execution_success"]]
        lines.append("- %s 共 %d 条执行失败。" % (name, len(failures)))
        for item in failures:
            lines.append("  - %s %s" % (item["id"], item["error"]))
    lines.extend([
        "", "逐样本预测、匹配和修复检查保存在 cases 目录，模型请求与响应保存在 calls 目录。",
        "evaluation.json 包含完整指标和配对 Bootstrap 95% 置信区间。",
        "production_claim_allowed=false，真实 PR 泛化效果需要真实数据评测。", "",
    ])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--token-budget", type=int, default=12000)
    parser.add_argument("--time-budget", type=int, default=240)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--thinking-mode", choices=("default", "disabled"), default="disabled")
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--case-ids", default="", help="Comma-separated validation case ids for a smoke run.")
    parser.add_argument("--bootstrap-iterations", type=int, default=2000)
    args = parser.parse_args()
    if args.workers < 1 or args.token_budget < 1024 or args.retries < 0:
        parser.error("workers must be positive, token budget at least 1024, retries nonnegative")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    profile = Settings.from_env().resolved_llm()
    if not profile:
        parser.error("Configure a model in the project's .env")
    original = load_jsonl(str(args.dataset))
    cases = [
        {**case, "expected_findings": [
            {**finding, "should_comment": bool(finding.get("should_comment", True))}
            for finding in case["expected_findings"]
        ]} for case in original
    ]
    if args.case_ids:
        selected = set(args.case_ids.split(","))
        cases = [case for case in cases if case["id"] in selected]
        if {case["id"] for case in cases} != selected or any(case["split"] != "validation" for case in cases):
            parser.error("smoke selection must contain existing validation cases")
    elif len(cases) != 100 or sum(bool(c["expected_findings"]) for c in cases) != 40:
        parser.error("Full controlled evaluation requires 100 cases with 40 risk cases")
    for case in cases:
        if not str(case["id"]).replace("-", "").replace("_", "").isalnum():
            parser.error("case ids must use letters, digits, hyphens or underscores")
    source_files = sorted((ROOT / "pr_harness").rglob("*.py")) + [Path(__file__)]
    source_files.extend(sorted(
        path for path in (ROOT / "skills").rglob("*")
        if path.is_file() and path.suffix in {".md", ".yaml", ".yml", ".json"}
    ))
    protocol = {
        "schema_version": 1, "provider": profile["provider"], "model": profile["model"],
        "base_url": profile["base_url"], "temperature": 0,
        "thinking_mode": args.thinking_mode, "token_budget_per_pr": args.token_budget,
        "time_budget_seconds_per_pr": args.time_budget, "timeout_seconds": args.timeout,
        "retries": args.retries, "workers": args.workers, "line_tolerance": 2,
        "api_request_concurrency": 4,
        "repairer": "FixtureRepairer", "repair_scope": "controlled-pattern-and-compile-checks",
        "arms": list(ARMS), "bootstrap_iterations": args.bootstrap_iterations,
        "original_file_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
        "original_dataset_sha256": dataset_fingerprint(original),
        "evaluated_dataset_sha256": dataset_fingerprint(cases),
        "label_adaptation": "default should_comment=true for existing expected_findings",
        "cases": len(cases), "case_ids": [c["id"] for c in cases],
        "risk_cases": sum(bool(c["expected_findings"]) for c in cases),
        "clean_cases": sum(not c["expected_findings"] for c in cases),
        "split_counts": dict(Counter(c["split"] for c in cases)),
        "source_kinds": sorted({c.get("source", {}).get("kind", "unknown") for c in cases}),
        "source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files},
        "python": platform.python_version(), "cost_estimation": "unconfigured",
    }
    readiness = validate_real_dataset(cases)
    if readiness["repository_overlap"]:
        parser.error("repositories overlap between dataset splits")
    protocol_path = args.output / "protocol.json"
    if protocol_path.exists():
        if json.loads(protocol_path.read_text(encoding="utf-8")) != protocol:
            parser.error("Saved protocol differs. Use a new output directory for a changed experiment")
    else:
        write_json(protocol_path, protocol)
        with zipfile.ZipFile(args.output / "source.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            for path in source_files:
                archive.write(path, path.relative_to(ROOT))
        shutil.copyfile(args.dataset, args.output / "dataset.original.jsonl")
        (args.output / "dataset.jsonl").write_text(
            "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases), encoding="utf-8",
        )
    print("Frozen protocol", args.output, "model", profile["model"], "cases", len(cases), flush=True)
    reports = {}
    for arm in ARMS:
        results = {}
        for case in cases:
            path = args.output / "cases" / arm / (case["id"] + ".json")
            if path.exists():
                result = json.loads(path.read_text(encoding="utf-8"))
                if result["id"] != case["id"] or result["split"] != case["split"]:
                    raise ValueError("Cached case identity differs")
                results[case["id"]] = result
        print(arm, "cached", len(results), flush=True)
        for split in ("validation", "holdout"):
            pending = [c for c in cases if c["split"] == split and c["id"] not in results]
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = {pool.submit(review_case, arm, case, args, profile): case for case in pending}
                for future in as_completed(futures):
                    result = future.result()
                    results[result["id"]] = result
                    write_json(args.output / "progress.json", {
                        "arm": arm, "completed": len(results), "total_per_arm": len(cases),
                        "last_case": result["id"], "updated_at": datetime.now(timezone.utc).isoformat(),
                    })
                    print(arm, len(results), "/", len(cases), result["id"],
                          "ok", result["execution_success"], "TP/FP/FN",
                          result["tp"], result["fp"], result["fn"], flush=True)
                    if any(error.startswith(("HTTP 401 ", "HTTP 402 "))
                           for error in result["api_errors"]):
                        for queued in futures:
                            queued.cancel()
                        raise RuntimeError(
                            "Provider credentials or balance unavailable; saved case records retained"
                        )
        reports[arm] = arm_report(arm, [results[c["id"]] for c in cases])
        write_json(args.output / (arm + ".json"), reports[arm])
    print("Aggregating metrics and paired confidence intervals", flush=True)
    comparisons = {}
    pairs = (("single-llm", "single-llm-scanner"), ("single-llm-scanner", "multi-llm-no-critic"),
             ("multi-llm-no-critic", "full-agentic"), ("single-llm", "full-agentic"))
    for left_name, right_name in pairs:
        comparison = {}
        for split in (None, "holdout"):
            left = reports[left_name]
            right = reports[right_name]
            if split:
                left = {"metrics": left["by_split"][split], "case_results": [c for c in left["case_results"] if c["split"] == split]}
                right = {"metrics": right["by_split"][split], "case_results": [c for c in right["case_results"] if c["split"] == split]}
            comparison[split or "all"] = paired_bootstrap_comparison(
                left, right, args.bootstrap_iterations,
                metrics=("f1", "high_risk_recall", "clean_accuracy", "safe_fix_rate", "e2e_security_fix_rate"),
            )
        comparisons[right_name + "_vs_" + left_name] = comparison
    baseline = ProductionEvaluationHarness(repairer=FixtureRepairer()).run(
        CompositeReviewer([LocalRuleReviewer(), ContextRuleReviewer()]), cases, "controlled-rules",
    )
    report = {
        "schema_version": 1, "execution_mode": "controlled-real-model",
        "protocol": protocol, "dataset_readiness": readiness,
        "production_claim_allowed": False, "arms": reports, "comparisons": comparisons,
        "deterministic_rule_reference": baseline,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json(args.output / "evaluation.json", report)
    (args.output / "summary.md").write_text(render_summary(report), encoding="utf-8")
    write_json(args.output / "progress.json", {"status": "complete", "model_cases": len(cases) * len(ARMS)})
    print("Complete", args.output / "summary.md", flush=True)


if __name__ == "__main__":
    main()
