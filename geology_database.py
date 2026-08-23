"""V4.0 地质现象数据库。

数据库以观察点编号为主键，保存空间信息、照片证据、视觉观察、综合解释和人工修正。
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any


_POINT_ID = re.compile(r"^G\d{3,}$")


class GeologyDatabaseError(RuntimeError):
    """地质数据库读写或校验异常。"""


def _now_iso() -> str:
    """返回当前本地时间。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    """原子保存数据库 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_suffix(path.suffix + ".bak")
    if path.exists():
        try:
            backup.write_bytes(path.read_bytes())
        except OSError:
            pass
    fd, temporary = tempfile.mkstemp(prefix=".geology_", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _empty_record(point_id: str) -> dict[str, Any]:
    """生成空的观察点数据库记录。"""
    return {
        "观察点编号": point_id,
        "空间信息": {},
        "空间拓扑": {},
        "照片证据": [],
        "视觉观察": {},
        "综合解释": {},
        "人工修正": "",
        "审核状态": "未审核",
        "更新时间": _now_iso(),
    }


class GeologyDatabase:
    """以 JSON 文件实现的轻量地质现象数据库。"""

    def __init__(self, database_path: str | Path) -> None:
        self.path = Path(database_path).resolve()
        if not self.path.exists():
            _atomic_write(self.path, {})

    def load(self) -> dict[str, Any]:
        """读取数据库；主文件损坏时尝试使用备份。"""
        candidates = (self.path, self.path.with_suffix(self.path.suffix + ".bak"))
        last_error: Exception | None = None
        for candidate in candidates:
            if not candidate.exists():
                continue
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("数据库根节点必须是对象")
                return data
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                last_error = exc
        raise GeologyDatabaseError(f"地质数据库无法读取：{last_error or '文件不存在'}")

    def save(self, data: dict[str, Any]) -> None:
        """保存并检查所有主键均为观察点编号。"""
        if not isinstance(data, dict):
            raise GeologyDatabaseError("数据库必须是 JSON 对象。")
        invalid_keys = [
            key for key in data
            if key != "observation_topology" and not _POINT_ID.fullmatch(str(key))
        ]
        if invalid_keys:
            raise GeologyDatabaseError(f"存在非法观察点编号：{invalid_keys}")
        _atomic_write(self.path, data)

    def ensure_observation(
        self,
        point_id: str,
        spatial_info: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """确保观察点记录存在，并可同步空间信息。"""
        point_id = str(point_id).strip().upper()
        if not _POINT_ID.fullmatch(point_id):
            raise GeologyDatabaseError("观察点编号必须采用 G001 形式。")
        data = self.load()
        record = data.setdefault(point_id, _empty_record(point_id))
        if spatial_info is not None:
            record["空间信息"] = deepcopy(spatial_info)
            record["空间拓扑"] = {
                "观察点顺序": spatial_info.get("观察点顺序"),
                "下一观察点编号": spatial_info.get("下一观察点编号", ""),
                "点间距离（m）": spatial_info.get(
                    "DasViewer测量距离",
                    spatial_info.get("距离下一观察点"),
                ),
                "方位（°）": spatial_info.get("DasViewer测量方位", ""),
                "高程": spatial_info.get(
                    "模型坐标Z",
                    spatial_info.get("高程"),
                ),
                "高程差": spatial_info.get("高程差"),
                "空间关系描述": spatial_info.get(
                    "空间关系描述",
                    spatial_info.get("空间关系说明", ""),
                ),
                "数据来源": "地质人员人工输入或DasViewer测量",
            }
        record["更新时间"] = _now_iso()
        self.save(data)
        return deepcopy(record)

    def get_observation(self, point_id: str) -> dict[str, Any]:
        """返回指定观察点的完整记录。"""
        data = self.load()
        if point_id not in data:
            raise GeologyDatabaseError(f"数据库中不存在观察点：{point_id}")
        return deepcopy(data[point_id])

    def list_observations(self) -> list[dict[str, Any]]:
        """按观察点编号列出记录。"""
        data = self.load()
        return [deepcopy(data[key]) for key in sorted(data)]

    def update_photo_evidence(self, point_id: str, photos: list[dict[str, Any]]) -> None:
        """替换观察点的照片证据清单。"""
        data = self.load()
        record = data.setdefault(point_id, _empty_record(point_id))
        record["照片证据"] = deepcopy(photos)
        record["更新时间"] = _now_iso()
        self.save(data)

    def update_visual_observation(self, point_id: str, result: dict[str, Any]) -> None:
        """保存视觉模型和文本综合模型的完整结果。"""
        if not isinstance(result, dict):
            raise GeologyDatabaseError("视觉观察结果必须是 JSON 对象。")
        data = self.load()
        record = data.setdefault(point_id, _empty_record(point_id))
        record["视觉观察"] = deepcopy(result)
        record["更新时间"] = _now_iso()
        self.save(data)

    def update_comprehensive_interpretation(
        self,
        point_id: str,
        interpretation: dict[str, Any],
    ) -> None:
        """保存观察点级综合解释。"""
        data = self.load()
        record = data.setdefault(point_id, _empty_record(point_id))
        record["综合解释"] = deepcopy(interpretation)
        record["更新时间"] = _now_iso()
        self.save(data)

    def update_manual_correction(
        self,
        point_id: str,
        correction: str,
        review_status: str = "已修正",
    ) -> None:
        """保存专家修正，并明确审核状态。"""
        data = self.load()
        record = data.setdefault(point_id, _empty_record(point_id))
        record["人工修正"] = str(correction or "")
        record["审核状态"] = str(review_status)
        record["更新时间"] = _now_iso()
        self.save(data)

    def rebuild_observation_topology(self) -> dict[str, Any]:
        """根据人工顺序和相邻观察点 XYZ 坐标重建 observation_topology 表。"""
        data = self.load()
        observation_items = [
            (key, value)
            for key, value in data.items()
            if key != "observation_topology" and isinstance(value, dict)
        ]
        ordered = sorted(
            observation_items,
            key=lambda item: (
                item[1].get("空间信息", {}).get("观察点顺序") is None,
                item[1].get("空间信息", {}).get("观察点顺序")
                if isinstance(item[1].get("空间信息", {}).get("观察点顺序"), (int, float))
                else 10**9,
            ),
        )
        topology: dict[str, Any] = {}
        for index, (point_id, record) in enumerate(ordered):
            spatial = record.get("空间信息", {})
            next_id = spatial.get("下一观察点编号", "")
            if not next_id and index + 1 < len(ordered):
                next_id = ordered[index + 1][0]
            next_spatial = data.get(next_id, {}).get("空间信息", {}) if next_id else {}
            xyz = [spatial.get("模型坐标X"), spatial.get("模型坐标Y"), spatial.get("模型坐标Z")]
            next_xyz = [next_spatial.get("模型坐标X"), next_spatial.get("模型坐标Y"), next_spatial.get("模型坐标Z")]
            distance = None
            azimuth = None
            elevation_difference = None
            if all(isinstance(value, (int, float)) for value in xyz + next_xyz):
                dx = next_xyz[0] - xyz[0]
                dy = next_xyz[1] - xyz[1]
                dz = next_xyz[2] - xyz[2]
                distance = math.sqrt(dx * dx + dy * dy + dz * dz)
                azimuth = (math.degrees(math.atan2(dx, dy)) + 360) % 360
                elevation_difference = dz
            manual_distance = spatial.get(
                "DasViewer测量距离",
                spatial.get("距离下一观察点"),
            )
            manual_azimuth = spatial.get("DasViewer测量方位", "")
            manual_elevation_difference = spatial.get("高程差")
            if distance is None:
                distance = manual_distance
            if azimuth is None:
                azimuth = manual_azimuth
            if elevation_difference is None:
                elevation_difference = manual_elevation_difference

            topology[point_id] = {
                "观察点编号": point_id,
                "观察点顺序": spatial.get("观察点顺序"),
                "下一观察点编号": next_id,
                "起点XYZ": xyz,
                "终点XYZ": next_xyz if next_id else [None, None, None],
                "点间距离（m）": distance,
                "方位（°）": azimuth,
                "高程": spatial.get("模型坐标Z", spatial.get("高程")),
                "高程差": elevation_difference,
                "空间关系描述": spatial.get("空间关系描述", spatial.get("空间关系说明", "")),
                "数据来源": (
                    "根据人工输入XYZ坐标自动计算"
                    if all(isinstance(value, (int, float)) for value in xyz + next_xyz)
                    else "地质人员人工输入或DasViewer测量"
                ),
            }
        data["observation_topology"] = topology
        for point_id, topology_record in topology.items():
            if point_id in data and isinstance(data[point_id], dict):
                data[point_id]["空间拓扑"] = deepcopy(topology_record)
                data[point_id]["更新时间"] = _now_iso()
        self.save(data)
        return deepcopy(topology)

    def build_regional_payload(self) -> dict[str, Any]:
        """生成区域分析输入；路线顺序只来自人工填写的观察点顺序。"""
        payload: dict[str, Any] = {}
        records = [(key, value) for key, value in self.load().items() if key != "observation_topology"]
        records.sort(key=lambda item: (
            item[1].get("空间信息", {}).get("观察点顺序") is None,
            item[1].get("空间信息", {}).get("观察点顺序")
            if isinstance(item[1].get("空间信息", {}).get("观察点顺序"), (int, float))
            else 10**9,
        ))
        topology = self.rebuild_observation_topology()
        for point_id, record in records:
            spatial = deepcopy(record.get("空间信息", {}))
            topology_record = deepcopy(topology.get(point_id, {}))
            payload[point_id] = {
                "空间信息": spatial,
                "空间拓扑": topology_record,
                "路线节点": topology_record,
                "照片证据": [
                    {
                        "照片编号": item.get("照片编号"),
                        "原始文件名": item.get("原始文件名"),
                    }
                    for item in record.get("照片证据", [])
                ],
                "视觉观察": deepcopy(record.get("视觉观察", {})),
                "综合解释": deepcopy(record.get("综合解释", {})),
                "人工修正": record.get("人工修正", ""),
                "审核状态": record.get("审核状态", "未审核"),
            }
        return payload

    def validate_evidence_references(self) -> list[str]:
        """检查证据链引用的观察点和照片编号，返回问题清单。"""
        data = self.load()
        valid_points = set(data)
        valid_photos = {
            item.get("照片编号")
            for record in data.values()
            for item in record.get("照片证据", [])
            if item.get("照片编号")
        }
        issues: list[str] = []
        for point_id, record in data.items():
            result = record.get("视觉观察", {})
            for index, evidence in enumerate(result.get("证据链", []) if isinstance(result, dict) else []):
                referenced_points = evidence.get("观察点编号", [point_id])
                if isinstance(referenced_points, str):
                    referenced_points = [referenced_points]
                unknown_points = set(referenced_points or []) - valid_points
                if unknown_points:
                    issues.append(f"{point_id} 证据链第{index + 1}项引用未知观察点：{sorted(unknown_points)}")
                referenced_photos = evidence.get("证据照片编号", [])
                if isinstance(referenced_photos, str):
                    referenced_photos = [referenced_photos]
                unknown_photos = set(referenced_photos or []) - valid_photos
                if unknown_photos:
                    issues.append(f"{point_id} 证据链第{index + 1}项引用未知照片：{sorted(unknown_photos)}")
        return issues

