"""FastAPI application factory.

The app is wired with an ``AppContext`` containing the live ``Engine`` plus
optional account / browser / brain / workflow components. Routes are loaded
once at import time and are completely independent.
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from automation.api.deps import AppContext

if TYPE_CHECKING:  # pragma: no cover
    from automation.accounts.manager import AccountManager
    from automation.agent.agent import BrowserAgent
    from automation.ai.brain import AIBrain
    from automation.browser.manager import BrowserManager
    from automation.controllers.workflow_engine import WorkflowEngine
    from automation.core.engine import Engine

log = logging.getLogger(__name__)


def create_app(
    engine: "Engine",
    accounts: "AccountManager | None" = None,
    browser: "BrowserManager | None" = None,
    brain: "AIBrain | None" = None,
    workflow_engine: "WorkflowEngine | None" = None,
    agent: "BrowserAgent | None" = None,
    workflows_dir: str = "config/workflows",
    enable_dashboard: bool = True,
) -> FastAPI:
    """Build the FastAPI application."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not os.environ.get("AUTOMATION_API_TOKEN"):
            log.warning(
                "AUTOMATION_API_TOKEN is not set; API runs in OPEN mode "
                "(do not expose publicly)"
            )
        yield

    app = FastAPI(
        title="Automation Framework API",
        version="1.0.0",
        description="Production automation control plane.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.ctx = AppContext(
        engine=engine,
        accounts=accounts,
        browser=browser,
        brain=brain,
        workflow_engine=workflow_engine,
        agent=agent,
        workflows_dir=workflows_dir,
    )

    # access log middleware (lightweight)
    @app.middleware("http")
    async def access_log(request: Request, call_next):
        started = time.time()
        try:
            resp = await call_next(request)
        except Exception:  # noqa: BLE001
            log.exception("request error: %s %s", request.method, request.url.path)
            return JSONResponse({"detail": "internal error"}, status_code=500)
        dur = int((time.time() - started) * 1000)
        log.info(
            "%s %s -> %d (%dms)",
            request.method, request.url.path, resp.status_code, dur,
        )
        return resp

    # mount routes (imported here to keep module import side-effect-free)
    from automation.api.routes import (
        accounts_routes, ai_routes, config_routes, control_routes,
        distributed_routes, logs_routes, metrics_routes, plugins_routes,
        status_routes, tasks_routes, workflows_routes,
    )
    from automation.api.routes import agent as agent_routes
    from automation.api.routes import telemetry as telemetry_routes
    app.include_router(status_routes.router)
    app.include_router(control_routes.router)
    app.include_router(plugins_routes.router)
    app.include_router(config_routes.router)
    app.include_router(tasks_routes.router)
    app.include_router(logs_routes.router)
    app.include_router(accounts_routes.router)
    app.include_router(workflows_routes.router)
    app.include_router(metrics_routes.router)
    app.include_router(ai_routes.router)
    app.include_router(distributed_routes.router)
    app.include_router(agent_routes.router)
    app.include_router(telemetry_routes.router)

    # health/liveness
    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/")
    async def root() -> dict:
        return {
            "name": "automation-framework",
            "version": "1.0.0",
            "docs": "/docs",
            "dashboard": "/dashboard" if enable_dashboard else None,
        }

    if enable_dashboard:
        _mount_dashboard(app)

    return app


def _mount_dashboard(app: FastAPI) -> None:
    """Mount the static dashboard if available on disk."""
    here = Path(__file__).resolve().parent
    static_dir = here.parent / "dashboard" / "static"
    template = here.parent / "dashboard" / "index.html"
    if static_dir.exists():
        app.mount("/dashboard/static", StaticFiles(directory=static_dir), name="dashboard-static")

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard() -> HTMLResponse:
        if template.exists():
            return HTMLResponse(template.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>Dashboard not installed</h1>", status_code=404)
