"""Shared transactional prompt release operations for SQLite and PostgreSQL."""
import json
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


class PromptReleaseStore:
    def _release_sql(self, conn, sql, args=()):
        return conn.execute(sql.replace("?", "%s") if hasattr(self, "psycopg") else sql, args)

    def _release_row(self, conn, tenant, skill):
        suffix = " FOR UPDATE" if hasattr(self, "psycopg") else ""
        row = self._release_sql(conn,
            "SELECT * FROM deployments WHERE tenant_id=? AND skill_name=?" + suffix,
            (tenant, skill)).fetchone()
        return dict(row) if row else None

    def _set_release_metadata(self, conn, tenant, skill, config):
        self._release_sql(conn,
            "UPDATE deployments SET max_disagreement_rate=?,auto_promote=?,"
            "shadow_samples=0,disagreements=0,shadow_errors=0,release_id=?,previous_version=? "
            "WHERE tenant_id=? AND skill_name=?",
            (float(config.get("max_disagreement_rate", .2)), int(bool(config.get("auto_promote", False))),
             str(uuid.uuid4()), config.get("previous_version", config.get("stable_version")), tenant, skill))

    def _activate_release(self, conn, tenant, skill, version, previous, status):
        if version is not None and not self._release_sql(conn,
                "SELECT 1 FROM skill_versions WHERE skill_name=? AND version=?", (skill, version)).fetchone():
            raise ValueError("prompt version does not exist")
        release = self._release_row(conn, tenant, skill)
        if release and status == "promoted" and release["candidate_version"] != version:
            self._set_release_metadata(conn, tenant, skill, {**release, "previous_version": previous})
        self._release_sql(conn, "UPDATE skill_versions SET active=FALSE WHERE skill_name=?", (skill,))
        if version is not None:
            self._release_sql(conn, "UPDATE skill_versions SET active=TRUE WHERE skill_name=? AND version=?",
                              (skill, version))
        self._release_sql(conn,
            "INSERT INTO deployments(tenant_id,skill_name,stable_version,candidate_version,status,"
            "updated_at,previous_version,release_id) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(tenant_id,skill_name) DO UPDATE SET stable_version=excluded.stable_version,"
            "candidate_version=CASE WHEN excluded.status='promoted' THEN excluded.candidate_version "
            "ELSE deployments.candidate_version END,"
            "previous_version=excluded.previous_version,status=excluded.status,canary_percent=0,"
            "shadow_percent=0,auto_promote=0,updated_at=excluded.updated_at",
            (tenant, skill, version, version, status, now(), previous, str(uuid.uuid4())))

    def activate_prompt_release(self, tenant, skill, version, previous=None, status="promoted"):
        with getattr(self, "_lock", nullcontext()), self._connect() as conn:
            release = self._release_row(conn, tenant, skill)
            if (release and status == "promoted" and release["status"] == "promoted"
                    and release["stable_version"] == version):
                return release
            self._activate_release(conn, tenant, skill, version, previous, status)
        return self.get_deployment(tenant, skill)

    def stop_prompt_release(self, tenant, skill):
        with getattr(self, "_lock", nullcontext()), self._connect() as conn:
            self._release_sql(conn,
                "UPDATE deployments SET status='stopped',canary_percent=0,shadow_percent=0,"
                "auto_promote=0,updated_at=? WHERE tenant_id=? AND skill_name=?", (now(), tenant, skill))
        return self.get_deployment(tenant, skill)

    def record_shadow_observation(self, tenant_id, skill_name, task_id, lane, primary,
                                  candidate, disagreement, candidate_failed=False):
        with getattr(self, "_lock", nullcontext()), self._connect() as conn:
            release = self._release_row(conn, tenant_id, skill_name)
            if not release or release["status"] != "running":
                return None
            candidate = candidate or {}
            # Delayed work from an older/replaced trial must not advance this trial.
            if candidate.get("release_id") and candidate["release_id"] != release["release_id"]:
                return None
            if candidate.get("prompt_version") is not None and candidate["prompt_version"] != release["candidate_version"]:
                return None
            duplicate = self._release_sql(conn,
                "SELECT 1 FROM release_observations WHERE tenant_id=? AND skill_name=? AND task_id=? AND lane=?",
                (tenant_id, skill_name, task_id, lane)).fetchone()
            if duplicate:
                return release
            self._release_sql(conn,
                "INSERT INTO release_observations(tenant_id,skill_name,task_id,lane,primary_json,"
                "candidate_json,disagreement,candidate_failed,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (tenant_id, skill_name, task_id, lane, json.dumps(primary, ensure_ascii=False),
                 json.dumps(candidate, ensure_ascii=False), float(disagreement), int(candidate_failed), now()))
            self._release_sql(conn,
                "UPDATE deployments SET shadow_samples=shadow_samples+1,shadow_errors=shadow_errors+?,"
                "disagreements=disagreements+?,updated_at=? WHERE tenant_id=? AND skill_name=?",
                (int(candidate_failed), int(disagreement > 0), now(), tenant_id, skill_name))
            release = self._release_row(conn, tenant_id, skill_name)
            samples = release["shadow_samples"]
            if samples >= release["min_samples"]:
                error_rate = release["shadow_errors"] / samples
                disagreement_rate = release["disagreements"] / samples
                if error_rate > release["max_error_rate"]:
                    self._release_sql(conn,
                        "UPDATE deployments SET status='rolled_back',shadow_percent=0,canary_percent=0,"
                        "auto_promote=0 WHERE tenant_id=? AND skill_name=?", (tenant_id, skill_name))
                elif (release["auto_promote"] and not candidate_failed
                      and disagreement_rate <= release["max_disagreement_rate"]):
                    self._activate_release(conn, tenant_id, skill_name, release["candidate_version"],
                                           release["stable_version"], "promoted")
            return self._release_row(conn, tenant_id, skill_name)

    def list_release_observations(self, tenant_id, skill_name, limit=100):
        with self._connect() as conn:
            rows = self._release_sql(conn,
                "SELECT * FROM release_observations WHERE tenant_id=? AND skill_name=? ORDER BY id DESC LIMIT ?",
                (tenant_id, skill_name, max(1, min(int(limit), 500)))).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["primary"] = json.loads(item.pop("primary_json"))
            raw = item.pop("candidate_json")
            item["candidate"] = json.loads(raw) if raw else None
            result.append(item)
        return result
