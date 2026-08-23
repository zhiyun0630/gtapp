"""V4.0 项目级数据管理。

本模块只负责项目、观察点和照片文件的组织，不包含任何模型推理逻辑。
所有保存操作均采用“临时文件 + 原子替换”，降低比赛现场异常退出导致 JSON 损坏的风险。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_FILE_NAME = "project.json"
DATABASE_FILE_NAME = "geology_database.json"
_INVALID_WINDOWS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_OBSERVATION_ID = re.compile(r"^G\d{3,}$")


class ProjectManagerError(RuntimeError):
    """项目管理过程中可展示给用户的异常。"""


def _now_iso() -> str:
    """返回不带时区歧义的本地 ISO 时间字符串。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _safe_component(value: str, fallback: str = "未命名项目") -> str:
    """清理 Windows 文件名中的非法字符并阻止路径穿越。"""
    cleaned = _INVALID_WINDOWS_CHARS.sub("_", str(value or "").strip())
    cleaned = cleaned.rstrip(" .")
    if cleaned in {"", ".", ".."}:
        cleaned = fallback
    return cleaned[:80]


def _atomic_write_json(path: Path, data: Any) -> None:
    """以 UTF-8 原子写入 JSON，并保留上一版备份。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = path.with_suffix(path.suffix + ".bak")
    if path.exists():
        try:
            backup_path.write_bytes(path.read_bytes())
        except OSError:
            # 备份失败不应覆盖真正的保存异常，但后续原子写入仍需继续尝试。
            pass

    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.stem}_",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _empty_spatial_info() -> dict[str, Any]:
    """返回统一的观察点空间信息结构。"""
    return {
        "观察点顺序": None,
        "下一观察点编号": "",
        "空间关系描述": "",
        "坐标": "",
        "坡度": None,
        "坡向": "",
        "距离下一观察点": None,
        "距离来源": "",
        "距离计算方式": "",
        "模型坐标X": None,
        "模型坐标Y": None,
        "模型坐标Z": None,
        "模型坐标单位": "",
        "模型坐标说明": "",
        "DasViewer测量距离": None,
        "DasViewer测量方位": "",
        "空间关系说明": "",
    }


def _empty_observation(point_id: str) -> dict[str, Any]:
    """创建一个符合 V4.0 数据约定的观察点。"""
    return {
        "编号": point_id,
        "照片": [],
        "照片元数据": [],
        "空间信息": _empty_spatial_info(),
        "三维辅助信息": {},
        "AI分析结果": {},
        "人工修正": "",
        "审核状态": "未审核",
        "更新时间": _now_iso(),
    }


class ProjectManager:
    """管理多个独立地质调查项目及其观察点。"""

    def __init__(self, projects_root: str | Path = "projects") -> None:
        self.projects_root = Path(projects_root).resolve()
        self.projects_root.mkdir(parents=True, exist_ok=True)

    def _project_dir(self, project_key: str) -> Path:
        """解析并校验项目目录，防止访问 projects 目录以外的位置。"""
        if not project_key or _safe_component(project_key) != project_key:
            raise ProjectManagerError("项目标识不合法。")
        path = (self.projects_root / project_key).resolve()
        if path.parent != self.projects_root:
            raise ProjectManagerError("项目目录越界。")
        return path

    def project_dir(self, project_key: str) -> Path:
        """返回已存在项目的绝对目录。"""
        path = self._project_dir(project_key)
        if not (path / PROJECT_FILE_NAME).exists():
            raise ProjectManagerError(f"项目不存在：{project_key}")
        return path

    def list_projects(self) -> list[dict[str, Any]]:
        """列出所有可正常读取的项目，损坏项目不会阻塞界面。"""
        projects: list[dict[str, Any]] = []
        for project_file in sorted(self.projects_root.glob(f"*/{PROJECT_FILE_NAME}")):
            try:
                data = json.loads(project_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            projects.append(
                {
                    "项目标识": project_file.parent.name,
                    "项目名称": data.get("项目名称", project_file.parent.name),
                    "路线": data.get("路线", ""),
                    "观察点数量": len(data.get("观察点", [])),
                    "更新时间": data.get("更新时间", ""),
                }
            )
        return projects

    def create_project(self, project_name: str, route: str = "") -> tuple[str, dict[str, Any]]:
        """创建项目目录、项目 JSON、数据库 JSON 和成果子目录。"""
        project_name = str(project_name or "").strip()
        if not project_name:
            raise ProjectManagerError("项目名称不能为空。")

        base_key = _safe_component(project_name)
        project_key = base_key
        suffix = 2
        while self._project_dir(project_key).exists():
            project_key = f"{base_key}_{suffix}"
            suffix += 1

        project_dir = self._project_dir(project_key)
        for child in ("images", "auxiliary", "exports"):
            (project_dir / child).mkdir(parents=True, exist_ok=True)

        now = _now_iso()
        project = {
            "数据版本": "4.0",
            "项目名称": project_name,
            "路线": str(route or "").strip(),
            "创建时间": now,
            "更新时间": now,
            "观察点": [],
            "区域综合分析": {},
            "剖面成果": {},
        }
        _atomic_write_json(project_dir / PROJECT_FILE_NAME, project)
        _atomic_write_json(project_dir / DATABASE_FILE_NAME, {})
        return project_key, deepcopy(project)

    def load_project(self, project_key: str) -> dict[str, Any]:
        """读取项目；主文件损坏时尝试读取上一版备份。"""
        project_file = self.project_dir(project_key) / PROJECT_FILE_NAME
        candidates = (project_file, project_file.with_suffix(project_file.suffix + ".bak"))
        last_error: Exception | None = None
        for candidate in candidates:
            if not candidate.exists():
                continue
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or not isinstance(data.get("观察点", []), list):
                    raise ValueError("project.json 结构不正确")
                return data
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                last_error = exc
        raise ProjectManagerError(f"项目文件无法读取：{last_error or '文件不存在'}")

    def save_project(self, project_key: str, project: dict[str, Any]) -> None:
        """保存完整项目对象。"""
        if not isinstance(project, dict) or not isinstance(project.get("观察点", []), list):
            raise ProjectManagerError("项目数据结构不正确。")
        project["更新时间"] = _now_iso()
        _atomic_write_json(self.project_dir(project_key) / PROJECT_FILE_NAME, project)

    def add_observation(self, project_key: str, point_id: str | None = None) -> dict[str, Any]:
        """新增观察点；未指定编号时自动使用下一个 Gxxx 编号。"""
        project = self.load_project(project_key)
        existing = {item.get("编号") for item in project.get("观察点", [])}
        if point_id is None:
            number = 1
            while f"G{number:03d}" in existing:
                number += 1
            point_id = f"G{number:03d}"
        point_id = str(point_id).strip().upper()
        if not _OBSERVATION_ID.fullmatch(point_id):
            raise ProjectManagerError("观察点编号必须采用 G001 形式。")
        if point_id in existing:
            raise ProjectManagerError(f"观察点已存在：{point_id}")

        observation = _empty_observation(point_id)
        project["观察点"].append(observation)
        self.save_project(project_key, project)
        return deepcopy(observation)

    def get_observation(self, project_key: str, point_id: str) -> dict[str, Any]:
        """读取指定观察点。"""
        project = self.load_project(project_key)
        for item in project.get("观察点", []):
            if item.get("编号") == point_id:
                return deepcopy(item)
        raise ProjectManagerError(f"找不到观察点：{point_id}")

    def update_observation(
        self,
        project_key: str,
        point_id: str,
        *,
        spatial_info: dict[str, Any] | None = None,
        auxiliary_3d: dict[str, Any] | None = None,
        ai_result: dict[str, Any] | None = None,
        manual_correction: str | None = None,
        review_status: str | None = None,
    ) -> dict[str, Any]:
        """按需更新观察点，未传入的字段保持不变。"""
        project = self.load_project(project_key)
        for item in project.get("观察点", []):
            if item.get("编号") != point_id:
                continue
            if spatial_info is not None:
                merged = _empty_spatial_info()
                merged.update(item.get("空间信息") or {})
                merged.update(spatial_info)
                item["空间信息"] = merged
            if auxiliary_3d is not None:
                item["三维辅助信息"] = deepcopy(auxiliary_3d)
            if ai_result is not None:
                item["AI分析结果"] = deepcopy(ai_result)
            if manual_correction is not None:
                item["人工修正"] = str(manual_correction)
            if review_status is not None:
                item["审核状态"] = str(review_status)
            item["更新时间"] = _now_iso()
            self.save_project(project_key, project)
            return deepcopy(item)
        raise ProjectManagerError(f"找不到观察点：{point_id}")

    def add_photo(
        self,
        project_key: str,
        point_id: str,
        original_name: str,
        image_bytes: bytes,
    ) -> dict[str, Any]:
        """保存观察点照片并返回稳定照片编号；相同内容不会重复写入。"""
        if not image_bytes:
            raise ProjectManagerError("照片内容为空。")
        project = self.load_project(project_key)
        observation = next(
            (item for item in project.get("观察点", []) if item.get("编号") == point_id),
            None,
        )
        if observation is None:
            raise ProjectManagerError(f"找不到观察点：{point_id}")

        digest = hashlib.sha256(image_bytes).hexdigest()
        metadata = observation.setdefault("照片元数据", [])
        for record in metadata:
            if record.get("SHA256") == digest:
                return deepcopy(record)

        for other_observation in project.get("观察点", []):
            if other_observation.get("编号") == point_id:
                continue
            for record in other_observation.get("照片元数据", []):
                if record.get("SHA256") == digest:
                    other_point = other_observation.get("编号", "未知观察点")
                    raise ProjectManagerError(
                        f"该照片已绑定到观察点 {other_point}，不能重复绑定到当前观察点。"
                    )

        photo_id = f"{point_id}-P{len(metadata) + 1:02d}"
        clean_name = _safe_component(Path(original_name).name, f"{photo_id}.jpg")
        point_dir = self.project_dir(project_key) / "images" / point_id
        point_dir.mkdir(parents=True, exist_ok=True)
        output_path = point_dir / clean_name
        if output_path.exists():
            output_path = point_dir / f"{output_path.stem}_{digest[:8]}{output_path.suffix}"
        output_path.write_bytes(image_bytes)
        relative_path = output_path.relative_to(self.project_dir(project_key)).as_posix()

        record = {
            "照片编号": photo_id,
            "原始文件名": str(original_name),
            "相对路径": relative_path,
            "SHA256": digest,
            "上传时间": _now_iso(),
        }
        metadata.append(record)
        observation.setdefault("照片", []).append(relative_path)
        observation["更新时间"] = _now_iso()
        self.save_project(project_key, project)
        return deepcopy(record)

    def add_photos(
        self,
        project_key: str,
        point_id: str,
        photos: list[tuple[str, bytes]],
    ) -> list[dict[str, Any]]:
        """一次性保存照片；先完成跨观察点冲突预检，避免批次部分写入。"""
        digests = [hashlib.sha256(image_bytes).hexdigest() for _, image_bytes in photos]
        if len(digests) != len(set(digests)):
            raise ProjectManagerError("本次上传中包含重复照片。")
        project = self.load_project(project_key)
        for observation in project.get("观察点", []):
            if observation.get("编号") == point_id:
                continue
            existing_digests = {
                record.get("SHA256")
                for record in observation.get("照片元数据", [])
            }
            conflict = next((digest for digest in digests if digest in existing_digests), None)
            if conflict:
                raise ProjectManagerError(
                    f"该照片已绑定到观察点 {observation.get('编号', '未知观察点')}，不能重复绑定到当前观察点。"
                )
        return [
            self.add_photo(project_key, point_id, original_name, image_bytes)
            for original_name, image_bytes in photos
        ]

    def set_regional_analysis(self, project_key: str, result: dict[str, Any]) -> None:
        """保存项目级区域综合分析结果。"""
        project = self.load_project(project_key)
        project["区域综合分析"] = deepcopy(result)
        self.save_project(project_key, project)

    def set_profile_result(self, project_key: str, result: dict[str, Any]) -> None:
        """保存剖面参数及输出文件引用。"""
        project = self.load_project(project_key)
        project["剖面成果"] = deepcopy(result)
        self.save_project(project_key, project)

