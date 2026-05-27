"""Workflow engine: declarative steps with optional AI adaptation.

A workflow is a list of typed steps. The engine runs them in order against a
``Playwright`` page (and an ``AIBrain`` if available). Workflows are loaded
from JSON or YAML files; nothing is hardcoded.

Step types
----------
* ``navigate``  — go to a URL.
* ``analyze``   — let the brain perceive the current page.
* ``find``      — locate an element by intent (and store the result).
* ``act``       — click / fill / check / select using a found element.
* ``ai_goal``   — let the brain plan + execute a high-level goal.
* ``wait``      — wait for selector / load state / fixed time.
* ``screenshot`` — capture a screenshot.
* ``verify``    — check URL / title / element presence.
* ``branch``    — conditional jump within the workflow.
* ``set``       — bind a variable in the workflow context.
* ``log``       — emit a structured log line.

This module lives under ``controllers`` for naming reasons (it controls the
flow). It is the only piece of code that interprets workflow definitions.
"""
from automation.controllers.workflow_engine import (
    WorkflowEngine,
    Workflow,
    WorkflowStep,
    WorkflowResult,
    WorkflowStatus,
    load_workflow,
)

__all__ = [
    "WorkflowEngine",
    "Workflow",
    "WorkflowStep",
    "WorkflowResult",
    "WorkflowStatus",
    "load_workflow",
]
