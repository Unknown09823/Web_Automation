"""Log viewing routes."""
from __future__ import annotations

from collections import deque
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from automation.api.deps import auth_required, get_context

router = APIRouter(prefix="/logs", tags=["logs"])

_VALID = {"activity", "error", "debug"}


@router.get("")
async def list_logs(request: Request, _: str = Depends(auth_required)) -> dict:
    ctx = get_context(request)
    log_dir = Path(ctx.engine.config.get("logging.dir", "data/logs"))
    files = []
    if log_dir.exists():
        for p in sorted(log_dir.iterdir()):
            if p.is_file():
                stat = p.stat()
                files.append({"name": p.name, "size": stat.st_size, "mtime": stat.st_mtime})
    return {"directory": str(log_dir), "files": files}


@router.get("/{name}")
async def tail_log(
    name: str,
    request: Request,
    lines: int = 200,
    _: str = Depends(auth_required),
) -> dict:
    if name not in _VALID:
        raise HTTPException(status_code=400, detail=f"unknown log; valid: {sorted(_VALID)}")
    ctx = get_context(request)
    log_dir = Path(ctx.engine.config.get("logging.dir", "data/logs"))
    path = log_dir / f"{name}.log"
    if not path.exists():
        return {"name": name, "lines": []}
    lines = max(1, min(lines, 5000))
    buf: deque[str] = deque(maxlen=lines)
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                buf.append(line.rstrip("\n"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"name": name, "lines": list(buf)}
