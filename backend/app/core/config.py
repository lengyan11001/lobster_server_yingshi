from __future__ import annotations

import socket
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
    """微信/支付回调根地址。不填时自动用本机 LAN IP:PORT；服务器仅公网 IP 无域名时填 http://公网IP:8000"""
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
    """为 False 时仅提供 API，不挂载前端静态页与 /；online 客户端用本地页面访问本服务时设为 false。"""
    serve_frontend: bool = True
    # 自建微信登录（不用速推）：小程序 appid/secret，配置后登录页展示小程序码扫码
    wechat_app_id: Optional[str] = None
    wechat_app_secret: Optional[str] = None
    """小程序码跳转的页面路径，如 pages/index/index，扫码后打开该页并带 scene"""
    wechat_miniprogram_page: Optional[str] = None
    # 自建微信支付（不用速推）：商户号、APIv3 密钥，配置后充值可走微信 Native 扫码
    wechat_mch_id: Optional[str] = None
    wechat_pay_apiv3_key: Optional[str] = None
    """微信支付商户证书序列号（回调验签用）"""
    wechat_pay_serial_no: Optional[str] = None
    """微信支付商户私钥文件路径（.pem）或 PEM 内容，统一下单签名用"""
    wechat_pay_private_key_path: Optional[str] = None
    openclaw_gateway_url: Optional[str] = None
    openclaw_gateway_token: Optional[str] = None
    openclaw_agent_id: str = "main"
    """本地轮询拉取/提交回复时的鉴权：请求头 X-Forward-Secret 需与此一致。不设则不做校验（仅内网或隧道时建议设置）。"""
    wecom_forward_secret: Optional[str] = None
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


def get_effective_public_base_url() -> str:
    """微信/支付回调等用的根地址。未配置 PUBLIC_BASE_URL 时用本机 LAN IP + PORT（本地或服务器仅 IP 时可直接用）。"""
    base = (getattr(settings, "public_base_url", None) or "").strip().rstrip("/")
    if base:
        return base
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = "127.0.0.1"
    port = getattr(settings, "port", 8000)
    return f"http://{ip}:{port}"
