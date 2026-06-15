"""Telemetry retention: control how much per-run debug data we keep.

The framework produces a lot of breadcrumbs per run — screenshots
on every step, reasoning entries on every decision, event logs,
and replay JSONs. That's invaluable when debugging, and overwhelming
when the operator just wants to know "did the run succeed?".

This module is the *control plane* for those breadcrumbs:

* a small JSON-backed settings file that persists the operator's
  retention preferences across restarts,
* deterministic cleanup operations callable from the Telegram bot,
  the dashboard, or a periodic task.

Settings model
==============

Three knobs, each independently configurable:

  * **screenshot_limit** — keep the last *N* screenshots per
    account (10 / 50 / 100 / unlimited). The trim is global per
    account: across all runs older than the operator's threshold
    we simply pop the oldest images.
  * **auto_delete_minutes** — execution-event messages older
    than this are removed on the next sweep. ``0`` means "keep
    forever". The Telegram bot rounds operator UI choices
    (5 / 30 / 60) to the value here so persistence is
    canonical regardless of which control set it.
  * **reasoning_limit** — keep at most *N* reasoning entries
    per account, trimmed from the head (oldest first).

The same module also exposes "one-shot clear" operations the
Telegram bot wires to /clear_chat /clear_logs /clear_runs
/clear_screenshots /clear_reasoning. Those are *imperative* and
do **not** read settings — they delete whatever the operator
named in the request.

Safety
======

Cleanup never deletes the run's ``status.json`` or ``plan.json`` —
those are the durable record of *what was attempted*. We only
trim the verbose breadcrumbs around them: screenshots, reasoning
JSONL tails, event logs, and the replay sidecar. A dropped
breadcrumb is recoverable (you can rerun the workflow); a deleted
plan is not.

All operations are best-effort: a missing directory or a file
that's currently being held open returns 0 successes and a note
in the result, never an exception. The Telegram bot can show the
operator "1 of 3 cleanup steps had a partial failure" instead
of crashing the chat handler.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- settings
@dataclass(slots=True)
class RetentionSettings:
    """Persisted operator preferences for retention.

    Numeric fields use ``0`` to mean "unlimited" / "never" — this
    matches the operator-facing Telegram UI ("Never" auto-delete,
    "Unlimited" screenshots) and avoids ``None`` checks downstream.
    """

    screenshot_limit: int = 0          # per account, 0 = unlimited
    reasoning_limit: int = 0           # per account, 0 = unlimited
    auto_delete_minutes: int = 0       # 0 = never auto-delete events
    keep_completed_runs: int = 0       # 0 = keep all completed runs
    last_updated: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RetentionSettings":
        return cls(
            screenshot_limit=int(data.get("screenshot_limit", 0)),
            reasoning_limit=int(data.get("reasoning_limit", 0)),
            auto_delete_minutes=int(data.get("auto_delete_minutes", 0)),
            keep_completed_runs=int(data.get("keep_completed_runs", 0)),
            last_updated=float(data.get("last_updated", 0.0)),
        )

    def normalize(self) -> "RetentionSettings":
        """Clamp values to the operator-visible UI presets.

        Keeps the persisted shape consistent regardless of which
        control surface set it. Negative values become ``0``.
        """
        s = RetentionSettings(
            screenshot_limit=max(0, int(self.screenshot_limit)),
            reasoning_limit=max(0, int(self.reasoning_limit)),
            auto_delete_minutes=max(0, int(self.auto_delete_minutes)),
            keep_completed_runs=max(0, int(self.keep_completed_runs)),
            last_updated=self.last_updated,
        )
        return s


# Operator-visible presets. The Telegram bot renders these as button
# rows; the dashboard shows them as a select. Keeping them here means
# both UIs stay in sync without re-defining the menu twice.
SCREENSHOT_PRESETS: tuple[int, ...] = (10, 50, 100, 0)  # 0 = unlimited
AUTO_DELETE_PRESETS_MIN: tuple[int, ...] = (5, 30, 60, 0)  # 0 = never
REASONING_PRESETS: tuple[int, ...] = (50, 200, 1000, 0)
KEEP_RUNS_PRESETS: tuple[int, ...] = (10, 50, 100, 0)


# ---------------------------------------------------------------- result
@dataclass(slots=True)
class CleanupResult:
    """Outcome of one cleanup operation.

    ``files_removed`` and ``bytes_removed`` are best-effort counters
    — when the filesystem refuses a deletion the file is left in
    place and ``notes`` records why. Operators looking at chat
    output can spot partial failures at a glance.
    """

    operation: str
    files_removed: int = 0
    bytes_removed: int = 0
    runs_touched: int = 0
    accounts_touched: int = 0
    duration_ms: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        # "ok" means we ran without errors, NOT that anything was deleted.
        # An empty directory tree is a valid no-op.
        return not any(n.startswith("error:") for n in self.notes)

    def add(self, other: "CleanupResult") -> "CleanupResult":
        return CleanupResult(
            operation=self.operation,
            files_removed=self.files_removed + other.files_removed,
            bytes_removed=self.bytes_removed + other.bytes_removed,
            runs_touched=self.runs_touched + other.runs_touched,
            accounts_touched=self.accounts_touched + other.accounts_touched,
            duration_ms=self.duration_ms + other.duration_ms,
            notes=list(self.notes) + list(other.notes),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "files_removed": self.files_removed,
            "bytes_removed": self.bytes_removed,
            "runs_touched": self.runs_touched,
            "accounts_touched": self.accounts_touched,
            "duration_ms": self.duration_ms,
            "ok": self.ok,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------- manager
class TelemetryRetention:
    """File-backed retention manager.

    Threading: not thread-safe. The agent owns one instance and
    serializes operations behind its event loop. The Telegram bot
    posts cleanup commands one at a time; the FastAPI handlers run
    inside the same loop.

    Parameters
    ----------
    runs_root:
        Where the framework writes per-run directories. Same value
        :class:`BrowserAgent` uses.
    settings_path:
        Where retention preferences are persisted. Defaults to a
        sibling of the runs directory so a deployment that mounts
        ``/data`` gets settings + runs in the same volume.
    """

    def __init__(
        self,
        *,
        runs_root: str | Path = "data/runs",
        settings_path: str | Path = "data/state/telemetry_settings.json",
    ) -> None:
        self.runs_root = Path(runs_root)
        self.settings_path = Path(settings_path)
        self._settings: RetentionSettings | None = None

    # ---------------------------------------------------------- settings I/O
    @property
    def settings(self) -> RetentionSettings:
        if self._settings is None:
            self._settings = self._load_settings()
        return self._settings

    def update_settings(self, **changes: Any) -> RetentionSettings:
        """Merge ``changes`` into the persisted settings.

        Unknown keys are tolerated and dropped (so a future Telegram
        client sending a field we don't recognize doesn't crash the
        backend). Numeric strings are coerced.
        """
        current = self.settings.to_dict()
        for key, value in changes.items():
            if key not in current:
                continue
            try:
                current[key] = int(value)
            except (TypeError, ValueError):
                continue
        new_settings = RetentionSettings.from_dict(current).normalize()
        new_settings.last_updated = time.time()
        self._settings = new_settings
        self._save_settings(new_settings)
        return new_settings

    def reset_settings(self) -> RetentionSettings:
        """Restore defaults and persist."""
        new = RetentionSettings(last_updated=time.time())
        self._settings = new
        self._save_settings(new)
        return new

    def _load_settings(self) -> RetentionSettings:
        if not self.settings_path.exists():
            return RetentionSettings()
        try:
            data = json.loads(self.settings_path.read_text())
        except (OSError, json.JSONDecodeError):
            log.debug("could not read %s; using defaults", self.settings_path)
            return RetentionSettings()
        return RetentionSettings.from_dict(data).normalize()

    def _save_settings(self, settings: RetentionSettings) -> None:
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            self.settings_path.write_text(
                json.dumps(settings.to_dict(), indent=2, default=str),
            )
        except OSError:  # noqa: BLE001
            log.warning("could not persist retention settings", exc_info=True)

    # ---------------------------------------------------------- one-shots
    # The methods below are imperative — operators ask explicitly via
    # /clear_screenshots, /clear_logs, etc., and we delete on demand.
    # Settings only influence the *automatic* sweeps invoked from
    # ``apply_settings()`` further down.

    def clear_screenshots(
        self,
        *,
        run_id: str | None = None,
        account_id: str | None = None,
        keep_last: int | None = None,
    ) -> CleanupResult:
        """Delete screenshots.

        ``keep_last`` overrides the persisted ``screenshot_limit``
        for this single call. ``None`` means "use the persisted
        value"; ``0`` means "delete every screenshot we find".

        ``run_id`` / ``account_id`` scope the operation. Without
        them the sweep covers every run + account on disk.
        """
        result = CleanupResult(operation="clear_screenshots")
        started = time.time()
        kept_per_account = (
            int(keep_last) if keep_last is not None
            else int(self.settings.screenshot_limit)
        )
        kept_per_account = max(0, kept_per_account)

        for run_dir in self._iter_run_dirs(only_run_id=run_id):
            account_dirs = list(self._iter_account_dirs(run_dir, only_account_id=account_id))
            if not account_dirs:
                continue
            result.runs_touched += 1
            for acc_dir in account_dirs:
                shots_dir = acc_dir / "screenshots"
                if not shots_dir.exists():
                    continue
                files = sorted(
                    (f for f in shots_dir.iterdir() if f.is_file()),
                    key=lambda f: f.stat().st_mtime,
                    reverse=True,  # newest first
                )
                # Keep the first ``kept_per_account`` (newest); drop the rest.
                victims = files if kept_per_account == 0 else files[kept_per_account:]
                if victims:
                    sub = _remove_files(victims)
                    sub.runs_touched = 0  # avoid double-count
                    sub.accounts_touched = 1
                    result = result.add(sub)
                else:
                    result.accounts_touched += 1
        result.duration_ms = _elapsed_ms(started)
        return result

    def clear_reasoning(
        self,
        *,
        run_id: str | None = None,
        account_id: str | None = None,
        keep_last: int | None = None,
    ) -> CleanupResult:
        """Trim reasoning.jsonl files.

        Trimming preserves the *latest* ``keep_last`` lines so the
        operator can still see the tail context. ``keep_last=0``
        truncates the file to zero bytes; the file itself stays
        (so the agent's append-only writer can keep using it).
        """
        result = CleanupResult(operation="clear_reasoning")
        started = time.time()
        kept = (
            int(keep_last) if keep_last is not None
            else int(self.settings.reasoning_limit)
        )
        kept = max(0, kept)

        for run_dir in self._iter_run_dirs(only_run_id=run_id):
            account_dirs = list(self._iter_account_dirs(run_dir, only_account_id=account_id))
            if not account_dirs:
                continue
            result.runs_touched += 1
            for acc_dir in account_dirs:
                path = acc_dir / "reasoning.jsonl"
                if not path.exists():
                    continue
                try:
                    before = path.stat().st_size
                except OSError:
                    before = 0
                lines: list[str] = []
                try:
                    with path.open("r", encoding="utf-8") as f:
                        lines = f.readlines()
                except OSError as exc:
                    result.notes.append(f"error: read {path}: {exc!r}")
                    continue
                tail = lines[-kept:] if kept > 0 else []
                if len(tail) == len(lines):
                    # No trimming necessary.
                    result.accounts_touched += 1
                    continue
                try:
                    path.write_text("".join(tail), encoding="utf-8")
                except OSError as exc:
                    result.notes.append(f"error: write {path}: {exc!r}")
                    continue
                try:
                    after = path.stat().st_size
                except OSError:
                    after = 0
                result.bytes_removed += max(0, before - after)
                result.files_removed += 1 if not tail else 0
                result.accounts_touched += 1

        result.duration_ms = _elapsed_ms(started)
        return result

    def clear_logs(
        self,
        *,
        run_id: str | None = None,
        account_id: str | None = None,
    ) -> CleanupResult:
        """Delete the per-account log files and the run-level events.jsonl.

        Status / plan files are preserved so the run remains
        diagnose-able. The next emitted event will create a new
        events.jsonl from scratch, and per-account log files are
        recreated on first append by the logger.
        """
        result = CleanupResult(operation="clear_logs")
        started = time.time()

        for run_dir in self._iter_run_dirs(only_run_id=run_id):
            run_touched = False
            # Per-account logs/
            for acc_dir in self._iter_account_dirs(run_dir, only_account_id=account_id):
                logs_dir = acc_dir / "logs"
                if logs_dir.exists():
                    files = [f for f in logs_dir.iterdir() if f.is_file()]
                    if files:
                        sub = _remove_files(files)
                        sub.runs_touched = 0
                        sub.accounts_touched = 1
                        result = result.add(sub)
                        run_touched = True
                    else:
                        result.accounts_touched += 1
            # Run-level events log
            if account_id is None:
                events = run_dir / "events.jsonl"
                if events.exists():
                    sub = _remove_files([events])
                    sub.runs_touched = 0
                    result = result.add(sub)
                    run_touched = True
            if run_touched:
                result.runs_touched += 1

        result.duration_ms = _elapsed_ms(started)
        return result

    def clear_runs(
        self,
        *,
        keep_last: int | None = None,
        only_completed: bool = True,
        only_run_id: str | None = None,
    ) -> CleanupResult:
        """Remove old run directories entirely.

        ``keep_last`` is the number of *most recent matching* runs to
        preserve; the rest are removed. ``only_completed=True``
        protects active / paused / failed runs from accidental
        deletion — most operators want "clear my finished runs",
        not "wipe everything".

        Active runs (those still listed in the agent's in-memory
        registry) are *never* removed regardless of this flag —
        callers must pass ``run_id`` explicitly to remove a single
        run, in which case we still refuse to delete a directory
        whose ``status.json`` says "running" or "paused".
        """
        result = CleanupResult(operation="clear_runs")
        started = time.time()
        kept = (
            int(keep_last) if keep_last is not None
            else int(self.settings.keep_completed_runs)
        )
        kept = max(0, kept)

        runs = sorted(
            self._iter_run_dirs(only_run_id=only_run_id),
            key=lambda d: d.stat().st_mtime if d.exists() else 0,
            reverse=True,
        )
        if only_run_id:
            # Single-run mode: ignore keep_last; trust the caller.
            victims = runs
        else:
            # Filter to "completed-ish" runs first so we can rank only those.
            eligible = [r for r in runs if not only_completed or _run_is_finished(r)]
            victims = eligible[kept:] if kept > 0 else list(eligible)

        for run_dir in victims:
            if _run_is_active_on_disk(run_dir):
                result.notes.append(
                    f"skipped active run {run_dir.name}",
                )
                continue
            sub = _remove_tree(run_dir)
            sub.runs_touched = 1
            result = result.add(sub)

        result.duration_ms = _elapsed_ms(started)
        return result

    def clear_chat(
        self,
        *,
        run_id: str | None = None,
        account_id: str | None = None,
    ) -> CleanupResult:
        """Composite: clear screenshots + reasoning + logs.

        Maps onto the Telegram /clear_chat command, which is the
        operator's "make this run quiet" button.
        """
        composite = CleanupResult(operation="clear_chat")
        composite = composite.add(self.clear_logs(
            run_id=run_id, account_id=account_id,
        ))
        composite = composite.add(self.clear_reasoning(
            run_id=run_id, account_id=account_id, keep_last=0,
        ))
        composite = composite.add(self.clear_screenshots(
            run_id=run_id, account_id=account_id, keep_last=0,
        ))
        composite.operation = "clear_chat"
        return composite

    # ---------------------------------------------------------- automatic
    def apply_settings(self) -> CleanupResult:
        """Run a sweep using the *persisted* settings.

        Designed to be called periodically (every few minutes by a
        scheduler task, or on demand from the dashboard "Apply now"
        button). Honors all configured limits in one pass. Settings
        with value ``0`` are skipped — that's the operator's signal
        that they want unlimited retention for that category.
        """
        composite = CleanupResult(operation="apply_settings")
        s = self.settings

        if s.screenshot_limit > 0:
            composite = composite.add(self.clear_screenshots())
        if s.reasoning_limit > 0:
            composite = composite.add(self.clear_reasoning())
        if s.auto_delete_minutes > 0:
            composite = composite.add(
                self._auto_delete_old_events(minutes=s.auto_delete_minutes),
            )
        if s.keep_completed_runs > 0:
            composite = composite.add(self.clear_runs(only_completed=True))

        composite.operation = "apply_settings"
        return composite

    def _auto_delete_old_events(
        self, *, minutes: int,
    ) -> CleanupResult:
        """Trim events.jsonl files older than ``minutes`` minutes.

        The implementation is line-aware: each event line carries a
        ``ts`` (float seconds since epoch) which we use to filter.
        Lines without ``ts`` are kept (the cost of a malformed line
        is far smaller than the cost of dropping a non-trivial event).
        """
        result = CleanupResult(operation="auto_delete_events")
        started = time.time()
        threshold = time.time() - minutes * 60.0

        for run_dir in self._iter_run_dirs():
            events = run_dir / "events.jsonl"
            if not events.exists():
                continue
            try:
                before = events.stat().st_size
                lines = events.read_text(encoding="utf-8").splitlines(keepends=True)
            except OSError as exc:
                result.notes.append(f"error: read {events}: {exc!r}")
                continue
            keep: list[str] = []
            removed = 0
            for line in lines:
                stripped = line.strip()
                if not stripped:
                    continue
                ts = _safe_extract_ts(stripped)
                if ts is None or ts >= threshold:
                    keep.append(line)
                else:
                    removed += 1
            if removed == 0:
                continue
            try:
                events.write_text("".join(keep), encoding="utf-8")
                after = events.stat().st_size
            except OSError as exc:
                result.notes.append(f"error: write {events}: {exc!r}")
                continue
            result.bytes_removed += max(0, before - after)
            result.runs_touched += 1
            # Each removed line is logically a "file removed" for the
            # purposes of the operator's summary panel.
            result.files_removed += removed
        result.duration_ms = _elapsed_ms(started)
        return result

    # ---------------------------------------------------------- helpers
    def _iter_run_dirs(
        self, *, only_run_id: str | None = None,
    ) -> Iterable[Path]:
        if not self.runs_root.exists():
            return []
        if only_run_id:
            d = self.runs_root / _safe_segment(only_run_id)
            if d.is_dir():
                return [d]
            return []

        def _gen() -> Iterable[Path]:
            for d in self.runs_root.iterdir():
                if d.is_dir() and not d.name.startswith("."):
                    yield d
        return _gen()

    def _iter_account_dirs(
        self,
        run_dir: Path,
        *,
        only_account_id: str | None = None,
    ) -> Iterable[Path]:
        if only_account_id:
            d = run_dir / _safe_segment(only_account_id)
            if d.is_dir():
                return [d]
            return []
        return [
            d for d in run_dir.iterdir()
            if d.is_dir() and not d.name.startswith(".") and d.name not in {"logs"}
        ]

    # ---------------------------------------------------------- inventory
    def usage_summary(self) -> dict[str, Any]:
        """Aggregate disk usage across all runs.

        Used by the Telegram /clear menu and the dashboard to render
        "X MB across N runs" before the operator decides what to
        delete. Cheap; iterates directories with ``stat`` only.
        """
        total_bytes = 0
        run_count = 0
        screenshot_count = 0
        reasoning_lines = 0
        for run_dir in self._iter_run_dirs():
            run_count += 1
            for entry in run_dir.rglob("*"):
                if not entry.is_file():
                    continue
                try:
                    size = entry.stat().st_size
                except OSError:
                    continue
                total_bytes += size
                if entry.suffix == ".png" and "screenshots" in entry.parts:
                    screenshot_count += 1
                elif entry.name == "reasoning.jsonl":
                    try:
                        reasoning_lines += sum(
                            1 for _ in entry.read_text(
                                encoding="utf-8", errors="ignore",
                            ).splitlines() if _.strip()
                        )
                    except OSError:
                        pass
        return {
            "runs": run_count,
            "screenshots": screenshot_count,
            "reasoning_lines": reasoning_lines,
            "total_bytes": total_bytes,
            "total_mb": round(total_bytes / (1024 * 1024), 2),
            "settings": self.settings.to_dict(),
        }


# ---------------------------------------------------------------- helpers
def _remove_files(paths: Iterable[Path]) -> CleanupResult:
    res = CleanupResult(operation="_remove_files")
    for p in paths:
        try:
            size = p.stat().st_size if p.exists() else 0
            p.unlink(missing_ok=True)
            res.files_removed += 1
            res.bytes_removed += size
        except OSError as exc:
            res.notes.append(f"error: unlink {p}: {exc!r}")
    return res


def _remove_tree(path: Path) -> CleanupResult:
    res = CleanupResult(operation="_remove_tree")
    if not path.exists():
        return res
    # Pre-count for accurate reporting.
    files = 0
    bytes_ = 0
    for entry in path.rglob("*"):
        if entry.is_file():
            files += 1
            try:
                bytes_ += entry.stat().st_size
            except OSError:
                pass
    try:
        shutil.rmtree(path, ignore_errors=False)
    except OSError as exc:
        # Best-effort cleanup. On failure, fall back to "ignore_errors"
        # so we delete *some* of the tree rather than nothing.
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass
        res.notes.append(f"error: rmtree {path}: {exc!r}")
        return res
    res.files_removed = files
    res.bytes_removed = bytes_
    return res


def _run_is_finished(run_dir: Path) -> bool:
    """True when the run's ``status.json`` is in a terminal state."""
    status_file = run_dir / "status.json"
    if not status_file.exists():
        return True  # no status → safe to assume finished
    try:
        data = json.loads(status_file.read_text())
    except (OSError, json.JSONDecodeError):
        return True
    return data.get("status") in {"completed", "failed", "cancelled"}


def _run_is_active_on_disk(run_dir: Path) -> bool:
    """True when the run's status says it's still active.

    Distinct from ``_run_is_finished`` — a run with status ``"paused"``
    is *not* finished but also *should not* be deleted, so we treat it
    as active.
    """
    status_file = run_dir / "status.json"
    if not status_file.exists():
        return False
    try:
        data = json.loads(status_file.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return data.get("status") in {"created", "planning", "running", "paused", "resuming"}


_TS_RE = re.compile(r'"ts"\s*:\s*([0-9.]+)')


def _safe_extract_ts(line: str) -> float | None:
    """Parse the ``ts`` field out of a JSONL event line without full json.loads.

    ``json.loads`` is fine but slower; this regex is one-pass and lets
    the auto-delete sweep stay in O(n) over file size. If the regex
    misses, the caller keeps the line — same fail-safe behaviour as
    a parse error.
    """
    m = _TS_RE.search(line)
    if not m:
        return None
    try:
        return float(m.group(1))
    except (TypeError, ValueError):
        return None


def _safe_segment(name: str) -> str:
    """Mirror :func:`automation.agent.run._safe` for path safety."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:128]


def _elapsed_ms(started: float) -> int:
    return int((time.time() - started) * 1000)
