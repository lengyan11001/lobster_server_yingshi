"""Custom configuration management: store arbitrary JSON config blocks and custom models."""
import json
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from .auth import get_current_user
from ..models import User

router = APIRouter()

_BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
_CUSTOM_CONFIGS_FILE = _BASE_DIR / "custom_configs.json"


def _load_configs() -> dict:
    if not _CUSTOM_CONFIGS_FILE.exists():
        return {"configs": {}, "custom_models": []}
    try:
        return json.loads(_CUSTOM_CONFIGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"configs": {}, "custom_models": []}


def _save_configs(data: dict):
    _CUSTOM_CONFIGS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# --- JSON Config Blocks ---

class ConfigBlockIn(BaseModel):
    name: str
    config_json: str  # raw JSON or Python-dict-like string


@router.get("/api/custom-configs", summary="List all custom config blocks")
def list_configs(current_user: User = Depends(get_current_user)):
    data = _load_configs()
    configs = data.get("configs", {})
    return {
        "configs": [
            {"name": k, "config": v}
            for k, v in configs.items()
        ]
    }


@router.post("/api/custom-configs", summary="Add or update a config block")
def save_config(body: ConfigBlockIn, current_user: User = Depends(get_current_user)):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Config name is required")

    raw = body.config_json.strip()
    parsed = _try_parse_config(raw)
    if parsed is None:
        raise HTTPException(
            status_code=400,
            detail="Invalid config format. Supports JSON object, Python dict, or KEY=VALUE lines.",
        )

    data = _load_configs()
    configs = data.setdefault("configs", {})
    configs[name] = parsed
    _save_configs(data)

    _write_config_to_env(name, parsed)

    return {"ok": True, "name": name, "message": f"Config '{name}' saved"}


@router.delete("/api/custom-configs/{name}", summary="Delete a config block")
def delete_config(name: str, current_user: User = Depends(get_current_user)):
    data = _load_configs()
    configs = data.get("configs", {})
    if name not in configs:
        raise HTTPException(status_code=404, detail=f"Config '{name}' not found")
    del configs[name]
    _save_configs(data)
    return {"ok": True, "message": f"Config '{name}' deleted"}


def _try_parse_config(raw: str) -> Optional[dict]:
    """Try to parse as JSON, Python dict literal, or KEY=VALUE lines."""
    # JSON
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass

    # Python dict-like: replace single quotes, handle unquoted keys
    try:
        import ast
        obj = ast.literal_eval(raw)
        if isinstance(obj, dict):
            return {str(k): v for k, v in obj.items()}
    except Exception:
        pass

    # KEY = VALUE lines
    lines = raw.strip().splitlines()
    if len(lines) >= 1 and all("=" in line or not line.strip() for line in lines):
        result = {}
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                k = k.strip().strip("'\"")
                v = v.strip().strip("'\"")
                result[k] = v
        if result:
            return result

    return None


def _write_config_to_env(name: str, config: dict):
    """Write config values as environment variables to .env file."""
    env_file = _BASE_DIR / ".env"
    if not env_file.exists():
        return

    existing = env_file.read_text(encoding="utf-8")
    lines = existing.splitlines()

    marker_start = f"# --- {name} START ---"
    marker_end = f"# --- {name} END ---"

    new_lines = []
    skipping = False
    for line in lines:
        if line.strip() == marker_start:
            skipping = True
            continue
        if line.strip() == marker_end:
            skipping = False
            continue
        if not skipping:
            new_lines.append(line)

    new_lines.append(marker_start)
    for k, v in config.items():
        new_lines.append(f"{k}={v}")
    new_lines.append(marker_end)

    env_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


# --- Custom Models ---

class CustomModelIn(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    model_id: str
    display_name: str
    provider: str = ""
    api_key_env: str = ""
    api_key_value: str = ""


@router.get("/api/custom-models", summary="List custom models")
def list_custom_models(current_user: User = Depends(get_current_user)):
    data = _load_configs()
    return {"models": data.get("custom_models", [])}


@router.post("/api/custom-models", summary="Add a custom model")
def add_custom_model(body: CustomModelIn, current_user: User = Depends(get_current_user)):
    if not body.model_id.strip():
        raise HTTPException(status_code=400, detail="Model ID is required")

    data = _load_configs()
    models = data.setdefault("custom_models", [])

    existing = [m for m in models if m.get("model_id") != body.model_id.strip()]
    entry = {
        "model_id": body.model_id.strip(),
        "display_name": body.display_name.strip() or body.model_id.strip(),
        "provider": body.provider.strip(),
    }
    if body.api_key_env.strip():
        entry["api_key_env"] = body.api_key_env.strip()
    existing.append(entry)
    data["custom_models"] = existing
    _save_configs(data)

    if body.api_key_value.strip() and body.api_key_env.strip():
        _update_env_key(body.api_key_env.strip(), body.api_key_value.strip())

    return {"ok": True, "message": f"Model '{body.display_name or body.model_id}' saved"}


@router.delete("/api/custom-models/{model_id:path}", summary="Remove a custom model")
def delete_custom_model(model_id: str, current_user: User = Depends(get_current_user)):
    data = _load_configs()
    models = data.get("custom_models", [])
    before = len(models)
    models = [m for m in models if m.get("model_id") != model_id]
    if len(models) == before:
        raise HTTPException(status_code=404, detail="Model not found")
    data["custom_models"] = models
    _save_configs(data)
    return {"ok": True, "message": "Model removed"}


def _update_env_key(key: str, value: str):
    """Set a key=value in the openclaw/.env file."""
    oc_env = _BASE_DIR / "openclaw" / ".env"
    oc_env.parent.mkdir(parents=True, exist_ok=True)

    existing = {}
    comments = []
    if oc_env.exists():
        for line in oc_env.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                comments.append(line)
                continue
            if "=" in stripped:
                k, _, v = stripped.partition("=")
                existing[k.strip()] = v.strip()

    existing[key] = value

    out_lines = comments[:]
    for k, v in sorted(existing.items()):
        out_lines.append(f"{k}={v}")
    oc_env.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
