"""API route modules."""
from automation.api.routes import (
    status as status_routes,
    plugins as plugins_routes,
    config as config_routes,
    tasks as tasks_routes,
    logs as logs_routes,
    accounts as accounts_routes,
    workflows as workflows_routes,
    metrics as metrics_routes,
    ai as ai_routes,
    control as control_routes,
    distributed as distributed_routes,
)

__all__ = [
    "status_routes",
    "plugins_routes",
    "config_routes",
    "tasks_routes",
    "logs_routes",
    "accounts_routes",
    "workflows_routes",
    "metrics_routes",
    "ai_routes",
    "control_routes",
    "distributed_routes",
]
