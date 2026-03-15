import logging
import socket
import httpx
from fastapi import APIRouter

router = APIRouter()
logger = logging.getLogger(__name__)
MCP_URL = "http://127.0.0.1:8001/mcp"


def _get_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


async def _mcp_status() -> dict:
    """检测 MCP 服务(8001)是否可达及返回的工具数量。用于诊断「启动后没有速推能力」."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.post(
                MCP_URL,
                json={"jsonrpc": "2.0", "id": "health", "method": "tools/list", "params": {}},
            )
            if r.status_code == 200:
                tools = r.json().get("result", {}).get("tools", [])
                return {"reachable": True, "tools_count": len(tools)}
    except Exception as e:
        logger.debug("[健康] MCP 不可达: %s", e)
    return {"reachable": False, "tools_count": 0}


@router.get("/api/health", summary="健康检查（含 MCP 能力服务状态）")
async def health():
    mcp = await _mcp_status()
    return {
        "status": "ok",
        "lan_ip": _get_lan_ip(),
        "mcp": mcp,
    }


@router.get("/api/lan-ip", summary="获取局域网 IP")
def lan_ip():
    return {"ip": _get_lan_ip()}
