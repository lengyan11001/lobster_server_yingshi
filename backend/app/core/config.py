from __future__ import annotations

from functools import lru_cache
from typing import List, Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "龙虾 (Lobster)"
    debug: bool = True
    secret_key: str = "lobster-secret-change-me"
    cors_origins: str = "*"
    database_url: str = "sqlite:///./lobster.db"
    host: str = "0.0.0.0"
    port: int = 8000
    """可选：用于生成素材文件等对外 URL 的根地址（纯 ASCII，避免编码问题）。例: http://192.168.200.57:8000"""
    public_base_url: Optional[str] = None
    mcp_port: int = 8001
    """本构建统一为在线版：online（独立登录/注册或速推扫码，速推 Token 来自登录）。"""
    lobster_edition: str = "online"
    """在线版为 True 时：登录注册与充值全部自维护，不走速推；用户配置算力账号（速推 Token）用于耗算力，速推扣多少我们扣多少积分。"""
    lobster_independent_auth: bool = True
    """完成充值订单时需在请求头 X-Admin-Secret 携带此值（仅服务端/管理员使用）。"""
    lobster_recharge_admin_secret: Optional[str] = None
    """充值创建订单后展示给用户的付款说明。"""
    lobster_recharge_payment_hint: Optional[str] = None
    """仅用于测试或脚本；在线版不创建默认用户。"""
    default_user_email: str = "user@lobster.local"
    default_user_password: str = "lobster123"
    """在线版：速推 OAuth 登录页 URL，登录成功后跳转到 /auth/sutui-callback?token=xxx"""
    sutui_oauth_login_url: Optional[str] = None
    """速推 API 根地址，用于 apikeys/list、balance 等（仅 online 使用）"""
    sutui_api_base: str = "https://api.xskill.ai"
    """服务器侧速推 Token：能力由服务器转发时使用，用户不直接走速推。MCP 从环境变量 SUTUI_SERVER_TOKEN 读取。"""
    sutui_server_token: Optional[str] = None
    """我方标识，登录时带在 URL 上供速推统计（仅 online 使用）"""
    sutui_source_id: Optional[str] = None
    """充值页链接，前端「充值」按钮跳转（仅 online 使用）"""
    sutui_recharge_url: Optional[str] = None
    """是否允许 online 用户自配模型 Key；False 时统一走速推服务端模型（仅 online 使用）"""
    sutui_online_model_self_config: bool = True
    openclaw_gateway_url: Optional[str] = None
    openclaw_gateway_token: Optional[str] = None
    openclaw_agent_id: str = "main"
    capability_sutui_mcp_url: Optional[str] = None
    capability_upstream_urls_json: Optional[str] = None
    reddit_comment2video_backend_url: Optional[str] = None

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    def cors_origins_list(self) -> List[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [x.strip() for x in self.cors_origins.split(",") if x.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
