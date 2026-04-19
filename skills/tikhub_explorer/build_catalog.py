"""把 TikHub OpenAPI 转成 lobster TikHub Explorer 的统一 catalog.

用法：
    python skills/tikhub_explorer/build_catalog.py
    或在 admin 后台点「刷新接口目录」，会调用此脚本（也可由 tikhub_proxy 后台异步刷新）。

输出：
    skills/tikhub_explorer/catalog.json   ← 客户端二级页 & 服务端白名单都吃这份。

设计要点：
- 平台从 OpenAPI tags 推断（Douyin-* → douyin，TikTok-* → tiktok 等）。
- 接口 endpoint_id = sha1 友好的 operationId 简短化；server proxy 仅放行 catalog 内已登记的 id。
- 分页协议从参数名自动猜测（cursor / max_cursor / offset / page / pcursor / next_token）。
- 解 $ref：如果参数 schema 是 $ref，到 components.schemas 里取一层。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import urllib.request

DEFAULT_OPENAPI_URL = "https://api.tikhub.io/openapi.json"
HERE = Path(__file__).resolve().parent
DEFAULT_OUTPUT = HERE / "catalog.json"

# 平台映射：tag 前缀 -> (platform_id, 中文名, icon)。Tag 大小写忽略。
PLATFORM_MAP: List[Tuple[str, str, str, str]] = [
    ("douyin", "douyin", "抖音", "🎵"),
    ("tiktok", "tiktok", "TikTok", "🎬"),
    ("xiaohongshu", "xhs", "小红书", "📕"),
    ("bilibili", "bilibili", "B站", "📺"),
    ("kuaishou", "kuaishou", "快手", "⚡"),
    ("weibo", "weibo", "微博", "🐧"),
    ("wechat", "wechat", "微信", "💚"),
    ("instagram", "instagram", "Instagram", "📷"),
    ("youtube", "youtube", "YouTube", "▶️"),
    ("twitter", "twitter", "X / Twitter", "🐦"),
    ("threads", "threads", "Threads", "🧵"),
    ("reddit", "reddit", "Reddit", "🤖"),
    ("zhihu", "zhihu", "知乎", "🦓"),
    ("linkedin", "linkedin", "LinkedIn", "💼"),
    ("toutiao", "toutiao", "今日头条", "📰"),
    ("lemon8", "lemon8", "Lemon8", "🍋"),
    ("pipixia", "pipixia", "皮皮虾", "🦐"),
    ("xigua", "xigua", "西瓜视频", "🍉"),
    ("sora2", "sora2", "Sora 2", "🌅"),
    ("hybrid", "hybrid", "通用解析", "🔧"),
    ("temp-mail", "tempmail", "临时邮箱", "📧"),
    ("tikhub-downloader", "tikhub", "TikHub 下载器", "⭐"),
    ("tikhub-user", "tikhub", "TikHub 账户", "⭐"),
]

# 这些 tag 不暴露给前端
SKIP_TAG_PREFIXES = {"health", "demo", "ios-shortcut", "captcha"}

# 分页参数识别：按出现顺序匹配
PAGINATION_RULES: List[Dict[str, Any]] = [
    {"kind": "max_cursor",  "in_param": "max_cursor",  "out_field": "max_cursor",  "has_more_field": "has_more", "page_size_param": "count"},
    {"kind": "min_cursor",  "in_param": "min_cursor",  "out_field": "min_cursor",  "has_more_field": "has_more", "page_size_param": "count"},
    {"kind": "next_cursor", "in_param": "next_cursor", "out_field": "next_cursor", "has_more_field": "has_more", "page_size_param": "count"},
    {"kind": "next_token",  "in_param": "next_token",  "out_field": "next_token",  "has_more_field": "has_more", "page_size_param": "count"},
    {"kind": "next_max_id", "in_param": "next_max_id", "out_field": "next_max_id", "has_more_field": "more_available", "page_size_param": "count"},
    {"kind": "pcursor",     "in_param": "pcursor",     "out_field": "pcursor",     "has_more_field": None,        "page_size_param": "count"},
    {"kind": "cursor",      "in_param": "cursor",      "out_field": "cursor",      "has_more_field": "has_more", "page_size_param": "count"},
    {"kind": "offset",      "in_param": "offset",      "out_field": None,          "has_more_field": None,        "page_size_param": "limit"},
    {"kind": "page",        "in_param": "page",        "out_field": None,          "has_more_field": None,        "page_size_param": "page_size"},
    {"kind": "page",        "in_param": "page",        "out_field": None,          "has_more_field": None,        "page_size_param": "size"},
]

# 多个翻译候选优先匹配
PARAM_LABELS: Dict[str, str] = {
    "sec_user_id": "用户 sec_id",
    "user_id": "用户 ID",
    "uniqueId": "用户 uniqueId",
    "aweme_id": "作品 ID",
    "video_id": "视频 ID",
    "challenge_id": "话题 ID",
    "music_id": "音乐 ID",
    "note_id": "笔记 ID",
    "share_url": "分享链接",
    "url": "链接",
    "keyword": "关键词",
    "query": "查询词",
    "cursor": "翻页游标",
    "max_cursor": "翻页游标 (max_cursor)",
    "min_cursor": "翻页游标 (min_cursor)",
    "next_cursor": "翻页游标",
    "pcursor": "翻页游标 (pcursor)",
    "offset": "偏移量",
    "page": "页码",
    "limit": "每页数量",
    "count": "每页数量",
    "page_size": "每页数量",
    "size": "每页数量",
    "page_token": "下页 Token",
    "next_token": "下页 Token",
    "language": "语言",
    "country": "国家",
    "region": "地区",
    "proxy": "代理 (可留空)",
    "type": "类型",
    "sort": "排序",
    "sort_type": "排序方式",
    "filter": "过滤",
    "search_id": "搜索会话 ID",
    "comment_id": "评论 ID",
    "reply_id": "回复 ID",
    "article_id": "文章 ID",
    "topic_id": "话题 ID",
}


def fetch_openapi(url: str, token: Optional[str], cache_path: Optional[Path] = None, force_refresh: bool = False) -> Dict[str, Any]:
    if cache_path and cache_path.exists() and not force_refresh:
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    req.add_header("User-Agent", "lobster-tikhub-catalog-builder/1.0")
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    spec = json.loads(data.decode("utf-8"))
    if cache_path:
        cache_path.write_bytes(data)
    return spec


def slugify(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text or "").strip("_").lower()
    return re.sub(r"_+", "_", text)


def detect_platform(tags: List[str]) -> Optional[Tuple[str, str, str, str]]:
    for raw_tag in tags or []:
        t = (raw_tag or "").lower().strip()
        if not t:
            continue
        if any(t.startswith(p) for p in SKIP_TAG_PREFIXES):
            return None
        for prefix, pid, name, icon in PLATFORM_MAP:
            if t.startswith(prefix):
                return prefix, pid, name, icon
    return None


def group_label_from_tag(tag: str) -> str:
    """Douyin-Web-API → Web，Xiaohongshu-App-V2-API → App V2"""
    parts = re.split(r"[-_\s]+", tag or "")
    if len(parts) <= 1:
        return tag
    keep = parts[1:]
    if keep and keep[-1].lower() in {"api"}:
        keep = keep[:-1]
    return " ".join(p.capitalize() for p in keep) or tag


def resolve_schema(schema: Dict[str, Any], components: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(schema, dict):
        return {}
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
        name = ref.split("/")[-1]
        return components.get("schemas", {}).get(name, {})
    return schema


def normalize_param_type(schema: Dict[str, Any]) -> Tuple[str, Optional[List[Any]]]:
    """返回 (type, enum_or_None)。type 是 string/integer/number/boolean/array。"""
    if not schema:
        return "string", None
    enum_vals = schema.get("enum")
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "string")
    if not t and "anyOf" in schema:
        for sub in schema["anyOf"]:
            sub_t = sub.get("type") if isinstance(sub, dict) else None
            if sub_t and sub_t != "null":
                t = sub_t
                if not enum_vals:
                    enum_vals = sub.get("enum")
                break
    return (t or "string"), enum_vals


def detect_pagination(params: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    names = {p["name"] for p in params}
    for rule in PAGINATION_RULES:
        if rule["in_param"] in names:
            page_size = rule.get("page_size_param")
            if page_size and page_size not in names:
                # try fallback page-size param names
                for cand in ("count", "limit", "page_size", "size"):
                    if cand in names:
                        rule = {**rule, "page_size_param": cand}
                        break
            return rule
    return None


def humanize(text: str, fallback: str) -> str:
    txt = (text or "").strip()
    if not txt:
        return fallback
    # 截掉末尾的英文「 | xxx」/换行
    txt = re.split(r"\r?\n", txt, maxsplit=1)[0]
    return txt[:80]


def build(spec: Dict[str, Any]) -> Dict[str, Any]:
    components = spec.get("components", {}) or {}
    paths = spec.get("paths", {}) or {}

    platforms_dict: Dict[str, Dict[str, Any]] = {}
    endpoints_index: Dict[str, Dict[str, Any]] = {}

    skipped = 0
    seen_ids = set()

    for path, ops in paths.items():
        if not isinstance(ops, dict):
            continue
        for method, op in ops.items():
            if method.lower() not in {"get", "post"}:
                continue
            if not isinstance(op, dict):
                continue
            tags = op.get("tags") or []
            plat = detect_platform(tags)
            if not plat:
                skipped += 1
                continue
            tag_prefix, platform_id, platform_name, platform_icon = plat
            primary_tag = next((t for t in tags if t.lower().startswith(tag_prefix)), tags[0])
            group_id = slugify(primary_tag)
            group_name = group_label_from_tag(primary_tag)

            # operationId 优先；没有则用 path
            op_id = (op.get("operationId") or "").strip()
            base_id = slugify(op_id) if op_id else slugify(path)
            # 简化 operationId 中冗余的 _api_v1_xxx_get 后缀
            base_id = re.sub(r"_api_v\d+_[a-z0-9_]+_(get|post)$", "", base_id)
            base_id = re.sub(r"_(get|post)$", "", base_id)
            endpoint_id = f"{platform_id}_{base_id}" if not base_id.startswith(platform_id) else base_id
            # 去重
            uniq = endpoint_id
            i = 2
            while uniq in seen_ids:
                uniq = f"{endpoint_id}_{i}"
                i += 1
            endpoint_id = uniq
            seen_ids.add(endpoint_id)

            # 整理参数
            params_out: List[Dict[str, Any]] = []
            for p in op.get("parameters") or []:
                if not isinstance(p, dict):
                    continue
                if p.get("in") not in {"query", "path"}:
                    continue
                schema = resolve_schema(p.get("schema") or {}, components)
                ptype, enum_vals = normalize_param_type(schema)
                default = schema.get("default")
                desc = humanize(p.get("description", ""), p["name"])
                label = PARAM_LABELS.get(p["name"], desc or p["name"])
                params_out.append({
                    "name": p["name"],
                    "in": p["in"],
                    "type": ptype,
                    "required": bool(p.get("required")),
                    "default": default,
                    "enum": enum_vals,
                    "label": label,
                    "description": desc,
                })

            # 翻页协议
            pagination = detect_pagination(params_out)

            title = humanize(op.get("summary", ""), endpoint_id)
            summary = op.get("description", "") or op.get("summary", "")
            summary = (summary or "").strip()[:600]

            entry = {
                "id": endpoint_id,
                "platform": platform_id,
                "group": group_id,
                "title": title,
                "summary": summary,
                "method": method.upper(),
                "path": path,
                "params": params_out,
                "pagination": pagination,
            }

            plat_entry = platforms_dict.setdefault(platform_id, {
                "id": platform_id,
                "name": platform_name,
                "icon": platform_icon,
                "groups": {},
            })
            grp_entry = plat_entry["groups"].setdefault(group_id, {
                "id": group_id,
                "name": group_name,
                "endpoints": [],
            })
            grp_entry["endpoints"].append(entry)
            endpoints_index[endpoint_id] = entry

    # dict → list（保持稳定顺序）
    platforms_list = []
    for plat in platforms_dict.values():
        groups_list = sorted(plat["groups"].values(), key=lambda g: g["name"])
        for g in groups_list:
            g["endpoints"].sort(key=lambda e: e["title"])
        plat["groups"] = groups_list
        platforms_list.append(plat)
    platforms_list.sort(key=lambda p: p["name"])

    catalog = {
        "version": "1.0",
        "generated_at": int(time.time()),
        "openapi_title": spec.get("info", {}).get("title", ""),
        "platform_count": len(platforms_list),
        "endpoint_count": len(endpoints_index),
        "skipped_paths": skipped,
        "platforms": platforms_list,
        "endpoints_index": endpoints_index,
    }
    return catalog


def main(argv: List[str]) -> int:
    out_path = DEFAULT_OUTPUT
    cache_path = HERE / ".openapi_cache.json"
    spec_url = os.environ.get("TIKHUB_OPENAPI_URL", DEFAULT_OPENAPI_URL)
    token = os.environ.get("TIKHUB_API_KEY", "").strip() or None
    force_refresh = "--refresh" in argv

    print(f"[tikhub_catalog] fetching openapi from {spec_url}")
    spec = fetch_openapi(spec_url, token, cache_path=cache_path, force_refresh=force_refresh)
    print(f"[tikhub_catalog] paths: {len(spec.get('paths', {}))}")
    catalog = build(spec)
    out_path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    size_kb = out_path.stat().st_size / 1024
    print(f"[tikhub_catalog] wrote {out_path} ({size_kb:.1f} KB)")
    print(f"[tikhub_catalog] platforms={catalog['platform_count']} endpoints={catalog['endpoint_count']} skipped={catalog['skipped_paths']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
