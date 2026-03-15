"""算力账号：用户可配置多个，绑定速推 Token，耗算力时用其一并扣主账号积分。"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ConsumptionAccount, User
from .auth import get_current_user

router = APIRouter()


class AccountIn(BaseModel):
    name: str
    sutui_token: Optional[str] = None
    is_default: Optional[bool] = None


def get_effective_sutui_token(user: User, db: Session) -> Optional[str]:
    """当前用户用于调用速推的 Token：优先算力账号中的默认或首个有 token 的，否则 user.sutui_token。"""
    row = (
        db.query(ConsumptionAccount)
        .filter(ConsumptionAccount.user_id == user.id)
        .order_by(ConsumptionAccount.is_default.desc(), ConsumptionAccount.id.asc())
        .first()
    )
    if row and (row.sutui_token or "").strip():
        return (row.sutui_token or "").strip()
    return (getattr(user, "sutui_token", None) or "").strip() or None


@router.get("/api/consumption-accounts", summary="我的算力账号列表")
def list_accounts(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(ConsumptionAccount)
        .filter(ConsumptionAccount.user_id == current_user.id)
        .order_by(ConsumptionAccount.is_default.desc(), ConsumptionAccount.id.asc())
        .all()
    )
    return [
        {
            "id": r.id,
            "name": r.name,
            "has_token": bool((r.sutui_token or "").strip()),
            "is_default": r.is_default,
            "created_at": r.created_at.isoformat() if r.created_at else "",
        }
        for r in rows
    ]


@router.post("/api/consumption-accounts", summary="新增算力账号")
def create_account(
    body: AccountIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="账号名称不能为空")
    token = (body.sutui_token or "").strip() or None
    is_default = body.is_default if body.is_default is not None else False
    if is_default:
        db.query(ConsumptionAccount).filter(ConsumptionAccount.user_id == current_user.id).update({"is_default": False})
    acc = ConsumptionAccount(
        user_id=current_user.id,
        name=name,
        sutui_token=token,
        is_default=is_default,
    )
    db.add(acc)
    db.commit()
    db.refresh(acc)
    return {"id": acc.id, "name": acc.name, "has_token": bool(token), "is_default": acc.is_default}


@router.put("/api/consumption-accounts/{account_id}", summary="更新算力账号")
def update_account(
  account_id: int,
  body: AccountIn,
  current_user: User = Depends(get_current_user),
  db: Session = Depends(get_db),
):
    acc = db.query(ConsumptionAccount).filter(
        ConsumptionAccount.id == account_id,
        ConsumptionAccount.user_id == current_user.id,
    ).first()
    if not acc:
        raise HTTPException(status_code=404, detail="算力账号不存在")
    if body.name is not None:
        name = (body.name or "").strip()
        if name:
            acc.name = name
    if body.sutui_token is not None:
        acc.sutui_token = (body.sutui_token or "").strip() or None
    if body.is_default is not None:
        if body.is_default:
            db.query(ConsumptionAccount).filter(ConsumptionAccount.user_id == current_user.id).update({"is_default": False})
        acc.is_default = body.is_default
    db.commit()
    db.refresh(acc)
    return {"id": acc.id, "name": acc.name, "has_token": bool((acc.sutui_token or "").strip()), "is_default": acc.is_default}


@router.delete("/api/consumption-accounts/{account_id}", summary="删除算力账号")
def delete_account(
  account_id: int,
  current_user: User = Depends(get_current_user),
  db: Session = Depends(get_db),
):
    acc = db.query(ConsumptionAccount).filter(
        ConsumptionAccount.id == account_id,
        ConsumptionAccount.user_id == current_user.id,
    ).first()
    if not acc:
        raise HTTPException(status_code=404, detail="算力账号不存在")
    db.delete(acc)
    db.commit()
    return {"ok": True}
