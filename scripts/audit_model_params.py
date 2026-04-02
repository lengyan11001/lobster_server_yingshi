#!/usr/bin/env python3
"""
全量模型参数审计脚本

功能：
1. 从 xskill API 获取所有模型的 params_schema
2. 检查 _normalize_*_payload 函数是否正确处理所有参数
3. 特别检查 duration/aspect_ratio/resolution 的枚举合法性
4. 生成问题清单，按风险等级排序

使用方法：
    python scripts/audit_model_params.py
    python scripts/audit_model_params.py --fix  # 自动修复部分问题
"""

import json
import sys
import os
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any, Optional, Set, Tuple
from dataclasses import dataclass, field
from enum import Enum
import httpx
import re


def _log(msg: str) -> None:
    print(msg, flush=True)

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# 导入 normalize 函数（需要先设置环境变量等）
try:
    # 尝试导入，如果失败则使用模拟函数
    from mcp.http_server import _normalize_image_generate_payload, _normalize_video_generate_payload
except ImportError as e:
    print(f"⚠️  无法导入 normalize 函数: {e}")
    print("   将使用模拟函数进行基本检查")
    
    def _normalize_image_generate_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        """模拟函数，返回原 payload"""
        return payload or {}
    
    def _normalize_video_generate_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        """模拟函数，返回原 payload"""
        return payload or {}


class RiskLevel(Enum):
    CRITICAL = "CRITICAL"  # 必填参数缺失、枚举值错误
    HIGH = "HIGH"  # 参数类型错误、默认值不当
    MEDIUM = "MEDIUM"  # 参数未传递、可选参数处理不当
    LOW = "LOW"  # 参数命名不一致、文档缺失


@dataclass
class ParamIssue:
    """参数问题"""
    model_id: str
    param_name: str
    risk_level: RiskLevel
    issue_type: str  # missing_required, invalid_enum, type_mismatch, missing_param, etc.
    description: str
    expected: Any = None
    actual: Any = None
    location: str = ""  # 代码位置


@dataclass
class ModelAuditResult:
    """模型审计结果"""
    model_id: str
    category: str  # image/video
    schema: Dict[str, Any]
    issues: List[ParamIssue] = field(default_factory=list)
    handled_params: Set[str] = field(default_factory=set)
    missing_params: Set[str] = field(default_factory=set)


def _xskill_http_headers() -> Dict[str, str]:
    """与官方 CLI 一致：有 Key 时带 Bearer，便于拉 /docs。"""
    h = {"Accept": "application/json"}
    token = (os.environ.get("XSKILL_API_KEY") or os.environ.get("SUTUI_SERVER_TOKEN") or "").strip()
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


class ModelParamAuditor:
    """模型参数审计器"""
    
    def __init__(self, xskill_base_url: str = "https://api.xskill.ai"):
        self.xskill_base_url = xskill_base_url.rstrip("/")
        self.results: List[ModelAuditResult] = []
        
    async def fetch_all_models(self) -> List[Dict[str, Any]]:
        """从 xskill API 获取所有模型列表"""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    f"{self.xskill_base_url}/api/v3/mcp/models",
                    headers=_xskill_http_headers(),
                )
                resp.raise_for_status()
                data = resp.json()
                return data.get("data", {}).get("models", [])
        except Exception as e:
            _log(f"❌ 获取模型列表失败: {e}")
            return []
    
    async def fetch_model_schema(
        self, client: httpx.AsyncClient, model_id: str
    ) -> Optional[Dict[str, Any]]:
        """获取单个模型的参数 schema"""
        try:
            encoded_id = model_id.replace("/", "%2F")
            resp = await client.get(
                f"{self.xskill_base_url}/api/v3/models/{encoded_id}/docs",
                params={"lang": "zh"},
                headers=_xskill_http_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", {}).get("params_schema")
        except Exception as e:
            _log(f"⚠️  获取模型 {model_id} 的 schema 失败: {e}")
            return None

    async def fetch_schemas_parallel(
        self,
        model_ids: List[str],
        concurrency: int = 12,
    ) -> Dict[str, Optional[Dict[str, Any]]]:
        """并发拉取多个模型的 params_schema，减少总耗时。"""
        sem = asyncio.Semaphore(max(1, concurrency))
        out: Dict[str, Optional[Dict[str, Any]]] = {}

        async with httpx.AsyncClient(timeout=45.0) as client:

            async def one(mid: str) -> Tuple[str, Optional[Dict[str, Any]]]:
                async with sem:
                    sch = await self.fetch_model_schema(client, mid)
                    return mid, sch

            pairs = await asyncio.gather(*[one(mid) for mid in model_ids])
        for mid, sch in pairs:
            out[mid] = sch
        return out
    
    def extract_handled_models_from_code(self) -> Dict[str, Dict[str, Any]]:
        """从代码中提取已处理的模型列表"""
        # 读取 http_server.py 文件
        code_file = Path(__file__).parent.parent / "mcp" / "http_server.py"
        if not code_file.exists():
            return {}
        
        content = code_file.read_text(encoding="utf-8")
        handled_models = {}
        
        # 提取图片模型
        image_patterns = [
            (r'if\s+"jimeng-"', "jimeng-*"),
            (r'if\s+"flux-2', "flux-2/flash"),
            (r'if\s+"seedream"', "seedream"),
            (r'if\s+"nano-banana"', "nano-banana"),
        ]
        
        # 提取视频模型
        video_patterns = [
            (r'"super-seed2"', "st-ai/super-seed2"),
            (r'"wan/v2\.6"', "wan/v2.6/*"),
            (r'"hailuo"', "hailuo"),
            (r'"vidu"', "vidu"),
            (r'"seedance/v1"', "seedance/v1/*"),
            (r'"sora-2"', "sora-2/*"),
            (r'"kling"', "kling"),
            (r'"veo"', "veo-3.1"),
            (r'"grok"', "grok"),
            (r'"jimeng"', "jimeng"),
        ]
        
        for pattern, model_name in image_patterns:
            if re.search(pattern, content):
                handled_models[model_name] = {"category": "image"}
        
        for pattern, model_name in video_patterns:
            if re.search(pattern, content):
                handled_models[model_name] = {"category": "video"}
        
        return handled_models

    @staticmethod
    def _first_http_example(prop: Dict[str, Any]) -> Optional[str]:
        ex = prop.get("examples")
        if isinstance(ex, list):
            for item in ex:
                if isinstance(item, str) and item.startswith("http"):
                    return item
        ex1 = prop.get("example")
        if isinstance(ex1, str) and ex1.startswith("http"):
            return ex1
        return None

    @classmethod
    def build_probe_payload(
        cls,
        model_id: str,
        category: str,
        schema: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        按 params_schema 构造探测用 payload：必填项尽量从 enum/examples/default 取值，
        duration 若枚举为字符串数字则传字符串，避免 int/str 假阳性。
        """
        properties: Dict[str, Any] = schema.get("properties") or {}
        required: List[str] = list(schema.get("required") or [])
        mid = model_id.lower()

        payload: Dict[str, Any] = {
            "model": model_id,
            "prompt": "audit probe",
        }

        if category == "image":
            payload.setdefault("num_images", 1)

        if category == "video":
            is_t2v = (
                "text-to-video" in mid
                and "image-to-video" not in mid
                and "reference-to-video" not in mid
                and "first-last" not in mid
                and "extend" not in mid
                and "remix" not in mid
                and "motion-control" not in mid
                and "video-to-video" not in mid
            )
            probe_img = "https://example.com/audit-probe.jpg"
            probe_vid = "https://example.com/audit-probe.mp4"
            if not is_t2v:
                payload["image_url"] = probe_img
                payload["filePaths"] = [probe_img]
                payload["media_files"] = [probe_img]
            else:
                payload.setdefault("aspect_ratio", "16:9")

            if "motion-control" in mid or "video-to-video" in mid:
                payload.setdefault("video_url", probe_vid)

        # duration：按 schema 选合法探测值（优先常见秒数）
        if "duration" in properties:
            dp = properties["duration"]
            if isinstance(dp.get("enum"), list) and dp["enum"]:
                enums = list(dp["enum"])
                pick = None
                for cand in ("6", 6, "5", 5, "10", 10, "15", 15, "4s", "6s", "8s"):
                    if cand in enums:
                        pick = cand
                        break
                payload["duration"] = pick if pick is not None else enums[0]
            else:
                payload.setdefault("duration", 5)

        # aspect_ratio：若有枚举则选 16:9 或首项
        if "aspect_ratio" in properties:
            ap = properties["aspect_ratio"]
            if isinstance(ap.get("enum"), list) and ap["enum"]:
                if "16:9" in ap["enum"]:
                    payload["aspect_ratio"] = "16:9"
                else:
                    payload["aspect_ratio"] = ap["enum"][0]
            elif "aspect_ratio" not in payload:
                payload["aspect_ratio"] = "16:9"

        if "resolution" in properties:
            rp = properties["resolution"]
            if isinstance(rp.get("enum"), list) and rp["enum"]:
                for cand in ("1080p", "720p", "480p"):
                    if cand in rp["enum"]:
                        payload["resolution"] = cand
                        break
                else:
                    payload["resolution"] = rp["enum"][0]
            else:
                payload.setdefault("resolution", "1080p")

        # 补齐 required（仍未出现的键）
        for key in required:
            if key in payload and payload[key] is not None and payload[key] != "":
                continue
            prop = properties.get(key) or {}
            if "default" in prop:
                payload[key] = prop["default"]
                continue
            if isinstance(prop.get("enum"), list) and prop["enum"]:
                payload[key] = prop["enum"][0]
                continue
            ex_url = cls._first_http_example(prop)
            if ex_url:
                payload[key] = ex_url
                continue
            ex = prop.get("examples")
            if isinstance(ex, list) and ex:
                payload[key] = ex[0]
                continue
            t = prop.get("type")
            if t == "string":
                if "url" in key.lower() or key.endswith("_url"):
                    payload[key] = "https://example.com/audit-probe.bin"
                else:
                    payload[key] = "probe"
            elif t == "integer":
                payload[key] = int(prop.get("minimum", 1)) if prop.get("minimum") is not None else 1
            elif t == "number":
                payload[key] = float(prop.get("minimum", 1.0)) if prop.get("minimum") is not None else 1.0
            elif t == "boolean":
                payload[key] = True
            elif t == "array":
                payload[key] = []
            elif t == "object":
                payload[key] = {}
            else:
                payload[key] = None

        return payload

    @staticmethod
    def _value_matches_enum(value: Any, enum_vals: List[Any]) -> bool:
        if value in enum_vals:
            return True
        # 字符串枚举与 int 互通：'5' <-> 5
        for ev in enum_vals:
            if isinstance(ev, str) and isinstance(value, int):
                if ev == str(value):
                    return True
                if ev.endswith("s") and ev[:-1].isdigit() and str(value) == ev[:-1]:
                    return True
            if isinstance(ev, str) and isinstance(value, str) and value.endswith("s") and value[:-1].isdigit():
                if ev == value or ev == value[:-1]:
                    return True
        return False

    @staticmethod
    def _numeric_for_range(value: Any) -> Optional[float]:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            s = value.strip().lower().rstrip("s")
            try:
                return float(s)
            except ValueError:
                return None
        return None

    def check_param_handling(
        self,
        model_id: str,
        schema: Dict[str, Any],
        category: str,
    ) -> List[ParamIssue]:
        """对 normalize 输出做一次 schema 校验（单遍、枚举兼容 int/str）。"""
        issues: List[ParamIssue] = []
        properties: Dict[str, Any] = schema.get("properties") or {}
        required: List[str] = list(schema.get("required") or [])

        test_payload = self.build_probe_payload(model_id, category, schema)

        if category == "image":
            try:
                normalized = _normalize_image_generate_payload(dict(test_payload))
            except Exception as e:
                issues.append(
                    ParamIssue(
                        model_id=model_id,
                        param_name="*",
                        risk_level=RiskLevel.CRITICAL,
                        issue_type="normalize_error",
                        description=f"normalize 函数执行失败: {e}",
                        location="mcp/http_server.py:_normalize_image_generate_payload",
                    )
                )
                return self._dedupe_issues(issues)
        else:
            try:
                normalized = _normalize_video_generate_payload(dict(test_payload))
            except Exception as e:
                issues.append(
                    ParamIssue(
                        model_id=model_id,
                        param_name="*",
                        risk_level=RiskLevel.CRITICAL,
                        issue_type="normalize_error",
                        description=f"normalize 函数执行失败: {e}",
                        location="mcp/http_server.py:_normalize_video_generate_payload",
                    )
                )
                return self._dedupe_issues(issues)

        for param in required:
            if param not in normalized or normalized[param] is None:
                issues.append(
                    ParamIssue(
                        model_id=model_id,
                        param_name=param,
                        risk_level=RiskLevel.CRITICAL,
                        issue_type="missing_required",
                        description=f"必填参数 {param} 未在 normalize 结果中或为空",
                        expected=properties.get(param),
                        actual=normalized.get(param),
                        location="mcp/http_server.py",
                    )
                )

        for param_name, param_schema in properties.items():
            if param_name not in normalized:
                continue
            value = normalized[param_name]

            if isinstance(param_schema.get("enum"), list) and param_schema["enum"]:
                if not self._value_matches_enum(value, param_schema["enum"]):
                    issues.append(
                        ParamIssue(
                            model_id=model_id,
                            param_name=param_name,
                            risk_level=RiskLevel.CRITICAL,
                            issue_type="invalid_enum",
                            description=f"参数 {param_name} 不在 schema 枚举中（已做 int/str 兼容比较）",
                            expected=param_schema["enum"],
                            actual=value,
                            location="mcp/http_server.py",
                        )
                    )

            param_type = param_schema.get("type")
            if not param_type:
                continue
            type_ok = False
            if param_type == "string" and isinstance(value, str):
                type_ok = True
            elif param_type == "integer" and isinstance(value, int) and not isinstance(value, bool):
                type_ok = True
            elif param_type == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
                type_ok = True
            elif param_type == "boolean" and isinstance(value, bool):
                type_ok = True
            elif param_type == "array" and isinstance(value, list):
                type_ok = True
            elif param_type == "object" and isinstance(value, dict):
                type_ok = True
            # schema 写 string 但上游常用数字字符串：枚举已通过则不再报类型
            if (
                not type_ok
                and param_type == "string"
                and isinstance(param_schema.get("enum"), list)
                and self._value_matches_enum(value, param_schema["enum"])
            ):
                type_ok = True
            if not type_ok:
                issues.append(
                    ParamIssue(
                        model_id=model_id,
                        param_name=param_name,
                        risk_level=RiskLevel.HIGH,
                        issue_type="type_mismatch",
                        description=f"参数 {param_name} 类型与 schema 声明不一致",
                        expected=param_type,
                        actual=type(value).__name__,
                        location="mcp/http_server.py",
                    )
                )

            num = self._numeric_for_range(value)
            if num is not None:
                min_val = param_schema.get("minimum")
                max_val = param_schema.get("maximum")
                if min_val is not None and num < float(min_val):
                    issues.append(
                        ParamIssue(
                            model_id=model_id,
                            param_name=param_name,
                            risk_level=RiskLevel.HIGH,
                            issue_type="out_of_range",
                            description=f"{param_name} 小于 minimum",
                            expected=f">= {min_val}",
                            actual=value,
                            location="mcp/http_server.py",
                        )
                    )
                if max_val is not None and num > float(max_val):
                    issues.append(
                        ParamIssue(
                            model_id=model_id,
                            param_name=param_name,
                            risk_level=RiskLevel.HIGH,
                            issue_type="out_of_range",
                            description=f"{param_name} 大于 maximum",
                            expected=f"<= {max_val}",
                            actual=value,
                            location="mcp/http_server.py",
                        )
                    )

        return self._dedupe_issues(issues)

    @staticmethod
    def _dedupe_issues(issues: List[ParamIssue]) -> List[ParamIssue]:
        seen: Set[Tuple[str, str, str]] = set()
        out: List[ParamIssue] = []
        for it in issues:
            key = (it.param_name, it.issue_type, it.description[:120])
            if key in seen:
                continue
            seen.add(key)
            out.append(it)
        return out
    
    async def audit_all_models(
        self,
        limit: int = 0,
        concurrency: int = 12,
    ) -> List[ModelAuditResult]:
        """审计所有模型。limit>0 时只审计前 N 个（调试用）。"""
        _log("🔍 开始全量模型参数审计...")

        models = await self.fetch_all_models()
        _log(f"📋 找到 {len(models)} 个模型")

        image_models = [m for m in models if m.get("category") == "image"]
        video_models = [m for m in models if m.get("category") == "video"]
        _log(f"  - 图片模型: {len(image_models)}")
        _log(f"  - 视频模型: {len(video_models)}")

        # 仅审计生图/生视频；对话、音频等不走 normalize_image/video，避免噪声
        rows = [
            m
            for m in models
            if m.get("id")
            and str(m.get("category") or "") in ("image", "video")
        ]
        skipped_cat = len(models) - len(rows)
        if skipped_cat:
            _log(f"  - 按类别跳过（非 image/video）: {skipped_cat} 个")
        if limit and limit > 0:
            rows = rows[:limit]
            _log(f"  - 本次仅审计前 {limit} 个（--limit）")

        ids = [str(m["id"]) for m in rows]
        _log(f"⏳ 并发拉取 params_schema（并发={concurrency}）...")
        schemas = await self.fetch_schemas_parallel(ids, concurrency=concurrency)
        ok_schema = sum(1 for s in schemas.values() if s)
        _log(f"✅ 成功拉取 schema: {ok_schema} / {len(ids)}")

        all_results: List[ModelAuditResult] = []
        for model in rows:
            model_id = str(model["id"])
            category = str(model["category"])
            schema = schemas.get(model_id)
            if not schema:
                _log(f"  ⚠️  跳过（无 schema）: {model_id}")
                continue

            issues = self.check_param_handling(model_id, schema, category)
            result = ModelAuditResult(
                model_id=model_id,
                category=category,
                schema=schema,
                issues=issues,
            )
            all_results.append(result)
            if issues:
                _log(f"  ❌ {model_id}: {len(issues)} 个问题")
            else:
                _log(f"  ✅ {model_id}")

        self.results = all_results
        return all_results
    
    def generate_report(self, output_file: Optional[str] = None) -> str:
        """生成审计报告"""
        if not self.results:
            return "无审计结果"
        
        # 按风险等级统计
        critical_count = sum(len([i for i in r.issues if i.risk_level == RiskLevel.CRITICAL]) for r in self.results)
        high_count = sum(len([i for i in r.issues if i.risk_level == RiskLevel.HIGH]) for r in self.results)
        medium_count = sum(len([i for i in r.issues if i.risk_level == RiskLevel.MEDIUM]) for r in self.results)
        low_count = sum(len([i for i in r.issues if i.risk_level == RiskLevel.LOW]) for r in self.results)
        
        report_lines = [
            "# 模型参数审计报告",
            "",
            f"**审计时间**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC",
            f"**审计模型数**: {len(self.results)}",
            "",
            "> 方法：仅 image/video；按 xskill `params_schema` 构造探测 payload；校验 `_normalize_*_payload` 输出；枚举支持 int/字符串互通；见 `docs/参数审计方法对比.md`。",
            "",
            "## 问题统计",
            "",
            f"- 🔴 CRITICAL: {critical_count} 个",
            f"- 🟠 HIGH: {high_count} 个",
            f"- 🟡 MEDIUM: {medium_count} 个",
            f"- 🟢 LOW: {low_count} 个",
            "",
            "## 详细问题清单",
            "",
        ]
        
        # 按风险等级和模型分组
        for risk_level in [RiskLevel.CRITICAL, RiskLevel.HIGH, RiskLevel.MEDIUM, RiskLevel.LOW]:
            level_issues = []
            for result in self.results:
                for issue in result.issues:
                    if issue.risk_level == risk_level:
                        level_issues.append((result.model_id, issue))
            
            if level_issues:
                report_lines.append(f"### {risk_level.value} 级别问题 ({len(level_issues)} 个)")
                report_lines.append("")
                
                for model_id, issue in level_issues:
                    report_lines.append(f"#### {model_id} - {issue.param_name}")
                    report_lines.append(f"- **问题类型**: {issue.issue_type}")
                    report_lines.append(f"- **描述**: {issue.description}")
                    if issue.expected is not None:
                        report_lines.append(f"- **期望**: {issue.expected}")
                    if issue.actual is not None:
                        report_lines.append(f"- **实际**: {issue.actual}")
                    report_lines.append(f"- **位置**: {issue.location}")
                    report_lines.append("")
        
        report = "\n".join(report_lines)
        
        if output_file:
            Path(output_file).write_text(report, encoding="utf-8")
            _log(f"\n📄 报告已保存到: {output_file}")
        
        return report


async def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description="模型参数审计工具")
    parser.add_argument("--output", "-o", default="audit_report.md", help="输出报告文件")
    parser.add_argument("--xskill-url", default="https://api.xskill.ai", help="xskill API 地址")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="只审计前 N 个模型（0 表示全部）",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=12,
        help="拉取 /docs 的并发数",
    )
    args = parser.parse_args()

    auditor = ModelParamAuditor(xskill_base_url=args.xskill_url)
    results = await auditor.audit_all_models(
        limit=args.limit,
        concurrency=args.concurrency,
    )

    _log(f"\n✅ 审计完成，共审计 {len(results)} 个模型")

    report = auditor.generate_report(output_file=args.output)
    _log(f"\n📊 报告预览（前800字符）:")
    _log(report[:800])


if __name__ == "__main__":
    asyncio.run(main())
