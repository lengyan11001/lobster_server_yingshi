"""系统日志只读接口：GET /api/logs 返回 lobster/logs/app.log 末尾内容，供「日志」Tab 查看。"""
import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse

from .auth import get_current_user
from ..models import User

router = APIRouter()
logger = logging.getLogger(__name__)

# lobster 项目根目录（与 run.py 中 _root 一致，即含 backend 的目录）
_BASE = Path(__file__).resolve().parent.parent.parent.parent
_LOG_FILE = (_BASE / "logs" / "app.log").resolve()
_MAX_LINES = 5000
_DEFAULT_TAIL = 2000


def _read_log_tail(path: Path, tail: int) -> tuple[str, int]:
    """同步读文件最后 tail 行，在 executor 中调用避免阻塞。返回 (文本, 总行数)。"""
    if not path.exists():
        return "", 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return "", 0
    n = len(lines)
    if n > tail:
        lines = lines[-tail:]
    return "".join(lines), n


@router.get("/api/logs", summary="读取系统日志（末尾 N 行）")
async def get_logs(
    tail: int = Query(default=_DEFAULT_TAIL, ge=100, le=_MAX_LINES, description="返回最后 N 行"),
    current_user: User = Depends(get_current_user),
):
    """返回 lobster/logs/app.log 最后 tail 行，用于前端「日志」Tab 或排错。"""
    logger.info("[日志] GET /api/logs tail=%s path=%s exists=%s", tail, _LOG_FILE, _LOG_FILE.exists())
    if not _LOG_FILE.exists():
        logger.warning("[日志] 文件不存在: %s", _LOG_FILE)
        return PlainTextResponse(
            f"日志文件不存在: {_LOG_FILE}\n请确认已用 start.bat 或 run_backend 启动过至少一次。",
            status_code=404,
        )
    loop = asyncio.get_event_loop()
    text, total = await loop.run_in_executor(None, _read_log_tail, _LOG_FILE, tail)
    lines_returned = len(text.splitlines())
    logger.info("[日志] 返回 lines=%s total=%s", lines_returned, total)
    return PlainTextResponse(
        text if text else "(空)",
        media_type="text/plain; charset=utf-8",
        headers={"X-Log-Lines": str(lines_returned), "X-Log-Total-Lines": str(total)},
    )
