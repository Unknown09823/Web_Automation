"""FastAPI backend for the automation framework.

The API is the single source of truth for remote control. All other
control channels (SSH CLI, Telegram, dashboard) are clients of this API.
"""
from automation.api.app import create_app, AppContext

__all__ = ["create_app", "AppContext"]
