"""V4.0 AI 辅助地质剖面参数生成与确定性绘图。

AI 只负责从已有结构化证据中整理参数；Matplotlib 负责绘图。
任何缺少来源的高程、距离、厚度、产状和断层性质都不得自动补造。
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from regional_analysis import call_ollama_for_json


class ProfileGenerationError(RuntimeError):
    """剖面参数不足、格式错误或绘图失败。"""


def build_profile_prompt(
    observation_payload: dict[str, Any],
    regional_result: dict[str, Any],
    direction: str,
) -> str:
    """构造剖面参数 Prompt，明确禁止模型补造测量数值。"""
    source = json.dumps(
        {"观察点": observation_payload, "区域综合分析": regional_result},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"""
你是地质剖面参数整理助手。你只能把输入 JSON 中已经出现的测量值和有编号的地质证据整理成剖面参数，不能猜测任何数值。

计划剖面方向：{direction}
输入资料：{source}

强制规则：
1. 高程、沿剖面距离、厚度、倾角、断层位置必须能在输入资料中找到明确数值；空间数值只能来自地质人员人工输入的空间拓扑字段。
2. 观察点顺序、下一观察点编号、点间距离（m）、方位、高程、高程差和空间关系描述只能作为人工事实使用，不得自动排序、计算或改写。
2. 只有方位文字而没有倾角时，dip 必须为 null。
3. 没有可靠位移证据时，断层性质写“无法确定”。
4. 每个地层、断层、侵入体和产状符号都必须填写“证据观察点”。
5. 少于两个具有高程和沿剖面距离的观察点时，“数据充分性”必须为 false。
6. 数据不足时不要生成虚假地形线或地层参数，只列出缺失资料。

只输出严格 JSON：
{{
  "数据充分性": false,
  "无法生成原因": [],
  "剖面方向": "{direction}",
  "比例尺": "1:1000",
  "地形线": [{{"距离": 0.0, "高程": 0.0, "观察点编号": "G001"}}],
  "地层": [
    {{
      "name": "",
      "thickness": null,
      "dip": null,
      "dip_direction": "",
      "pattern": "",
      "color": "",
      "证据观察点": ["G001"]
    }}
  ],
  "断层": [],
  "侵入体": [],
  "产状符号": [],
  "人工确认事项": []
}}

不要输出 Markdown 或说明文字。
""".strip()


def validate_profile_data(profile: dict[str, Any]) -> list[str]:
    """验证剖面 JSON 是否具备安全绘图所需的真实参数。"""
    errors: list[str] = []
    if not isinstance(profile, dict):
        return ["剖面参数必须是 JSON 对象"]
    if not isinstance(profile.get("数据充分性"), bool):
        errors.append("数据充分性必须是布尔值")
        return errors
    if profile.get("数据充分性") is False:
        reasons = profile.get("无法生成原因", [])
        if not isinstance(reasons, list) or not reasons:
            errors.append("数据不足时必须列出无法生成原因")
        return errors

    if not str(profile.get("剖面方向", "")).strip():
        errors.append("缺少剖面方向")
    terrain = profile.get("地形线")
    if not isinstance(terrain, list) or len(terrain) < 2:
        errors.append("地形线至少需要两个实测点")
    else:
        previous_distance: float | None = None
        for index, point in enumerate(terrain):
            try:
                distance = float(point["距离"])
                float(point["高程"])
            except (KeyError, TypeError, ValueError):
                errors.append(f"地形线第{index + 1}点缺少有效距离或高程")
                continue
            if previous_distance is not None and distance <= previous_distance:
                errors.append("地形线距离必须严格递增")
            previous_distance = distance

    strata = profile.get("地层")
    if not isinstance(strata, list) or not strata:
        errors.append("至少需要一个有证据支持的地层或岩性单元")
    else:
        for index, layer in enumerate(strata):
            if not isinstance(layer, dict) or not str(layer.get("name", "")).strip():
                errors.append(f"地层第{index + 1}项缺少名称")
                continue
            try:
                thickness = float(layer.get("thickness"))
                if thickness <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                errors.append(f"地层 {layer.get('name', index + 1)} 缺少有效厚度")
            dip = layer.get("dip")
            if dip is None:
                errors.append(f"地层 {layer.get('name', index + 1)} 缺少实测倾角，需人工确认")
            else:
                try:
                    dip_value = float(dip)
                    if not 0 <= dip_value < 90:
                        raise ValueError
                except (TypeError, ValueError):
                    errors.append(f"地层 {layer.get('name', index + 1)} 倾角必须在 0 到 90 度之间")
            references = layer.get("证据观察点", [])
            if not isinstance(references, list) or not references:
                errors.append(f"地层 {layer.get('name', index + 1)} 缺少证据观察点")

    for index, fault in enumerate(profile.get("断层", [])):
        try:
            float(fault["位置"])
            dip = float(fault["dip"])
            if not 0 < dip < 90:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            errors.append(f"断层第{index + 1}项缺少有效位置或倾角")
        if not fault.get("证据观察点"):
            errors.append(f"断层第{index + 1}项缺少证据观察点")
    return errors


def generate_profile_parameters(
    observation_payload: dict[str, Any],
    regional_result: dict[str, Any],
    direction: str,
    *,
    model_name: str = "qwen3:8b",
    base_url: str = "http://127.0.0.1:11434",
) -> dict[str, Any]:
    """调用本地文本模型生成可人工复核的剖面参数草案。"""
    if not observation_payload:
        raise ProfileGenerationError("没有可用于剖面的观察点数据。")
    prompt = build_profile_prompt(observation_payload, regional_result, direction)
    return call_ollama_for_json(
        prompt,
        model_name,
        validate_profile_data,
        base_url=base_url,
        context_window=8192,
        max_output_tokens=2200,
    )


def _nice_scale_length(total_distance: float) -> float:
    """根据剖面长度计算易读的线段比例尺长度。"""
    target = max(total_distance / 5, 1)
    exponent = 10 ** math.floor(math.log10(target))
    normalized = target / exponent
    base = 1 if normalized < 2 else 2 if normalized < 5 else 5
    return base * exponent


def _safe_basename(value: str) -> str:
    """生成安全的剖面输出文件名。"""
    cleaned = re.sub(r'[^0-9A-Za-z_\-\u4e00-\u9fff]+', "_", value.strip())
    return cleaned.strip("_")[:80] or "geology_profile"


def draw_geological_profile(
    profile: dict[str, Any],
    output_dir: str | Path,
    basename: str | None = None,
) -> dict[str, str]:
    """使用 Matplotlib 绘制 PNG/SVG 地质剖面，不调用生图模型。"""
    errors = validate_profile_data(profile)
    if errors:
        raise ProfileGenerationError("；".join(errors))

    try:
        os.environ.setdefault(
            "MPLCONFIGDIR",
            str(Path(tempfile.gettempdir()) / "geology_ai_matplotlib"),
        )
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
        from matplotlib.patches import Patch, Polygon
    except ImportError as exc:
        raise ProfileGenerationError("缺少 matplotlib 或 numpy，无法绘制剖面。") from exc

    output_path = Path(output_dir).resolve()
    output_path.mkdir(parents=True, exist_ok=True)
    filename = _safe_basename(
        basename or f"geology_profile_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )
    png_path = output_path / f"{filename}.png"
    svg_path = output_path / f"{filename}.svg"

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial Unicode MS", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    terrain = sorted(profile["地形线"], key=lambda item: float(item["距离"]))
    measured_x = np.asarray([float(item["距离"]) for item in terrain], dtype=float)
    measured_y = np.asarray([float(item["高程"]) for item in terrain], dtype=float)
    x = np.linspace(measured_x.min(), measured_x.max(), max(300, len(measured_x) * 80))
    topography = np.interp(x, measured_x, measured_y)

    fig, ax = plt.subplots(figsize=(14, 7), constrained_layout=True)
    ax.plot(x, topography, color="#2f2f2f", linewidth=2.2, label="地形线", zorder=10)
    ax.scatter(measured_x, measured_y, color="#111111", s=24, zorder=11)
    for point in terrain:
        point_id = str(point.get("观察点编号", ""))
        if point_id:
            ax.annotate(
                point_id,
                (float(point["距离"]), float(point["高程"])),
                xytext=(0, 8),
                textcoords="offset points",
                ha="center",
                fontsize=8,
            )

    default_colors = ["#e8d39a", "#c9d8a6", "#a8cbd4", "#d6b6c9", "#c7b59b", "#b8c7de"]
    hatch_map = {
        "砂岩": "..",
        "泥岩": "--",
        "灰岩": "//",
        "页岩": "xx",
        "砾岩": "OO",
        "花岗岩": "++",
    }
    upper = topography.copy()
    previous_dip_component = np.zeros_like(x)
    lower_boundaries: list[Any] = []
    legend_handles: list[Any] = []
    x_center = float((x.min() + x.max()) / 2)

    for index, layer in enumerate(profile["地层"]):
        dip = float(layer["dip"])
        direction_text = str(layer.get("dip_direction", "")).upper()
        sign = -1 if direction_text in {"W", "NW", "SW", "左"} else 1
        vertical_thickness = float(layer["thickness"]) / max(math.cos(math.radians(dip)), 0.1)
        dip_component = sign * math.tan(math.radians(dip)) * (x - x_center)
        dip_component -= float(np.mean(dip_component))
        # 相邻地层倾角相同时保持界线平行，避免逐层重复叠加倾角造成界线交叉。
        lower = upper - vertical_thickness + (dip_component - previous_dip_component)
        color = str(layer.get("color") or default_colors[index % len(default_colors)])
        pattern = str(layer.get("pattern") or "")
        hatch = hatch_map.get(pattern, pattern if len(pattern) <= 4 else "")
        ax.fill_between(
            x,
            upper,
            lower,
            facecolor=color,
            alpha=0.82,
            edgecolor="#555555",
            linewidth=0.7,
            hatch=hatch,
            zorder=3,
        )
        ax.plot(x, lower, color="#444444", linewidth=0.9, zorder=5)
        legend_handles.append(Patch(facecolor=color, edgecolor="#555555", hatch=hatch, label=layer["name"]))
        lower_boundaries.append(lower)
        upper = lower
        previous_dip_component = dip_component

    profile_bottom = float(np.nanmin(upper))
    for fault in profile.get("断层", []):
        position = float(fault["位置"])
        surface = float(np.interp(position, x, topography))
        dip = float(fault["dip"])
        sign = -1 if str(fault.get("dip_direction", "")).upper() in {"W", "NW", "SW", "左"} else 1
        depth = max(surface - profile_bottom, 1)
        horizontal_shift = sign * depth / max(math.tan(math.radians(dip)), 0.05)
        ax.plot(
            [position, position + horizontal_shift],
            [surface, profile_bottom],
            color="#c62828",
            linewidth=2.0,
            linestyle="--",
            zorder=12,
        )
        label = str(fault.get("name") or "断层")
        nature = str(fault.get("性质") or "无法确定")
        ax.annotate(f"{label}（{nature}）", (position, surface), xytext=(5, 8), textcoords="offset points", fontsize=8, color="#a31515")

    for intrusion in profile.get("侵入体", []):
        polygon_points = intrusion.get("polygon", [])
        if len(polygon_points) < 3:
            continue
        vertices = [(float(point["距离"]), float(point["高程"])) for point in polygon_points]
        patch = Polygon(
            vertices,
            closed=True,
            facecolor=str(intrusion.get("color") or "#e7a6a1"),
            edgecolor="#7f1d1d",
            hatch=str(intrusion.get("pattern") or "++"),
            alpha=0.9,
            zorder=8,
        )
        ax.add_patch(patch)
        legend_handles.append(Patch(facecolor=patch.get_facecolor(), edgecolor="#7f1d1d", hatch=patch.get_hatch(), label=str(intrusion.get("name") or "侵入体")))

    for attitude in profile.get("产状符号", []):
        try:
            position = float(attitude["位置"])
            dip = float(attitude["dip"])
        except (KeyError, TypeError, ValueError):
            continue
        elevation = float(np.interp(position, x, topography))
        length = max((x.max() - x.min()) * 0.035, 5)
        dx = length * math.cos(math.radians(dip))
        dy = length * math.sin(math.radians(dip))
        ax.plot([position - dx / 2, position + dx / 2], [elevation + dy / 2, elevation - dy / 2], color="#111111", linewidth=1.8, zorder=13)
        ax.annotate(f"{dip:g}°", (position, elevation), xytext=(5, 4), textcoords="offset points", fontsize=8)

    total_distance = float(x.max() - x.min())
    scale_length = _nice_scale_length(total_distance)
    scale_y = profile_bottom - max(float(np.ptp(topography)) * 0.08, 5)
    scale_x = float(x.min() + total_distance * 0.05)
    ax.plot([scale_x, scale_x + scale_length], [scale_y, scale_y], color="black", linewidth=3)
    ax.plot([scale_x, scale_x], [scale_y - 1, scale_y + 1], color="black", linewidth=1.5)
    ax.plot([scale_x + scale_length, scale_x + scale_length], [scale_y - 1, scale_y + 1], color="black", linewidth=1.5)
    ax.text(scale_x + scale_length / 2, scale_y - 2, f"{scale_length:g} m", ha="center", va="top", fontsize=9)

    direction = str(profile.get("剖面方向", ""))
    direction_parts = [part.strip() for part in re.split(r"[-—→至]", direction) if part.strip()]
    left_direction = direction_parts[0] if direction_parts else "起点"
    right_direction = direction_parts[-1] if len(direction_parts) > 1 else "终点"
    ax.text(x.min(), topography.max() + 4, left_direction, ha="left", va="bottom", fontweight="bold")
    ax.text(x.max(), topography.max() + 4, right_direction, ha="right", va="bottom", fontweight="bold")

    ax.set_title(f"{direction} 岩性—构造地质剖面图（{profile.get('比例尺', '1:1000')}）", fontsize=15, pad=18)
    ax.set_xlabel("沿剖面距离 / m")
    ax.set_ylabel("高程 / m")
    ax.grid(axis="y", color="#dddddd", linewidth=0.6, linestyle=":", zorder=0)
    ax.set_xlim(x.min(), x.max())
    ax.set_ylim(scale_y - max(float(np.ptp(topography)) * 0.08, 5), topography.max() + max(float(np.ptp(topography)) * 0.2, 12))
    if profile.get("等比例显示", True):
        ax.set_aspect("equal", adjustable="box")
    if legend_handles:
        ax.legend(handles=legend_handles, title="图例", loc="upper left", bbox_to_anchor=(1.01, 1), borderaxespad=0)

    try:
        fig.savefig(png_path, dpi=220, bbox_inches="tight", facecolor="white")
        fig.savefig(svg_path, format="svg", bbox_inches="tight", facecolor="white")
    finally:
        plt.close(fig)

    return {"PNG": str(png_path), "SVG": str(svg_path)}
