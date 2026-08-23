from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable


class RegionalAnalysisError(RuntimeError):
    """区域分析输入、模型调用或结果校验异常。"""


def extract_json_object(text: str) -> dict[str, Any] | None:
    """从模型文本中寻找第一个可完整解码的 JSON 对象，避免贪婪正则误截取。"""
    if not isinstance(text, str):
        return None
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _ollama_chat(
    prompt: str,
    model_name: str,
    *,
    base_url: str = "http://127.0.0.1:11434",
    context_window: int = 8192,
    max_output_tokens: int = 2200,
) -> str:
    """调用本机 Ollama 非流式接口，并在完成后主动释放文本模型。"""
    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "think": False,
        "keep_alive": 0,
        "options": {
            "temperature": 0.1,
            "num_ctx": context_window,
            "num_predict": max_output_tokens,
        },
    }
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/chat",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            envelope = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RegionalAnalysisError(f"无法连接本机 Ollama：{exc}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RegionalAnalysisError(f"Ollama 返回内容无法读取：{exc}") from exc

    content = envelope.get("message", {}).get("content", "")
    if not content:
        raise RegionalAnalysisError(f"模型 {model_name} 未返回内容。")
    return content


def call_ollama_for_json(
    prompt: str,
    model_name: str,
    validator: Callable[[dict[str, Any]], list[str]],
    *,
    base_url: str = "http://127.0.0.1:11434",
    context_window: int = 8192,
    max_output_tokens: int = 2200,
) -> dict[str, Any]:
    """调用模型并校验 JSON；首次失败时只允许进行一次定向修复。"""
    raw_text = _ollama_chat(prompt, model_name, base_url=base_url, context_window=context_window, max_output_tokens=max_output_tokens)
    parsed = extract_json_object(raw_text)
    errors = ["没有解析到 JSON 对象"] if parsed is None else validator(parsed)
    if not errors and parsed is not None:
        return parsed
    repair_prompt = f"""
你需要修复下面的 JSON 输出。只能修复格式、字段和引用问题，不得新增输入资料中没有的地质事实或数值。

校验错误：
{json.dumps(errors, ensure_ascii=False)}

原始输出：
{raw_text[:8000]}

请只输出修复后的严格 JSON 对象，不要 Markdown，不要解释。
""".strip()
    repaired_text = _ollama_chat(repair_prompt, model_name, base_url=base_url, context_window=context_window, max_output_tokens=max_output_tokens)
    repaired = extract_json_object(repaired_text)
    final_errors = ["修复后仍未解析到 JSON 对象"] if repaired is None else validator(repaired)
    if final_errors or repaired is None:
        raise RegionalAnalysisError("；".join(final_errors))
    return repaired


def build_regional_prompt(project_name: str, route: str, observation_payload: dict[str, Any]) -> str:
    """构造只包含结构化观察记录的区域分析 Prompt。"""
    compact_records: dict[str, Any] = {}
    for point_id, record in observation_payload.items():
        visual = record.get("视觉观察", {})
        if not isinstance(visual, dict):
            visual = {}
        structured = visual.get("结构化解译", {})
        if not isinstance(structured, dict):
            structured = {}
        compact_records[point_id] = {
            "观察点编号": point_id,
            "空间信息": record.get("空间信息", {}),
            "空间拓扑": record.get("空间拓扑", {}),
            "路线节点": record.get("路线节点", {}),
            "照片证据": record.get("照片证据", []),
            "直接观察事实": visual.get("直接观察事实", visual.get("可见要素", [])),
            "图像类型": visual.get("图像类型", ""),
            "自然语言解译": str(visual.get("自然语言解译", ""))[:1200],
            "结构化解译": structured,
            "人工修正": record.get("人工修正", ""),
            "审核状态": record.get("审核状态", "未审核"),
        }
    compact_payload = json.dumps(compact_records, ensure_ascii=False, separators=(",", ":"))
    point_ids = json.dumps(list(observation_payload.keys()), ensure_ascii=False)
    return f"""
你是全国大学生地质技能竞赛的区域地质综合分析助手。你看不到原始图片，也不能读取 OSGB 或 DasViewer 模型，只能使用下列结构化观察点记录和选手从 DasViewer 人工录入的空间测量资料。

项目名称：{project_name}
路线：{route}
本次必须分析的全部观察点编号（一个都不能遗漏）：{point_ids}
观察点资料：{compact_payload}

规则：
1. 必须逐一检查并覆盖输入中的每个观察点；资料不足的点也必须列出并写明“资料不足”，不得静默忽略。
2. 只使用记录中的空间信息、空间拓扑、路线节点和人工空间关系描述，不得猜测缺失数值。
3. 严格区分直接观察事实、初步解释和需要验证的假设。
4. 每条地质结论必须引用支持它的观察点编号。

只输出一个严格 JSON 对象：
{{
  "区域地质概况": "结论中使用[G001]形式引用观察点",
  "空间事实": [],
  "空间关系解释": [],
  "主要岩性组合": [{{"描述": "", "观察点编号": ["G001"]}}],
  "构造关系": [{{"描述": "", "观察点编号": ["G001"]}}],
  "地层关系": [{{"描述": "", "观察点编号": ["G001"]}}],
  "空间演化解释": "",
  "观察点覆盖情况": [
    {{"观察点编号": "G001", "点位事实": "", "区域意义": "", "证据状态": "充分/部分/不足"}}
  ],
  "证据链": [
    {{
      "结论": "",
      "观察点编号": ["G001"],
      "照片编号": ["G001-P01"],
      "空间依据": "",
      "直接依据": "",
      "证据级别": "直接观察事实/空间关系说明/待核验"
    }}
  ],
  "不确定性": [],
  "补充调查建议": [],
  "置信度": 0.0
}}
""".strip()


def validate_regional_result(result: dict[str, Any], valid_points: set[str], valid_photos: set[str]) -> list[str]:
    """验证区域分析字段、置信度和证据引用。"""
    errors: list[str] = []
    required = {
        "区域地质概况": str,
        "主要岩性组合": list,
        "构造关系": list,
        "地层关系": list,
        "空间演化解释": str,
        "观察点覆盖情况": list,
        "证据链": list,
        "不确定性": list,
        "置信度": (int, float),
    }
    for key, expected_type in required.items():
        if key not in result:
            errors.append(f"缺少字段：{key}")
        elif not isinstance(result[key], expected_type):
            errors.append(f"字段类型错误：{key}")
    confidence = result.get("置信度")
    if isinstance(confidence, (int, float)) and not 0 <= float(confidence) <= 1:
        errors.append("置信度必须在 0 到 1 之间")
    coverage = result.get("观察点覆盖情况", [])
    covered_points = set()
    if isinstance(coverage, list):
        for index, item in enumerate(coverage):
            if not isinstance(item, dict):
                errors.append(f"观察点覆盖情况第{index + 1}项必须是对象")
                continue
            point_id = item.get("观察点编号")
            if point_id not in valid_points:
                errors.append(f"观察点覆盖情况第{index + 1}项引用未知观察点：{point_id}")
            else:
                covered_points.add(point_id)
        missing_points = valid_points - covered_points
        if missing_points:
            errors.append(f"观察点覆盖情况缺少观察点：{sorted(missing_points)}")
    for field in ("主要岩性组合", "构造关系", "地层关系"):
        for index, item in enumerate(result.get(field, []) if isinstance(result.get(field), list) else []):
            if not isinstance(item, dict):
                errors.append(f"{field} 第{index + 1}项必须是对象")
                continue
            references = item.get("观察点编号", [])
            if isinstance(references, str):
                references = [references]
            if not references:
                errors.append(f"{field} 第{index + 1}项没有观察点引用")
            unknown = set(references or []) - valid_points
            if unknown:
                errors.append(f"{field} 第{index + 1}项引用未知观察点：{sorted(unknown)}")
    for index, item in enumerate(result.get("证据链", []) if isinstance(result.get("证据链"), list) else []):
        if not isinstance(item, dict):
            errors.append(f"证据链第{index + 1}项必须是对象")
            continue
        point_refs = item.get("观察点编号", [])
        photo_refs = item.get("照片编号", item.get("证据照片编号", []))
        if isinstance(point_refs, str):
            point_refs = [point_refs]
        if isinstance(photo_refs, str):
            photo_refs = [photo_refs]
        if "照片编号" not in item and "证据照片编号" in item:
            item["照片编号"] = photo_refs
        if not point_refs:
            inferred_points = {
                str(photo_id).split("-P", 1)[0]
                for photo_id in photo_refs
                if isinstance(photo_id, str) and "-P" in photo_id
            }
            if inferred_points and inferred_points <= valid_points:
                item["观察点编号"] = sorted(inferred_points)
                point_refs = item["观察点编号"]
            else:
                errors.append(f"证据链第{index + 1}项没有观察点引用")
        unknown_points = set(point_refs or []) - valid_points
        unknown_photos = set(photo_refs or []) - valid_photos
        if unknown_points:
            errors.append(f"证据链第{index + 1}项引用未知观察点：{sorted(unknown_points)}")
        if unknown_photos:
            errors.append(f"证据链第{index + 1}项引用未知照片：{sorted(unknown_photos)}")
    return errors


def analyze_region(project: dict[str, Any], observation_payload: dict[str, Any], *, model_name: str = "qwen3:8b", base_url: str = "http://127.0.0.1:11434") -> dict[str, Any]:
    """调用 qwen3 文本模型完成多个观察点的区域综合分析。"""
    if not isinstance(observation_payload, dict) or len(observation_payload) < 2:
        raise RegionalAnalysisError("区域综合分析至少需要两个已入库观察点。")
    valid_points = set(observation_payload)
    valid_photos = {
        photo.get("照片编号")
        for record in observation_payload.values()
        for photo in record.get("照片证据", [])
        if isinstance(photo, dict) and photo.get("照片编号")
    }
    prompt = build_regional_prompt(project.get("项目名称", ""), project.get("路线", ""), observation_payload)
    return call_ollama_for_json(prompt, model_name, lambda result: validate_regional_result(result, valid_points, valid_photos), base_url=base_url, context_window=8192, max_output_tokens=2200)
