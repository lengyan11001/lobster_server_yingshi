"""每小时对账：速推 server token 远端余额变动 vs 本地 credit_ledger（meta._recon）。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from sqlalchemy.orm import Session

from mcp.sutui_tokens import (
    _legacy_sutui_tokens_list,
    get_sutui_tokens_list_bihuo,
    get_sutui_tokens_list_yingshi,
    sutui_token_ref_from_secret,
)

from ..core.config import settings
from ..db import SessionLocal
from ..models import CreditLedger, SutuiReconciliationRun
from ..services.credits_amount import quantize_credits
from ..services.sutui_llm_probe import is_sutui_llm_probe_enabled_for_this_instance

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_DATA_DIR = _ROOT / "data"

_DEFAULT_INTERVAL = 3600.0


def is_sutui_reconcile_enabled() -> bool:
    if not is_sutui_llm_probe_enabled_for_this_instance():
        return False
    v = (os.environ.get("LOBSTER_SUTUI_RECONCILE_ENABLED") or "true").strip().lower()
    return v not in ("0", "false", "no", "off")


def _api_base() -> str:
    return (getattr(settings, "sutui_api_base", None) or os.environ.get("SUTUI_API_BASE") or "https://api.xskill.ai").rstrip(
        "/"
    )


def fetch_sutui_balance_server_token_sync(token: str) -> Tuple[Optional[Decimal], str]:
    """使用服务器 sk 拉取速推余额；先 Bearer，失败再 api_key 查询参数（与 openclaw 用户接口一致）。"""
    t = (token or "").strip()
    if not t:
        return None, "empty_token"
    base = _api_base()
    url = f"{base}/api/v3/balance"
    try:
        with httpx.Client(timeout=25.0, trust_env=True) as client:
            r = client.get(url, headers={"Authorization": f"Bearer {t}", "Accept": "application/json"})
            if r.status_code >= 400:
                r = client.get(url, params={"api_key": t}, headers={"Accept": "application/json"})
        try:
            data = r.json() if r.content else {}
        except Exception:
            return None, f"bad_json_http_{r.status_code}"
        if not isinstance(data, dict):
            return None, "not_dict"
        if data.get("code") != 200:
            return None, (data.get("detail") or data.get("msg") or f"code_{data.get('code')}")[:500]
        d = data.get("data") if isinstance(data.get("data"), dict) else {}
        raw = d.get("balance")
        if raw is None:
            raw = d.get("points") or d.get("remaining") or 0
        try:
            bal = quantize_credits(raw)
        except Exception:
            bal = quantize_credits(0)
        return bal, ""
    except Exception as e:
        logger.warning("[sutui-reconcile] balance 请求失败: %s", e)
        return None, str(e)[:500]


def _collect_unique_server_tokens() -> List[Tuple[str, str, str]]:
    """(pool_label, secret_token, token_ref) 去重。"""
    seen_ref: set[str] = set()
    out: List[Tuple[str, str, str]] = []
    for pool_label, lst in (
        ("bihuo", get_sutui_tokens_list_bihuo()),
        ("yingshi", get_sutui_tokens_list_yingshi()),
        ("legacy", _legacy_sutui_tokens_list()),
    ):
        for tok in lst:
            tok = (tok or "").strip()
            if not tok:
                continue
            ref = sutui_token_ref_from_secret(tok)
            if not ref or ref in seen_ref:
                continue
            seen_ref.add(ref)
            out.append((pool_label, tok, ref))
    return out


def collect_bihuo_yingshi_server_tokens() -> List[Tuple[str, str, str]]:
    """仅必火/影视 server 池（不含 legacy），同一 sk 只保留一条。用于双账户基线快照。"""
    seen_ref: set[str] = set()
    out: List[Tuple[str, str, str]] = []
    for pool_label, lst in (
        ("bihuo", get_sutui_tokens_list_bihuo()),
        ("yingshi", get_sutui_tokens_list_yingshi()),
    ):
        for tok in lst:
            tok = (tok or "").strip()
            if not tok:
                continue
            ref = sutui_token_ref_from_secret(tok)
            if not ref or ref in seen_ref:
                continue
            seen_ref.add(ref)
            out.append((pool_label, tok, ref))
    return out


def _local_net_credits_for_ref(db: Session, token_ref: str, t0: datetime, t1: datetime) -> Decimal:
    """窗口内：与本 token_ref 关联的积分变动净值（用户侧消耗为正）。"""
    q = (
        db.query(CreditLedger)
        .filter(CreditLedger.created_at >= t0, CreditLedger.created_at <= t1)
        .order_by(CreditLedger.id.asc())
    )
    net = Decimal(0)
    for row in q:
        m = row.meta
        if not isinstance(m, dict):
            continue
        recon = m.get("_recon")
        if not isinstance(recon, dict):
            continue
        if (recon.get("sutui_token_ref") or "").strip() != token_ref:
            continue
        net = quantize_credits(net - row.delta)
    return net


def seed_sutui_reconciliation_baseline() -> Dict[str, Any]:
    """
    手动拉取必火/影视两把（或多把）server sk 的速推余额，写入 sutui_reconciliation_runs（status=baseline_seed）
    与 data/sutui_reconcile_baseline.json。后续定时对账会以最新一条 balance_remote 为上一档口径算 remote_delta。
    """
    tokens = collect_bihuo_yingshi_server_tokens()
    summary: Dict[str, Any] = {
        "at": datetime.utcnow().isoformat() + "Z",
        "kind": "baseline_seed",
        "pools": ["bihuo", "yingshi"],
        "items": [],
    }
    if not tokens:
        summary["error"] = "未配置 SUTUI_SERVER_TOKENS_BIHUO / SUTUI_SERVER_TOKENS_YINGSHI"
        logger.warning("[sutui-reconcile] baseline_seed 跳过：无必火/影视 token")
        return summary

    db = SessionLocal()
    try:
        for pool, secret, ref in tokens:
            bal, err = fetch_sutui_balance_server_token_sync(secret)
            item: Dict[str, Any] = {
                "pool": pool,
                "sutui_token_ref": ref,
                "balance_remote": float(bal) if bal is not None else None,
                "ok": bal is not None,
                "error": err or None,
            }
            summary["items"].append(item)
            if bal is None:
                logger.warning("[sutui-reconcile] baseline_seed 跳过入库 pool=%s ref=%s err=%s", pool, ref, err)
                continue
            row = SutuiReconciliationRun(
                pool=pool,
                sutui_token_ref=ref,
                balance_remote=bal,
                remote_delta=None,
                local_net_credits=quantize_credits(0),
                diff=None,
                status="baseline_seed",
                detail="手动基线：记录远端余额供后续对账",
            )
            db.add(row)
            db.commit()
            logger.info(
                "[sutui-reconcile] baseline_seed 已写入 pool=%s ref=%s balance_remote=%s",
                pool,
                ref,
                bal,
            )
    except Exception as e:
        logger.exception("[sutui-reconcile] baseline_seed 异常: %s", e)
        summary["exception"] = str(e)[:500]
        db.rollback()
    finally:
        db.close()

    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = _DATA_DIR / "sutui_reconcile_baseline.json"
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except Exception as e:
        logger.debug("[sutui-reconcile] 写 baseline json: %s", e)

    return summary


def run_sutui_reconcile_sync() -> Dict[str, Any]:
    """同步跑一次对账（可由 asyncio.to_thread 调用）。"""
    tokens = _collect_unique_server_tokens()
    summary: Dict[str, Any] = {
        "at": datetime.utcnow().isoformat() + "Z",
        "tokens_checked": len(tokens),
        "rows": [],
    }
    if not tokens:
        logger.info("[sutui-reconcile] 未配置 SUTUI_SERVER_TOKENS_*，跳过")
        return summary

    db = SessionLocal()
    try:
        now = datetime.utcnow()
        for pool, secret, ref in tokens:
            prev_row = (
                db.query(SutuiReconciliationRun)
                .filter(SutuiReconciliationRun.sutui_token_ref == ref)
                .order_by(SutuiReconciliationRun.id.desc())
                .first()
            )
            window_start = prev_row.created_at if prev_row else (now - timedelta(minutes=65))
            bal, err = fetch_sutui_balance_server_token_sync(secret)

            local_net = _local_net_credits_for_ref(db, ref, window_start, now)
            prev_bal = prev_row.balance_remote if prev_row else None
            remote_used: Optional[Decimal] = None
            if prev_bal is not None and bal is not None:
                remote_used = quantize_credits(prev_bal - bal)

            diff: Optional[Decimal] = None
            status = "ok"
            detail = err or ""
            if bal is None:
                status = "error"
            elif remote_used is None:
                status = "baseline"
                detail = (detail + " | 首次采样，无 remote_delta").strip(" |")
            elif remote_used < 0:
                status = "info"
                detail = (
                    f"远端余额上升 {-remote_used}（可能速推侧充值/调账），本地窗口净消耗 local_net={local_net}"
                )
            else:
                diff = quantize_credits(remote_used - local_net)
                tol = Decimal("1")
                if abs(diff) > tol:
                    status = "warn"
                    detail = f"remote_used={remote_used} local_net={local_net} diff={diff}"

            row = SutuiReconciliationRun(
                pool=pool,
                sutui_token_ref=ref,
                balance_remote=bal,
                remote_delta=remote_used,
                local_net_credits=local_net,
                diff=diff,
                status=status,
                detail=detail[:2000] if detail else None,
            )
            db.add(row)
            db.commit()
            summary["rows"].append(
                {
                    "pool": pool,
                    "sutui_token_ref": ref,
                    "balance_remote": float(bal) if bal is not None else None,
                    "remote_delta": float(remote_used) if remote_used is not None else None,
                    "local_net": float(local_net),
                    "diff": float(diff) if diff is not None else None,
                    "status": status,
                }
            )
            log_fn = logger.warning if status == "warn" else logger.info
            log_fn(
                "[sutui-reconcile] pool=%s ref=%s status=%s remote_delta=%s local_net=%s diff=%s",
                pool,
                ref,
                status,
                remote_used,
                local_net,
                diff,
            )
    except Exception as e:
        logger.exception("[sutui-reconcile] 运行异常: %s", e)
        summary["error"] = str(e)[:500]
        db.rollback()
    finally:
        db.close()

    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = _DATA_DIR / "sutui_reconcile_last.json"
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except Exception as e:
        logger.debug("[sutui-reconcile] 写快照文件跳过: %s", e)

    return summary


async def sutui_reconcile_loop_forever(interval_sec: float = _DEFAULT_INTERVAL) -> None:
    """与 LLM 探针类似：后台循环。"""
    sec = float(os.environ.get("LOBSTER_SUTUI_RECONCILE_INTERVAL_SEC") or interval_sec)
    logger.info("[sutui-reconcile] 已启动，间隔 %.0fs", sec)
    while True:
        try:
            await asyncio.to_thread(run_sutui_reconcile_sync)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[sutui-reconcile] 单次执行失败")
        await asyncio.sleep(sec)
