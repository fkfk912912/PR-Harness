import os
import re
from dataclasses import dataclass
from typing import Dict, Iterable, Optional

from pr_harness.core.paths import PACKAGE_ROOT, PROJECT_ROOT


_DOTENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_dotenv(paths: Optional[Iterable[str]] = None) -> None:
    """Load local dotenv files without overriding real process environment values.

    The project-root file has priority over ``pr_harness/.env``.  This allows the
    latter to remain compatible with existing local setups while keeping the
    conventional root-level ``.env`` as the recommended location.
    """
    package_dir = str(PACKAGE_ROOT)
    project_root = str(PROJECT_ROOT)
    candidates = list(paths) if paths is not None else [
        os.path.join(project_root, ".env"),
        os.path.join(package_dir, ".env"),
    ]
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                lines = handle.readlines()
        except OSError:
            continue
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                continue
            key, value = (part.strip() for part in line.split("=", 1))
            if not _DOTENV_KEY.fullmatch(key):
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ.setdefault(key, value)


load_dotenv()


def _int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError("%s must be positive" % name)
    return value


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _non_negative_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 0:
        raise ValueError("%s must be non-negative" % name)
    return value


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    db_path: str
    max_diff_bytes: int
    max_steps: int
    timeout_seconds: int
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    github_webhook_secret: str
    github_token: str
    auto_post_review: bool
    database_url: str = ""
    redis_url: str = ""
    async_workers: int = 2
    memory_enabled: bool = True
    memory_recall_limit: int = 6
    memory_working_ttl_seconds: int = 86400
    skills_dir: str = "skills"
    github_app_id: str = ""
    github_app_slug: str = ""
    github_private_key_path: str = ""
    public_base_url: str = "http://127.0.0.1:8080"
    llm_provider: str = "local"
    deepseek_api_key: str = ""
    openrouter_api_key: str = ""
    openrouter_site_url: str = ""
    openrouter_app_name: str = "PR Harness"
    eval_max_cases: int = 5
    eval_min_cases: int = 3
    eval_min_improvement: float = 0.01
    eval_min_holdout_cases: int = 2
    eval_max_metric_regression: float = 0.0
    auth_required: bool = False
    auth_secret: str = ""
    bootstrap_admin_username: str = ""
    bootstrap_admin_password: str = ""
    default_tenant_id: str = "default"
    session_ttl_seconds: int = 3600
    webhook_max_age_seconds: int = 600
    queue_max_attempts: int = 3
    queue_lease_seconds: int = 60
    repair_test_command: str = ""
    repair_verify_timeout_seconds: int = 120
    otel_endpoint: str = ""
    otel_service_name: str = "pr_harness"
    alert_failure_rate: float = 0.20
    alert_min_samples: int = 10
    alert_window_seconds: int = 900
    alert_webhook_url: str = ""
    alert_smtp_host: str = ""
    alert_email_to: str = ""
    continuous_eval_seconds: int = 0
    agent_token_budget: int = 8000
    agent_time_budget_seconds: int = 60
    agent_context_window_tokens: int = 32768
    agent_context_input_tokens: int = 20000
    context_diff_token_budget: int = 12000
    context_observation_token_budget: int = 4000
    context_recent_observations: int = 2
    context_map_chunk_tokens: int = 3000
    enabled_agents: str = "lead,security,correctness-reliability,critic"
    llm_input_cost_per_million: float = 0.0
    llm_output_cost_per_million: float = 0.0
    evaluation_min_public_prs: int = 300
    evaluation_min_f1_improvement: float = 0.03
    evaluation_min_high_risk_improvement: float = 0.05

    def resolved_llm(self) -> Dict[str, object]:
        """Resolve a named provider to the existing OpenAI-compatible transport."""
        provider = self.llm_provider.strip().lower()
        if provider in {"", "local", "none"}:
            if self.llm_base_url or self.llm_api_key or self.llm_model:
                provider = "custom"
            else:
                return {}

        if provider == "deepseek":
            api_key = self.deepseek_api_key or self.llm_api_key
            if not api_key:
                raise ValueError("DeepSeek requires PR_HARNESS_DEEPSEEK_API_KEY")
            return {
                "provider": "deepseek",
                "base_url": self.llm_base_url or "https://api.deepseek.com",
                "api_key": api_key,
                "model": self.llm_model or "deepseek-v4-flash",
                "headers": {},
            }

        if provider in {"openrouter-deepseek-free", "openrouter_deepseek_free"}:
            api_key = self.openrouter_api_key or self.llm_api_key
            if not api_key:
                raise ValueError("OpenRouter requires PR_HARNESS_OPENROUTER_API_KEY")
            headers = {}
            if self.openrouter_site_url:
                headers["HTTP-Referer"] = self.openrouter_site_url
            if self.openrouter_app_name:
                headers["X-Title"] = self.openrouter_app_name
            return {
                "provider": "openrouter-deepseek-free",
                "base_url": self.llm_base_url or "https://openrouter.ai/api/v1",
                "api_key": api_key,
                "model": self.llm_model or "deepseek/deepseek-chat-v3-0324:free",
                "headers": headers,
            }

        if provider == "openrouter-free":
            api_key = self.openrouter_api_key or self.llm_api_key
            if not api_key:
                raise ValueError("OpenRouter requires PR_HARNESS_OPENROUTER_API_KEY")
            headers = {}
            if self.openrouter_site_url:
                headers["HTTP-Referer"] = self.openrouter_site_url
            if self.openrouter_app_name:
                headers["X-Title"] = self.openrouter_app_name
            return {
                "provider": "openrouter-free",
                "base_url": self.llm_base_url or "https://openrouter.ai/api/v1",
                "api_key": api_key,
                "model": self.llm_model or "openrouter/free",
                "headers": headers,
            }

        if provider == "custom":
            if not (self.llm_base_url and self.llm_api_key and self.llm_model):
                raise ValueError(
                    "Custom LLM requires PR_HARNESS_LLM_BASE_URL, "
                    "PR_HARNESS_LLM_API_KEY and PR_HARNESS_LLM_MODEL"
                )
            return {
                "provider": "custom",
                "base_url": self.llm_base_url,
                "api_key": self.llm_api_key,
                "model": self.llm_model,
                "headers": {},
            }
        raise ValueError("unsupported PR_HARNESS_LLM_PROVIDER: %s" % self.llm_provider)

    def validate_evolution(self) -> None:
        if self.eval_min_cases > self.eval_max_cases:
            raise ValueError("PR_HARNESS_EVAL_MIN_CASES cannot exceed PR_HARNESS_EVAL_MAX_CASES")
        if not 0.0 <= self.eval_min_improvement <= 1.0:
            raise ValueError("PR_HARNESS_EVAL_MIN_IMPROVEMENT must be between 0 and 1")
        if self.eval_min_holdout_cases > self.eval_max_cases:
            raise ValueError("PR_HARNESS_EVAL_MIN_HOLDOUT_CASES cannot exceed PR_HARNESS_EVAL_MAX_CASES")
        if not 0.0 <= self.eval_max_metric_regression <= 1.0:
            raise ValueError("PR_HARNESS_EVAL_MAX_METRIC_REGRESSION must be between 0 and 1")
        if self.auth_required and len(self.auth_secret.encode("utf-8")) < 32:
            raise ValueError(
                "PR_HARNESS_AUTH_SECRET must contain at least 32 bytes when authentication is enabled"
            )
        if bool(self.bootstrap_admin_username) != bool(self.bootstrap_admin_password):
            raise ValueError("bootstrap admin username and password must be configured together")
        if not 0.0 <= self.alert_failure_rate <= 1.0:
            raise ValueError("PR_HARNESS_ALERT_FAILURE_RATE must be between 0 and 1")
        if self.llm_input_cost_per_million < 0 or self.llm_output_cost_per_million < 0:
            raise ValueError("LLM token prices cannot be negative")
        if self.evaluation_min_public_prs < 300:
            raise ValueError("PR_HARNESS_EVALUATION_MIN_PUBLIC_PRS must be at least 300")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.getenv("PR_HARNESS_HOST", "127.0.0.1"),
            port=_int("PR_HARNESS_PORT", 8080),
            db_path=os.getenv("PR_HARNESS_DB_PATH", "pr_harness.db"),
            max_diff_bytes=_int("PR_HARNESS_MAX_DIFF_BYTES", 1024 * 1024),
            max_steps=_int("PR_HARNESS_MAX_STEPS", 8),
            timeout_seconds=_int("PR_HARNESS_TIMEOUT_SECONDS", 120),
            llm_base_url=os.getenv("PR_HARNESS_LLM_BASE_URL", "").rstrip("/"),
            llm_api_key=os.getenv("PR_HARNESS_LLM_API_KEY", ""),
            llm_model=os.getenv("PR_HARNESS_LLM_MODEL", ""),
            github_webhook_secret=os.getenv("PR_HARNESS_GITHUB_WEBHOOK_SECRET", ""),
            github_token=os.getenv("PR_HARNESS_GITHUB_TOKEN", ""),
            auto_post_review=_bool("PR_HARNESS_AUTO_POST_REVIEW"),
            database_url=os.getenv("PR_HARNESS_DATABASE_URL", ""),
            redis_url=os.getenv("PR_HARNESS_REDIS_URL", ""),
            async_workers=_int("PR_HARNESS_ASYNC_WORKERS", 2),
            memory_enabled=_bool("PR_HARNESS_MEMORY_ENABLED", True),
            memory_recall_limit=_int("PR_HARNESS_MEMORY_RECALL_LIMIT", 6),
            memory_working_ttl_seconds=_int(
                "PR_HARNESS_MEMORY_WORKING_TTL_SECONDS", 86400
            ),
            skills_dir=os.getenv("PR_HARNESS_SKILLS_DIR", "skills"),
            github_app_id=os.getenv("PR_HARNESS_GITHUB_APP_ID", ""),
            github_app_slug=os.getenv("PR_HARNESS_GITHUB_APP_SLUG", ""),
            github_private_key_path=os.getenv("PR_HARNESS_GITHUB_PRIVATE_KEY_PATH", ""),
            public_base_url=os.getenv("PR_HARNESS_PUBLIC_BASE_URL", "http://127.0.0.1:8080").rstrip("/"),
            llm_provider=os.getenv("PR_HARNESS_LLM_PROVIDER", "local"),
            deepseek_api_key=os.getenv("PR_HARNESS_DEEPSEEK_API_KEY", ""),
            openrouter_api_key=os.getenv("PR_HARNESS_OPENROUTER_API_KEY", ""),
            openrouter_site_url=os.getenv("PR_HARNESS_OPENROUTER_SITE_URL", ""),
            openrouter_app_name=os.getenv("PR_HARNESS_OPENROUTER_APP_NAME", "PR Harness"),
            eval_max_cases=_int("PR_HARNESS_EVAL_MAX_CASES", 5),
            eval_min_cases=_int("PR_HARNESS_EVAL_MIN_CASES", 3),
            eval_min_improvement=float(os.getenv("PR_HARNESS_EVAL_MIN_IMPROVEMENT", "0.01")),
            eval_min_holdout_cases=_non_negative_int("PR_HARNESS_EVAL_MIN_HOLDOUT_CASES", 2),
            eval_max_metric_regression=float(
                os.getenv("PR_HARNESS_EVAL_MAX_METRIC_REGRESSION", "0")
            ),
            auth_required=_bool("PR_HARNESS_AUTH_REQUIRED", False),
            auth_secret=os.getenv("PR_HARNESS_AUTH_SECRET", ""),
            bootstrap_admin_username=os.getenv("PR_HARNESS_BOOTSTRAP_ADMIN_USERNAME", ""),
            bootstrap_admin_password=os.getenv("PR_HARNESS_BOOTSTRAP_ADMIN_PASSWORD", ""),
            default_tenant_id=os.getenv("PR_HARNESS_DEFAULT_TENANT_ID", "default"),
            session_ttl_seconds=_int("PR_HARNESS_SESSION_TTL_SECONDS", 3600),
            webhook_max_age_seconds=_int("PR_HARNESS_WEBHOOK_MAX_AGE_SECONDS", 600),
            queue_max_attempts=_int("PR_HARNESS_QUEUE_MAX_ATTEMPTS", 3),
            queue_lease_seconds=_int("PR_HARNESS_QUEUE_LEASE_SECONDS", 60),
            repair_test_command=os.getenv("PR_HARNESS_REPAIR_TEST_COMMAND", ""),
            repair_verify_timeout_seconds=_int("PR_HARNESS_REPAIR_VERIFY_TIMEOUT_SECONDS", 120),
            otel_endpoint=os.getenv("PR_HARNESS_OTEL_ENDPOINT", ""),
            otel_service_name=os.getenv("PR_HARNESS_OTEL_SERVICE_NAME", "pr_harness"),
            alert_failure_rate=float(os.getenv("PR_HARNESS_ALERT_FAILURE_RATE", "0.20")),
            alert_min_samples=_int("PR_HARNESS_ALERT_MIN_SAMPLES", 10),
            alert_window_seconds=_int("PR_HARNESS_ALERT_WINDOW_SECONDS", 900),
            alert_webhook_url=os.getenv("PR_HARNESS_ALERT_WEBHOOK_URL", ""),
            alert_smtp_host=os.getenv("PR_HARNESS_ALERT_SMTP_HOST", ""),
            alert_email_to=os.getenv("PR_HARNESS_ALERT_EMAIL_TO", ""),
            continuous_eval_seconds=_non_negative_int(
                "PR_HARNESS_CONTINUOUS_EVAL_SECONDS", 0
            ),
            agent_token_budget=_int("PR_HARNESS_AGENT_TOKEN_BUDGET", 8000),
            agent_time_budget_seconds=_int("PR_HARNESS_AGENT_TIME_BUDGET_SECONDS", 60),
            agent_context_window_tokens=_int(
                "PR_HARNESS_AGENT_CONTEXT_WINDOW_TOKENS", 32768
            ),
            agent_context_input_tokens=_int(
                "PR_HARNESS_AGENT_CONTEXT_INPUT_TOKENS", 20000
            ),
            context_diff_token_budget=_int(
                "PR_HARNESS_CONTEXT_DIFF_TOKEN_BUDGET", 12000
            ),
            context_observation_token_budget=_int(
                "PR_HARNESS_CONTEXT_OBSERVATION_TOKEN_BUDGET", 4000
            ),
            context_recent_observations=_non_negative_int(
                "PR_HARNESS_CONTEXT_RECENT_OBSERVATIONS", 2
            ),
            context_map_chunk_tokens=_int(
                "PR_HARNESS_CONTEXT_MAP_CHUNK_TOKENS", 3000
            ),
            enabled_agents=os.getenv(
                "PR_HARNESS_ENABLED_AGENTS",
                "lead,security,correctness-reliability,critic",
            ),
            llm_input_cost_per_million=float(
                os.getenv("PR_HARNESS_LLM_INPUT_COST_PER_MILLION", "0")
            ),
            llm_output_cost_per_million=float(
                os.getenv("PR_HARNESS_LLM_OUTPUT_COST_PER_MILLION", "0")
            ),
            evaluation_min_public_prs=_int(
                "PR_HARNESS_EVALUATION_MIN_PUBLIC_PRS", 300
            ),
            evaluation_min_f1_improvement=float(
                os.getenv("PR_HARNESS_EVALUATION_MIN_F1_IMPROVEMENT", "0.03")
            ),
            evaluation_min_high_risk_improvement=float(
                os.getenv("PR_HARNESS_EVALUATION_MIN_HIGH_RISK_IMPROVEMENT", "0.05")
            ),
        )
