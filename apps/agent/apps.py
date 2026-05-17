from django.apps import AppConfig


class AgentConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.agent"
    verbose_name = "LLM-pipeline agent"

    def ready(self) -> None:
        # Install the process-wide rate limiter so the pure-Python
        # pipeline layer (which can't import Django) has a real
        # cross-process throttle backing the AGENT_RATE_LIMIT_RPS_PER_DOMAIN
        # contract. See apps.agent.rate_limit_redis for the rationale.
        from apps.agent.pipeline.rate_limit import set_default_limiter
        from apps.agent.rate_limit_redis import build_limiter_from_settings

        set_default_limiter(build_limiter_from_settings())
