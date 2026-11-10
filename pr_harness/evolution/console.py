"""Explicit candidate approval and observable prompt releases for the dashboard."""
import difflib
import threading

from pr_harness.evolution.prompt_evolution import DEFAULT_PROMPT


class EvolutionConsole:
    skill = "llm-review"

    def __init__(self, service):
        self.service = service
        self.store = service.store
        self.lock = threading.RLock()

    def current_version(self, tenant):
        deployment = self.store.get_deployment(tenant, self.skill)
        if deployment:
            return deployment["stable_version"]
        active = self.store.get_active_skill_version(self.skill)
        return active["version"] if active else None

    def status(self, tenant, can_manage=False):
        versions = self.store.list_skill_versions(self.skill)
        by_version = {v["version"]: v for v in versions}
        runs = []
        for source in self.store.list_evolution_runs(200):
            if source["skill_name"] != self.skill:
                continue
            run = dict(source)
            candidate = by_version.get(run["candidate_version"], {})
            baseline = by_version.get(run.get("baseline_version"), {})
            run["candidate_prompt"] = candidate.get("prompt", "")
            run["baseline_prompt"] = baseline.get("prompt", DEFAULT_PROMPT)
            run["prompt_diff"] = "".join(difflib.unified_diff(
                [line + "\n" for line in run["baseline_prompt"].splitlines()],
                [line + "\n" for line in run["candidate_prompt"].splitlines()],
                fromfile="评测基准", tofile="候选 V%s" % run["candidate_version"]))
            runs.append(run)
        deployment = self.store.get_deployment(tenant, self.skill)
        observations = self.store.list_release_observations(tenant, self.skill, 100)
        if deployment:
            observations = [o for o in observations if
                            (o.get("candidate") or {}).get("release_id") == deployment.get("release_id")]
        return {
            "current_version": self.current_version(tenant), "deployment": deployment,
            "runs": runs[:50], "observations": observations, "can_manage": can_manage,
        }

    def action(self, tenant, payload):
        if payload.get("confirmed") is not True:
            raise ValueError("请先确认这次版本操作")
        with self.lock:
            current = self.current_version(tenant)
            deployment = self.store.get_deployment(tenant, self.skill)
            if ("expected_current_version" in payload
                    and payload["expected_current_version"] != current):
                raise ValueError("正式版本已变化，请刷新页面后重新确认")
            if ("expected_release_id" in payload
                    and payload["expected_release_id"] != (deployment or {}).get("release_id")):
                raise ValueError("试运行状态已变化，请刷新页面后重新确认")
            action = payload.get("action")
            if action in {"shadow", "activate"}:
                version = payload.get("version")
                if isinstance(version, bool) or not isinstance(version, int):
                    raise ValueError("请选择有效的候选版本")
                run = next((r for r in self.store.list_evolution_runs(200)
                            if r["skill_name"] == self.skill and r["candidate_version"] == version), None)
                gates = ((run or {}).get("metrics") or {}).get("gates") or {}
                if not run or run["decision"] not in {"shadow_ready", "activated"} or not gates or not all(gates.values()):
                    raise ValueError("候选未通过离线评测，不能开始试运行或启用")
                if not self.store.get_skill_version(self.skill, version):
                    raise ValueError("候选版本不存在")
                if version == current:
                    raise ValueError("这个版本已经是当前正式版本")
                if run.get("baseline_version") != current:
                    raise ValueError("候选的评测基准与当前正式版本不同，请重新评测")
                if action == "shadow":
                    if deployment and deployment["status"] == "running":
                        raise ValueError("已有试运行，请先停止后再开始新的试运行")
                    config = {
                        "stable_version": current, "candidate_version": version,
                        "shadow_percent": payload.get("shadow_percent", 100), "canary_percent": 0,
                        "min_samples": payload.get("min_samples", 5),
                        "max_error_rate": payload.get("max_error_rate", .1),
                        "max_disagreement_rate": payload.get("max_disagreement_rate", .2),
                        "auto_promote": payload.get("auto_promote", False),
                    }
                    self.service.releases.configure(tenant, self.skill, config)
                else:
                    self.store.activate_prompt_release(tenant, self.skill, version, current)
                    self.service.reload_skills()
            elif action == "stop":
                if not deployment or deployment["status"] != "running":
                    raise ValueError("当前没有正在运行的试用")
                self.store.stop_prompt_release(tenant, self.skill)
            elif action == "rollback":
                if not deployment or deployment["status"] != "promoted":
                    raise ValueError("当前没有可以回退的已发布版本")
                self.store.activate_prompt_release(
                    tenant, self.skill, deployment.get("previous_version"), current, "rolled_back")
                self.service.reload_skills()
            else:
                raise ValueError("不支持的版本操作")
        return self.status(tenant, True)
