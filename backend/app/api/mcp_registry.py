"""MCP Registry proxy with local file cache and pagination.

- /api/mcp-registry/browse?page=1  — paginated browsing (one upstream page per request)
- /api/mcp-registry/search?q=xxx   — searches local cache (all previously fetched pages)
- /api/mcp-registry/categories      — category counts from cache
"""
import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, Query

from .auth import get_current_user
from ..models import User

logger = logging.getLogger(__name__)
router = APIRouter()

MCP_REGISTRY_BASE = "https://registry.modelcontextprotocol.io/v0.1"
_BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
CACHE_FILE = _BASE_DIR / "mcp_registry_cache.json"

PAGE_SIZE = 30

# ── persistent local cache ──────────────────────────────────────────

def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"servers": {}, "cursors": {}, "ts": 0}


def _save_cache(cache: dict):
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _cache_servers_list(cache: dict) -> List[dict]:
    return list(cache.get("servers", {}).values())


def _merge_into_cache(cache: dict, servers: List[dict]):
    store = cache.setdefault("servers", {})
    for srv in servers:
        name = srv.get("name", "")
        if name:
            store[name] = srv
    cache["ts"] = time.time()


# ── extract & tag ───────────────────────────────────────────────────

def _extract_server_info(srv: dict) -> dict:
    remotes = srv.get("remotes", [])
    remote_url = ""
    for r in remotes:
        url = r.get("url", "")
        if url and not url.startswith("{"):
            remote_url = url
            break

    packages = srv.get("packages", [])
    install_cmd = ""
    for pkg in packages:
        reg = pkg.get("registryType", "")
        ident = pkg.get("identifier", "")
        if reg == "npm" and ident:
            install_cmd = f"npx {ident}"
        elif reg == "oci" and ident:
            install_cmd = f"docker run {ident}"

    name = srv.get("name", "")
    title = srv.get("title", "") or name.split("/")[-1]
    desc = srv.get("description", "")

    return {
        "name": name,
        "title": title,
        "description": desc,
        "remote_url": remote_url,
        "install_cmd": install_cmd,
        "website": srv.get("websiteUrl", ""),
        "version": srv.get("version", ""),
        "repo": srv.get("repository", {}).get("url", ""),
        "tags": _infer_tags(name, title, desc),
    }


CATEGORY_KEYWORDS = {
    "image": ["image", "photo", "picture", "img", "dalle", "midjourney", "stable diffusion", "flux"],
    "video": ["video", "youtube", "vimeo", "ffmpeg"],
    "audio": ["audio", "voice", "speech", "music", "tts", "sound"],
    "database": ["database", "db", "sql", "postgres", "mysql", "mongo", "redis", "sqlite", "supabase"],
    "search": ["search", "web", "google", "bing", "brave", "tavily", "serp", "crawl", "scrape"],
    "code": ["code", "github", "git", "gitlab", "bitbucket", "lint", "compiler", "ide"],
    "file": ["file", "filesystem", "storage", "s3", "drive", "dropbox", "ftp"],
    "ai": ["ai", "llm", "openai", "anthropic", "model", "embedding", "vector"],
    "communication": ["slack", "discord", "email", "telegram", "sms", "chat", "notification"],
    "devops": ["docker", "kubernetes", "k8s", "aws", "cloud", "deploy", "ci", "cd", "terraform"],
}


def _infer_tags(name: str, title: str, desc: str) -> List[str]:
    text = f"{name} {title} {desc}".lower()
    tags = []
    for tag, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            tags.append(tag)
    return tags


def _get_category_counts(servers: List[dict]) -> dict:
    counts: Dict[str, int] = {}
    for srv in servers:
        for tag in srv.get("tags", []):
            counts[tag] = counts.get(tag, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: -x[1]))


def _matches(srv: dict, q: Optional[str], category: Optional[str]) -> bool:
    if category and category not in srv.get("tags", []):
        return False
    if q:
        q_lower = q.lower()
        searchable = f"{srv.get('name', '')} {srv.get('title', '')} {srv.get('description', '')}".lower()
        terms = q_lower.split()
        if not all(term in searchable for term in terms):
            return False
    return True


# ── upstream fetch ──────────────────────────────────────────────────

async def _fetch_upstream_page(cursor: Optional[str] = None) -> dict:
    """Fetch one page from the MCP registry."""
    params: dict = {}
    if cursor:
        params["cursor"] = cursor
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(f"{MCP_REGISTRY_BASE}/servers", params=params)
            if resp.status_code == 200:
                return resp.json()
    except Exception as e:
        logger.warning("MCP Registry page fetch failed: %s", e)
    return {}


# ── API endpoints ───────────────────────────────────────────────────

@router.get("/api/mcp-registry/browse", summary="Browse official MCP registry (paginated)")
async def browse_registry(
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    current_user: User = Depends(get_current_user),
):
    """Each page fetches one upstream page and caches its results locally."""
    cache = _load_cache()
    cursors = cache.setdefault("cursors", {})

    cursor = None
    if page > 1:
        cursor = cursors.get(str(page - 1))
        if not cursor:
            for p in range(1, page):
                prev_cursor = cursors.get(str(p - 1)) if p > 1 else None
                if str(p) in cursors:
                    continue
                data = await _fetch_upstream_page(prev_cursor)
                raw = data.get("servers", [])
                new_servers = []
                for entry in raw:
                    srv = entry.get("server", {})
                    if srv.get("name"):
                        new_servers.append(_extract_server_info(srv))
                _merge_into_cache(cache, new_servers)
                nc = data.get("metadata", {}).get("nextCursor")
                if nc:
                    cursors[str(p)] = nc
                else:
                    break
            _save_cache(cache)
            cursor = cursors.get(str(page - 1))

    data = await _fetch_upstream_page(cursor)
    raw = data.get("servers", [])
    servers: List[dict] = []
    seen: set = set()
    for entry in raw:
        srv = entry.get("server", {})
        name = srv.get("name", "")
        if not name or name in seen:
            continue
        seen.add(name)
        servers.append(_extract_server_info(srv))

    _merge_into_cache(cache, servers)
    next_cursor = data.get("metadata", {}).get("nextCursor")
    if next_cursor:
        cursors[str(page)] = next_cursor
    _save_cache(cache)

    has_next = bool(next_cursor)
    all_cached = _cache_servers_list(cache)

    return {
        "servers": servers,
        "page": page,
        "has_next": has_next,
        "cached_total": len(all_cached),
        "categories": _get_category_counts(all_cached),
    }


@router.get("/api/mcp-registry/search", summary="Search local cache of MCP registry")
async def search_mcp_registry(
    q: Optional[str] = Query(None, description="Search keyword"),
    category: Optional[str] = Query(None, description="Category filter"),
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
    current_user: User = Depends(get_current_user),
):
    """Search within locally cached servers (all previously browsed pages)."""
    cache = _load_cache()
    all_servers = _cache_servers_list(cache)

    if not q and not category:
        total = len(all_servers)
        start = (page - 1) * page_size
        return {
            "servers": all_servers[start:start + page_size],
            "total": total,
            "page": page,
            "has_next": start + page_size < total,
            "categories": _get_category_counts(all_servers),
        }

    results = [s for s in all_servers if _matches(s, q, category)]
    total = len(results)
    start = (page - 1) * page_size
    return {
        "servers": results[start:start + page_size],
        "total": total,
        "page": page,
        "has_next": start + page_size < total,
        "categories": _get_category_counts(all_servers),
    }


@router.get("/api/mcp-registry/categories", summary="Get available skill categories")
async def get_categories(
    current_user: User = Depends(get_current_user),
):
    cache = _load_cache()
    all_servers = _cache_servers_list(cache)
    return {"categories": _get_category_counts(all_servers), "total": len(all_servers)}
