"""Browser subsystem: Playwright-based, profile-isolated, recoverable."""
from automation.browser.manager import (
    BrowserManager,
    BrowserSession,
    BrowserConfig,
    BrowserOverrides,
)

__all__ = ["BrowserManager", "BrowserSession", "BrowserConfig", "BrowserOverrides"]
