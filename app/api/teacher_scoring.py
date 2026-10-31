"""教师评分工作台 API。"""
from datetime import datetime
from pathlib import Path
from uuid import uuid4
import importlib.util
import inspect
import json
import os
import re
import shutil
import threading
import zipfile
from urllib.parse import quote

from sqlalchemy import func, inspect as sqlalchemy_inspect, or_, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.responses import FileResponse, JSONResponse
from starlette.requests import Request
from starlette.routing import Route

from app.core.config import settings
from app.models.engine import engine, get_db
from app.models.exam.biz_models import BizAnswerFile, BizAnswerSheet, BizExam, BizExamGradingStandard, BizExamGradingStandardItem, BizExamGradingTemplate, BizExamGradingTask, BizExamPackage, BizLocalExamPackage, BizLocalGradingRecord, BizSubject
from app.models.modules.users import User
from app.models.modules.mcp_modules import McpModule
from app.models.modules.mcp_services import McpService
from app.services.mcp_service.service_manager import service_manager
from app.services.file_role_recognition import (
    ANSWER_SUPPORTING_KEYWORDS,
    BLANK_SUPPORTING_KEYWORDS,
    STUDENT_ID_PATTERN as STUDENT_DOCUMENT_ID_PATTERN,
    detect_upload_type as _detect_upload_type,
    is_answer_supporting_document as _is_answer_supporting_document,
    is_blank_supporting_document as _is_blank_supporting_document,
    is_student_answer_document as _is_student_answer_document,
)
from app.services.paper_structure_builder import (
    enrich_reference_answers_with_max_score,
    parse_blank_paper,
)
from app.services.structure_serializer import serialize_package
from app.services.package_structure_recognition import infer_paper_prefix, recognize_package_structure
from app.services.teacher_scoring_document_parser import (
    IMAGE_SUFFIXES as DOCUMENT_IMAGE_SUFFIXES,
    WORD_SUFFIXES as DOCUMENT_WORD_SUFFIXES,
    is_temporary_word_file,
    process_package_documents,
    process_reference_document,
)
from app.utils.logging import mcp_logger
from app.utils.response import error_response, success_response


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".svg"}
STUDENT_ID_KEYS = ("id", "studentId", "student_id", "student_no", "studentNo", "studentIdNo")
UPLOAD_FILE_CHUNK_SIZE = 1024 * 1024
ZIP_MEMBER_BATCH_SIZE = 100
DB_INSERT_BATCH_SIZE = 100
LOCAL_PACKAGE_STAGE_DIR = Path(settings.MCP_BASE_DIR) / "data" / "teacher_scoring" / "local-package-staging"
LOCAL_PACKAGE_DATA_DIR = Path(settings.MCP_BASE_DIR) / "data" / "teacher_scoring" / "local-packages"
# 答案卷/空白卷关键词与学号正则已收敛至 file_role_recognition（第二步改造），
# 经顶部 import 以原名称继续暴露。


def _iter_batches(iterable, batch_size=ZIP_MEMBER_BATCH_SIZE):
    batch = []
    for item in iterable:
        batch.append(item)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


async def _save_upload_file_stream(upload_file, save_path: Path):
    with save_path.open("wb") as target:
        while True:
            chunk = await upload_file.read(UPLOAD_FILE_CHUNK_SIZE)
            if not chunk:
                break
            target.write(chunk)


def _copy_stream_in_chunks(source, target, chunk_size=UPLOAD_FILE_CHUNK_SIZE):
    while True:
        chunk = source.read(chunk_size)
        if not chunk:
            break
        target.write(chunk)


def _success_response_with_data(data=None, message="操作成功", code=0, http_status_code=200):
    return JSONResponse(
        content={"code": code, "message": message, "data": data},
        status_code=http_status_code,
    )

def _current_teacher(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        return None, error_response("未登录", code=401, http_status_code=401)
    # 管理员与普通用户共用评分工作台能力；业务记录仍按 user_id 隔离，
    # 所以管理员访问时只会读写管理员账号自己创建的科目、考试和试卷包。
    return user, None


def _subject_to_dict(subject: BizSubject):
    return {
        "subject_id": subject.subject_id,
        "subject_no": subject.subject_no,
        "subject_name": subject.subject_name,
        "creator_id": subject.creator_id,
        "created_at": (
            subject.created_at.strftime("%Y-%m-%d %H:%M:%S")
            if subject.created_at else None
        ),
        "remark": subject.remark,
    }


def _exam_to_dict(exam: BizExam):
    return {
        "id": exam.id,
        "subject_id": exam.subject_id,
        "exam_name": exam.exam_name,
        "exam_time": (
            exam.exam_time.strftime("%Y-%m-%d %H:%M:%S")
            if exam.exam_time else None
        ),
        "answer_key_path": exam.answer_key_path,
        "blank_paper_path": exam.blank_paper_path,
        "creator_id": exam.creator_id,
    }



def _ensure_list_payload(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _parent_question_no(question_no):
    normalized = str(question_no or "").strip()
    if "." not in normalized:
        return normalized
    return normalized.split(".", 1)[0].strip()


def _grading_standard_to_dict(standard: BizExamGradingStandard):
    standard_items = getattr(standard, "_resolved_standard_items", None)
    if standard_items is None:
        standard_items = standard.standard_items
    standard_items = _ensure_list_payload(standard_items)
    return {
        "standard_id": standard.standard_id,
        "subject_id": standard.subject_id,
        "exam_id": standard.exam_id,
        "creator_id": standard.creator_id,
        "version_no": standard.version_no,
        "standard_items": standard_items,
        "remark": standard.remark,
        "created_at": (
            standard.created_at.strftime("%Y-%m-%d %H:%M:%S")
            if standard.created_at else None
        ),
        "updated_at": (
            standard.updated_at.strftime("%Y-%m-%d %H:%M:%S")
            if standard.updated_at else None
        ),
    }

def _ensure_grading_standard_item_table():
    try:
        BizExamGradingStandardItem.__table__.create(bind=engine, checkfirst=True)
        # 已有表补齐题目级备注列
        from sqlalchemy import inspect, text
        with engine.begin() as conn:
            columns = {column["name"] for column in inspect(conn).get_columns("biz_exam_grading_standard_item")}
            if "remark" not in columns:
                conn.execute(text("ALTER TABLE biz_exam_grading_standard_item ADD COLUMN remark VARCHAR(255) NULL"))
    except Exception as exc:
        mcp_logger.error(f"ensure grading standard item table failed: {str(exc)}")
        raise

def _grading_standard_item_to_dict(item: BizExamGradingStandardItem):
    return {
        "item_id": item.item_id,
        "question_no": item.question_no,
        "question_type": item.question_type,
        "mcp_service_id": item.mcp_service_id,
        "mcp_service_name": item.mcp_service_name or "",
        "question_text": item.question_text or "",
        "reference_answer": item.reference_answer or "",
        "grading_criteria": item.grading_criteria or "",
        "question_version_no": item.question_version_no,
        "source_standard_version_no": item.source_standard_version_no,
        "mcp_params": item.mcp_params or {},
        "max_score": float(item.max_score) if item.max_score is not None else None,
        "remark": item.remark or "",
    }


def _normalize_standard_item_payload(item, index=1):
    if not isinstance(item, dict):
        raise ValueError(f"grading standard item {index} format invalid")
    question_no = str(item.get("question_no") or "").strip()
    question_type = str(item.get("question_type") or "").strip()
    mcp_service_id = item.get("mcp_service_id")
    if not question_no:
        raise ValueError(f"grading standard item {index} missing question_no")
    if not question_type:
        raise ValueError(f"grading standard item {index} missing question_type")
    if not mcp_service_id:
        raise ValueError(f"grading standard item {index} missing mcp_service_id")
    question_version_no = _to_int_or_none(item.get("question_version_no")) or 1
    if question_version_no < 1:
        question_version_no = 1
    return {
        "item_id": _to_int_or_none(item.get("item_id")),
        "question_no": question_no,
        "question_type": question_type,
        "mcp_service_id": str(mcp_service_id),
        "mcp_service_name": str(item.get("mcp_service_name") or "").strip(),
        "question_text": str(item.get("question_text") or "").strip(),
        "reference_answer": str(item.get("reference_answer") or "").strip(),
        "grading_criteria": str(item.get("grading_criteria") or "").strip(),
        "question_version_no": question_version_no,
        "source_standard_version_no": _to_int_or_none(item.get("source_standard_version_no")),
        "mcp_params": item.get("mcp_params") if isinstance(item.get("mcp_params"), dict) else {},
        "max_score": _to_decimal_or_none(item.get("max_score")),
        "remark": str(item.get("remark") or "").strip(),
    }

def _standard_item_matches_payload(row: BizExamGradingStandardItem, item):
    return (
        str(row.question_no or "") == str(item.get("question_no") or "")
        and str(row.question_type or "") == str(item.get("question_type") or "")
        and str(row.mcp_service_id or "") == str(item.get("mcp_service_id") or "")
        and str(row.mcp_service_name or "") == str(item.get("mcp_service_name") or "")
        and str(row.question_text or "") == str(item.get("question_text") or "")
        and str(row.reference_answer or "") == str(item.get("reference_answer") or "")
        and str(row.grading_criteria or "") == str(item.get("grading_criteria") or "")
        and int(row.question_version_no or 0) == int(item.get("question_version_no") or 0)
        and (row.mcp_params or {}) == (item.get("mcp_params") or {})
    )

def _standard_items_equivalent(existing_items, normalized_items):
    """比对两组标准题目内容是否完全一致（忽略 item_id 与版本来源字段），用于避免生成内容重复的版本。"""
    existing_list = list(existing_items or [])
    payload_list = list(normalized_items or [])
    if len(existing_list) != len(payload_list):
        return False

    def build(item):
        return (
            str(item.get("question_type") or ""),
            str(item.get("mcp_service_id") or ""),
            str(item.get("mcp_service_name") or ""),
            str(item.get("question_text") or ""),
            str(item.get("reference_answer") or ""),
            str(item.get("grading_criteria") or ""),
            int(item.get("question_version_no") or 0),
            json.dumps(item.get("mcp_params") or {}, sort_keys=True, ensure_ascii=False, default=str),
            _to_decimal_or_none(item.get("max_score")),
        )

    existing_map = {str(item.get("question_no") or ""): build(item) for item in existing_list}
    payload_map = {str(item.get("question_no") or ""): build(item) for item in payload_list}
    return existing_map == payload_map


def _load_standard_items(db, standards):
    _ensure_grading_standard_item_table()
    standard_list = list(standards or [])
    if not standard_list:
        return {}
    standard_ids = [standard.standard_id for standard in standard_list if standard.standard_id]
    summary_item_ids = set()
    summary_order = {}
    for standard in standard_list:
        for index, summary in enumerate(standard.standard_items or []):
            if not isinstance(summary, dict):
                continue
            item_id = _to_int_or_none(summary.get("item_id"))
            if not item_id:
                continue
            summary_item_ids.add(item_id)
            summary_order.setdefault(standard.standard_id, []).append(item_id)

    filters = []
    if standard_ids:
        filters.append(BizExamGradingStandardItem.standard_id.in_(standard_ids))
    if summary_item_ids:
        filters.append(BizExamGradingStandardItem.item_id.in_(summary_item_ids))
    if not filters:
        return {}

    query = select(BizExamGradingStandardItem)
    query = query.where(or_(*filters)) if len(filters) > 1 else query.where(filters[0])
    rows = db.execute(
        query.order_by(
            BizExamGradingStandardItem.standard_id.asc(),
            BizExamGradingStandardItem.question_no.asc(),
            BizExamGradingStandardItem.question_version_no.asc(),
            BizExamGradingStandardItem.item_id.asc(),
        )
    ).scalars().all()

    rows_by_standard = {}
    rows_by_item_id = {}
    for row in rows:
        item_data = _grading_standard_item_to_dict(row)
        rows_by_standard.setdefault(row.standard_id, []).append(item_data)
        rows_by_item_id[row.item_id] = item_data

    item_map = {}
    for standard in standard_list:
        ordered_ids = summary_order.get(standard.standard_id) or []
        if ordered_ids:
            item_map[standard.standard_id] = [rows_by_item_id[item_id] for item_id in ordered_ids if item_id in rows_by_item_id]
        elif standard.standard_id in rows_by_standard:
            item_map[standard.standard_id] = rows_by_standard[standard.standard_id]
    return item_map

def _migrate_standard_items_if_needed(db, standards):
    standard_list = list(standards or [])
    item_map = _load_standard_items(db, standard_list)
    changed = False
    for standard in standard_list:
        if item_map.get(standard.standard_id):
            standard._resolved_standard_items = item_map[standard.standard_id]
            continue
        legacy_items = standard.standard_items if isinstance(standard.standard_items, list) else []
        migrated_items = []
        for index, item in enumerate(legacy_items, start=1):
            try:
                normalized = _normalize_standard_item_payload(item, index)
            except ValueError:
                continue
            if not normalized.get("source_standard_version_no"):
                normalized["source_standard_version_no"] = standard.version_no
            row = BizExamGradingStandardItem(
                standard_id=standard.standard_id,
                subject_id=standard.subject_id,
                exam_id=standard.exam_id,
                creator_id=standard.creator_id,
                question_no=normalized["question_no"],
                question_type=normalized["question_type"],
                mcp_service_id=normalized["mcp_service_id"],
                mcp_service_name=normalized["mcp_service_name"],
                question_text=normalized["question_text"],
                reference_answer=normalized["reference_answer"],
                grading_criteria=normalized["grading_criteria"],
                question_version_no=normalized["question_version_no"],
                source_standard_version_no=normalized["source_standard_version_no"],
                mcp_params=normalized["mcp_params"],
                max_score=normalized.get("max_score"),
                remark=normalized.get("remark"),
            )
            db.add(row)
            db.flush()
            item_data = _grading_standard_item_to_dict(row)
            migrated_items.append(item_data)
            changed = True
        standard._resolved_standard_items = migrated_items
        item_map[standard.standard_id] = migrated_items
    if changed:
        db.commit()
    return item_map


def _resolve_standards_for_response(db, standards):
    standard_list = list(standards or [])
    _migrate_standard_items_if_needed(db, standard_list)
    return standard_list


def _resolve_all_standard_items_for_versions(db, standards, subject_id, exam_id, user_id):
    standard_list = _resolve_standards_for_response(db, standards)
    if not standard_list:
        return standard_list

    _ensure_grading_standard_item_table()
    standards_by_id = {standard.standard_id: standard for standard in standard_list if standard.standard_id}
    rows = db.execute(
        select(BizExamGradingStandardItem)
        .where(
            BizExamGradingStandardItem.subject_id == subject_id,
            BizExamGradingStandardItem.exam_id == exam_id,
            BizExamGradingStandardItem.creator_id == int(user_id),
        )
        .order_by(
            BizExamGradingStandardItem.standard_id.asc(),
            BizExamGradingStandardItem.question_no.asc(),
            BizExamGradingStandardItem.question_version_no.asc(),
            BizExamGradingStandardItem.item_id.asc(),
        )
    ).scalars().all()

    seen_item_ids = {}
    for standard in standard_list:
        current_items = list(_standard_items_for_standard(standard))
        standard._resolved_standard_items = current_items
        seen_item_ids[standard.standard_id] = {
            _to_int_or_none(item.get("item_id"))
            for item in current_items
            if isinstance(item, dict) and _to_int_or_none(item.get("item_id"))
        }

    for row in rows:
        standard = standards_by_id.get(row.standard_id)
        if not standard:
            continue
        item_id = _to_int_or_none(row.item_id)
        if item_id and item_id in seen_item_ids.setdefault(row.standard_id, set()):
            continue
        standard._resolved_standard_items.append(_grading_standard_item_to_dict(row))
        if item_id:
            seen_item_ids[row.standard_id].add(item_id)

    version_lookup = _build_question_version_lookup(standard_list)
    _apply_question_version_lookup(standard_list, version_lookup)

    return standard_list

def _grading_template_to_dict(template: BizExamGradingTemplate):
    return {
        "template_id": template.template_id,
        "subject_id": template.subject_id,
        "exam_id": template.exam_id,
        "creator_id": template.creator_id,
        "template_name": template.template_name,
        "template_items": _ensure_list_payload(template.template_items),
        "description": template.description,
        "created_at": (
            template.created_at.strftime("%Y-%m-%d %H:%M:%S")
            if template.created_at else None
        ),
        "updated_at": (
            template.updated_at.strftime("%Y-%m-%d %H:%M:%S")
            if template.updated_at else None
        ),
    }

def _package_to_dict(package: BizExamPackage):
    return {
        "package_id": package.package_id,
        "exam_id": package.exam_id,
        "package_name": package.package_name,
        "file_path": package.file_path,
        "extract_path": package.extract_path,
        "uploaded_at": (
            package.uploaded_at.strftime("%Y-%m-%d %H:%M:%S")
            if package.uploaded_at else None
        ),
    }


def _ensure_local_package_schema():
    """为已有数据库补齐本地试卷包持久化字段。"""
    required_columns = {
        "word_document_count": "INTEGER NOT NULL DEFAULT 0",
        "total_word_size": "INTEGER NULL",
        "parsed_students": "JSON NULL",
        "parsed_image_manifest": "JSON NULL",
        "reference_answers": "JSON NULL",
        "blank_paper_json": "JSON NULL",
        "answer_key_json": "JSON NULL",
        "parsed_storage_path": "VARCHAR(500) NULL",
        "file_fingerprint": "VARCHAR(128) NULL",
    }
    try:
        inspector = sqlalchemy_inspect(engine)
        if not inspector.has_table("biz_local_exam_package"):
            BizLocalExamPackage.__table__.create(bind=engine, checkfirst=True)
            return
        existing = {item["name"] for item in inspector.get_columns("biz_local_exam_package")}
        missing = [(name, definition) for name, definition in required_columns.items() if name not in existing]
        if not missing:
            return
        with engine.begin() as connection:
            for name, definition in missing:
                connection.execute(text(
                    f"ALTER TABLE biz_local_exam_package ADD COLUMN {name} {definition}"
                ))
    except Exception as exc:
        mcp_logger.error(f"补齐本地试卷包字段失败: {exc}")
        raise


def _ensure_grading_task_schema():
    """为已有数据库按需创建阅卷任务关联表。"""
    try:
        BizExamGradingTask.__table__.create(bind=engine, checkfirst=True)
    except Exception as exc:
        mcp_logger.error(f"创建阅卷任务表失败: {exc}")
        raise


def _local_package_file_url(package: BizLocalExamPackage, relative_path: str) -> str:
    path_text = str(relative_path or "").replace("\\", "/").lstrip("/")
    return (
        f"/api/v1/mcp/teacher-scoring/subjects/{package.subject_id}/"
        f"exams/{package.exam_id}/local-packages/{package.local_package_id}/files"
        f"?path={quote(path_text, safe='')}"
    )


def _resolve_local_package_storage_root(package: BizLocalExamPackage):
    """Resolve local-package files from the current backend storage root only."""
    stored_path = str(getattr(package, "parsed_storage_path", None) or "").strip()
    if not stored_path:
        return None

    relative_path = Path(stored_path)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        return None

    try:
        base_dir = Path(settings.MCP_BASE_DIR).resolve()
        candidate = (base_dir / relative_path).resolve()
        candidate.relative_to(base_dir)
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_dir() else None


def _enrich_local_package_file_links(package: BizLocalExamPackage, value):
    if isinstance(value, list):
        return [_enrich_local_package_file_links(package, item) for item in value]
    if not isinstance(value, dict):
        return value

    copied = {
        key: _enrich_local_package_file_links(package, item)
        for key, item in value.items()
    }
    storage_path = copied.get("storage_path")
    relative_path = copied.get("relative_path")
    if (storage_path or relative_path) and copied.get("file_name"):
        file_path = storage_path or relative_path
        copied["url"] = _local_package_file_url(package, file_path)
    return copied



def _local_package_to_dict(package: BizLocalExamPackage):
    image_manifest = []
    for item in package.parsed_image_manifest or []:
        if not isinstance(item, dict):
            continue
        copied = dict(item)
        relative_path = copied.get("relative_path") or copied.get("path") or copied.get("name")
        file_path = copied.get("storage_path") or relative_path
        copied["url"] = _local_package_file_url(package, file_path)
        image_manifest.append(copied)
    return {
        "local_package_id": package.local_package_id,
        "package_id": f"local_{package.local_package_id}",
        "subject_id": package.subject_id,
        "exam_id": package.exam_id,
        "creator_id": package.creator_id,
        "package_name": package.package_name,
        "package_fingerprint": package.package_fingerprint,
        "quick_fingerprint": package.quick_fingerprint,
        "file_fingerprint": package.file_fingerprint,
        "folder_name": package.folder_name,
        "json_file_name": package.json_file_name,
        "json_file_size": package.json_file_size,
        "student_count": package.student_count,
        "image_count": package.image_count,
        "word_document_count": package.word_document_count,
        "total_file_size": package.total_file_size,
        "total_image_size": package.total_image_size,
        "total_word_size": package.total_word_size,
        "student_no_hash": package.student_no_hash,
        "structure_summary": package.structure_summary or {},
        "answer_index": package.answer_index or [],
        "file_manifest": package.file_manifest or [],
        "parsed_students": _enrich_local_package_file_links(package, package.parsed_students or []),
        "parsed_image_manifest": image_manifest,
        "reference_answers": package.reference_answers or [],
        "blank_paper_json": package.blank_paper_json,
        "answer_key_json": package.answer_key_json,
        "parsed_storage_path": package.parsed_storage_path,
        "status": package.status,
        "created_at": (
            package.created_at.strftime("%Y-%m-%d %H:%M:%S")
            if package.created_at else None
        ),
        "updated_at": (
            package.updated_at.strftime("%Y-%m-%d %H:%M:%S")
            if package.updated_at else None
        ),
        "isLocal": True,
    }


def _expected_reference_questions_from_package(
    package: BizLocalExamPackage,
) -> list[dict[str, str]]:
    """用已解析学生试卷里的题目结构约束参考答案导入题号。"""
    students = package.parsed_students or []
    if not isinstance(students, list):
        return []

    source_student = None
    for student in students:
        if isinstance(student, dict) and isinstance(student.get("result"), list):
            source_student = student
            break
    if not source_student:
        return []

    result: list[dict[str, str]] = []
    for major_index, major in enumerate(source_student.get("result") or [], 1):
        if not isinstance(major, dict):
            continue
        major_text = str(major.get("question") or "").strip()
        sub_questions = major.get("sub_question")
        if isinstance(sub_questions, list) and sub_questions:
            for sub_index, sub in enumerate(sub_questions, 1):
                if not isinstance(sub, dict):
                    continue
                question_no = f"{major_index}.{sub_index}"
                question_text = str(sub.get("question") or "").strip() or major_text
                result.append(
                    {
                        "question_no": question_no,
                        "question_text": question_text[:800],
                    }
                )
        else:
            result.append(
                {
                    "question_no": f"{major_index}.1",
                    "question_text": major_text[:800],
                }
            )
    return result


def _find_supporting_word_document(root: Path, preferred_name: str | None = None) -> Path | None:
    supporting_dir = root / "supporting"
    if not supporting_dir.is_dir():
        return None

    if preferred_name:
        preferred = (supporting_dir / Path(preferred_name).name).resolve()
        try:
            preferred.relative_to(supporting_dir.resolve())
        except ValueError:
            preferred = None
        if preferred and preferred.is_file() and preferred.suffix.lower() in DOCUMENT_WORD_SUFFIXES:
            return preferred

    candidates = sorted(
        (
            path for path in supporting_dir.iterdir()
            if path.is_file() and path.suffix.lower() in DOCUMENT_WORD_SUFFIXES
        ),
        key=lambda item: item.name.lower(),
    )
    return candidates[0] if candidates else None


def _grading_record_to_dict(record: BizLocalGradingRecord, creator_name: str = ""):
    # 兼容早期试阅记录：当时只保存了 MCP 原始结果，没有把分数和评语
    # 归一化到独立字段。读取时即时补齐，避免教师必须重新试阅。
    fallback_score = (
        _extract_result_score(record.mcp_raw_json)
        if record.score is None
        else None
    )
    fallback_comment = (
        _extract_result_comment(record.mcp_raw_json)
        if not record.grading_desc
        else None
    )
    return {
        "record_id": record.record_id,
        "local_package_id": record.local_package_id,
        "package_id": f"local_{record.local_package_id}",
        "subject_id": record.subject_id,
        "exam_id": record.exam_id,
        "package_fingerprint": record.package_fingerprint,
        "student_no": record.student_no,
        "question_no": record.question_no,
        "question_label": record.question_label,
        "grading_mode": record.grading_mode,
        "mcp_service_id": record.mcp_service_id,
        "mcp_service_name": record.mcp_service_name,
        "template_id": record.template_id,
        "template_name": record.template_name,
        "standard_label": record.standard_label,
        "standard_id": record.standard_id,
        "standard_version_no": record.standard_version_no,
        "question_version_no": record.question_version_no,
        "score": (
            float(record.score)
            if record.score is not None
            else fallback_score
        ),
        "max_score": float(record.max_score) if record.max_score is not None else None,
        "grading_desc": record.grading_desc or fallback_comment,
        "question_text": record.question_text,
        "answer_text": record.answer_text,
        "image_name": record.image_name,
        "mcp_raw_json": record.mcp_raw_json,
        "is_trial": record.is_trial,
        "round_note": record.round_note,
        "round_starred": bool(record.round_starred),
        "creator_name": creator_name,
        "graded_at": (
            record.graded_at.strftime("%Y-%m-%d %H:%M:%S")
            if record.graded_at else None
        ),
        "updated_at": (
            record.updated_at.strftime("%Y-%m-%d %H:%M:%S")
            if record.updated_at else None
        ),
    }
def _answer_sheet_to_dict(answer: BizAnswerSheet):
    answer_json = answer.page_mapping
    if answer_json is None and answer.text_content:
        try:
            answer_json = json.loads(answer.text_content)
        except (TypeError, json.JSONDecodeError):
            answer_json = None

    return {
        "answer_id": answer.answer_id,
        "student_no": answer.student_no,
        "exam_id": answer.exam_id,
        "package_id": answer.package_id,
        "upload_type": answer.upload_type,
        "file_path": answer.file_path,
        "text_content": answer.text_content,
        "answer_json": answer_json,
        "uploaded_at": (
            answer.uploaded_at.strftime("%Y-%m-%d %H:%M:%S")
            if answer.uploaded_at else None
        ),
    }


def _answer_file_to_dict(file: BizAnswerFile):
    return {
        "id": file.id,
        "answer_id": file.answer_id,
        "exam_id": file.exam_id,
        "package_id": file.package_id,
        "student_no": file.student_no,
        "file_name": file.file_name,
        "file_path": file.file_path,
        "file_type": file.file_type,
        "relative_path": file.relative_path,
        "file_size": file.file_size,
        "created_at": (
            file.created_at.strftime("%Y-%m-%d %H:%M:%S")
            if file.created_at else None
        ),
    }


def _get_subject_id(request: Request):
    try:
        return int(request.path_params["subject_id"]), None
    except (KeyError, TypeError, ValueError):
        return None, error_response("科目ID不正确", code=400, http_status_code=400)


def _get_exam_id(request: Request):
    try:
        return int(request.path_params["exam_id"]), None
    except (KeyError, TypeError, ValueError):
        return None, error_response("考试ID不正确", code=400, http_status_code=400)


def _get_package_id(request: Request):
    try:
        return int(request.path_params["package_id"]), None
    except (KeyError, TypeError, ValueError):
        return None, error_response("试卷包ID不正确", code=400, http_status_code=400)


def _get_answer_id(request: Request):
    try:
        return int(request.path_params["answer_id"]), None
    except (KeyError, TypeError, ValueError):
        return None, error_response("答卷ID不正确", code=400, http_status_code=400)


def _parse_subject_payload(data):
    subject_no = (data.get("subject_no") or "").strip()
    subject_name = (data.get("subject_name") or "").strip()
    created_at = (data.get("created_at") or "").strip()
    remark = (data.get("remark") or "").strip() or None

    if not subject_no:
        return None, error_response("科目号不能为空")
    if not subject_name:
        return None, error_response("科目名不能为空")
    if not created_at:
        return None, error_response("创建日期不能为空")

    try:
        created_date = datetime.strptime(created_at, "%Y-%m-%d")
    except ValueError:
        return None, error_response("创建日期格式不正确")

    return {
        "subject_no": subject_no,
        "subject_name": subject_name,
        "created_at": created_date,
        "remark": remark,
    }, None


def _parse_exam_time(value):
    value = (value or "").strip()
    if not value:
        return None, None

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt), None
        except ValueError:
            continue

    return None, error_response("考试时间格式不正确")


def _safe_upload_name(filename):
    filename = Path(filename or "file").name
    return filename.replace("/", "_").replace("\\", "_")


def _parse_json_dict(value):
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}


def _resolve_grading_service(db, service_uuid):
    service = None
    if service_uuid:
        service = db.execute(
            select(McpService).where(McpService.service_uuid == str(service_uuid))
        ).scalar_one_or_none()

    if not service:
        try:
            service_id = int(service_uuid)
        except (TypeError, ValueError):
            service_id = None
        if service_id:
            service = db.execute(
                select(McpService).where(McpService.id == service_id)
            ).scalar_one_or_none()

    if service:
        module = db.execute(
            select(McpModule).where(McpModule.id == service.module_id)
        ).scalar_one_or_none()
        return service, module

    if isinstance(service_uuid, str) and service_uuid.startswith("module:"):
        module_id = service_uuid.split(":", 1)[1]
    else:
        module_id = service_uuid

    try:
        module_id = int(module_id)
    except (TypeError, ValueError):
        return None, None

    module = db.execute(
        select(McpModule).where(McpModule.id == module_id)
    ).scalar_one_or_none()
    return None, module


def _normalize_max_score(value):
    if value in (None, ""):
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if score < 0:
        return None
    return int(score) if score.is_integer() else score


def _clamp_score_result(result, max_score, multi_service=False):
    if max_score is None or not isinstance(result, dict):
        return result

    score_data = result.get("score_data")
    target = score_data if isinstance(score_data, dict) else result
    if not isinstance(target, dict):
        return result

    for field in ("max_score", "full_score", "total_possible_score", "total_full_score"):
        target[field] = max_score
    result["max_score"] = max_score

    score_field = None
    if "total_score" in target:
        score_field = "total_score"
    elif "score" in target:
        score_field = "score"
    if score_field is None:
        return result

    try:
        score = float(target.get(score_field))
    except (TypeError, ValueError):
        return result

    # 多服务分工评阅时各服务按评分标准中自己部分的满分评分，
    # 不按题目总满分截断，由前端汇总求和。
    if multi_service:
        return result

    if score > max_score:
        target[score_field] = max_score
        target["score_adjusted"] = True
        target["score_adjust_reason"] = "模型返回分数超过本题满分，系统已按满分截断"

    return result


def _normalize_result_question_no(value):
    text = str(value or "").strip()
    if text.upper().startswith("Q"):
        text = text[1:].strip()
    return text


# 部分大题级评分模块（如“数据库第四大题评分”）按“得分点名称”返回各小题分数，
# 这里维护得分点名称 -> 小题题号的映射，供小题结果提取时匹配。
_RESULT_KEY_QUESTION_ALIASES = {
    "新建项目与界面设计": "4.1",
    "数据库连接与顾客下拉列表": "4.2",
    "顾客订单查询显示": "4.3",
    "新增消费券与清空控件": "4.4",
    "退出系统": "4.5",
    "创建模块与三个页面": "4.6",
    "添加数据与截图": "4.7",
    "订单管理页面查询功能": "4.8",
    "拷贝views文件夹": "4.9",
}


def _result_key_matches_question(key, question_no, index=None):
    normalized_key = _normalize_result_question_no(key)
    normalized_question_no = _normalize_result_question_no(question_no)
    if not normalized_key or not normalized_question_no:
        return False
    if normalized_key == normalized_question_no:
        return True
    alias_question_no = _RESULT_KEY_QUESTION_ALIASES.get(
        re.sub(r"\s+", "", str(key or ""))
    )
    if alias_question_no and alias_question_no == normalized_question_no:
        return True
    question_parts = normalized_question_no.split(".", 1)
    sub_no = question_parts[1] if len(question_parts) > 1 else question_parts[0]
    ordinal = str(index + 1) if isinstance(index, int) and index >= 0 else sub_no
    lowered_key = normalized_key.lower().replace(" ", "")
    return normalized_key == sub_no or lowered_key in {
        ordinal,
        f"小题{ordinal}",
        f"子题{ordinal}",
        f"第{ordinal}小题",
        f"第{ordinal}题",
        f"question{ordinal}",
        f"subquestion{ordinal}",
        f"item{ordinal}",
    }


def _extract_sub_question_result(result, question_no, index=None, total_count=None):
    """从一次大题评分结果中取出指定小题结果，兼容常见 MCP 返回结构。"""
    normalized = _parse_result_json(result)
    target_question_no = _normalize_result_question_no(question_no)
    if not target_question_no:
        return normalized

    def from_mapping(mapping):
        if not isinstance(mapping, dict):
            return None
        for key, value in mapping.items():
            if _result_key_matches_question(key, target_question_no, index=index):
                parsed_value = _parse_result_json(value)
                if isinstance(parsed_value, str):
                    # 形如 “2分 - 具体解析”或“6.5/8”的字符串得分，包装成统一结构
                    score_match = re.match(r"\s*(\d+(?:\.\d+)?)\s*分", parsed_value)
                    if score_match:
                        return {
                            "score": float(score_match.group(1)),
                            "comment": parsed_value,
                        }
                    fraction_match = re.match(
                        r"\s*(\d+(?:\.\d+)?)\s*/\s*\d+(?:\.\d+)?", parsed_value
                    )
                    if fraction_match:
                        return {
                            "score": float(fraction_match.group(1)),
                            "comment": parsed_value,
                        }
                return parsed_value
        for key in ("results", "question_results", "scores", "score_data", "data", "result"):
            value = _parse_result_json(mapping.get(key))
            if isinstance(value, dict):
                matched = from_mapping(value)
                if matched is not None:
                    return matched
            if isinstance(value, list):
                matched = from_list(value)
                if matched is not None:
                    return matched
        for key in ("sub_questions", "questions", "items"):
            value = _parse_result_json(mapping.get(key))
            if isinstance(value, list):
                matched = from_list(value)
                if matched is not None:
                    return matched
        return None

    def item_question_no(item):
        if not isinstance(item, dict):
            return ""
        for key in ("question_no", "questionNo", "no", "qid", "label", "question_label"):
            value = item.get(key)
            if value:
                return _normalize_result_question_no(value)
        return ""

    def from_list(items):
        if not isinstance(items, list):
            return None
        for item in items:
            parsed_item = _parse_result_json(item)
            if isinstance(parsed_item, dict) and _result_key_matches_question(
                item_question_no(parsed_item),
                target_question_no,
                index=index,
            ):
                return parsed_item
        if total_count == 1 and len(items) == 1:
            return _parse_result_json(items[0])
        if index is not None and 0 <= index < len(items):
            parsed_item = _parse_result_json(items[index])
            if isinstance(parsed_item, dict):
                return parsed_item
        return None

    if isinstance(normalized, dict):
        matched = from_mapping(normalized)
        if matched is not None:
            return matched
    elif isinstance(normalized, list):
        matched = from_list(normalized)
        if matched is not None:
            return matched

    if total_count == 1:
        return normalized
    return {
        "question_no": target_question_no,
        "raw_major_result": normalized,
        "extract_error": "未在大题评分结果中匹配到该小题题号",
    }


def _extract_score_point_for_question(result, question_no, max_score=None):
    """单题评分时从整题结果中提取该小题的得分点。

    大题级评分服务（如创意设计海报评分）无论评哪一小问都返回整道大题的
    结果（含各得分点分数）。单题接口（question_no 形如 "4.1"）必须只取
    本小问对应的得分点，否则整题总分会原样记到每个小问头上、总分翻倍。
    结果中没有该小题的得分点时原样返回（保持既有单题服务的行为）。
    """
    normalized = _normalize_result_question_no(question_no)
    if "." not in normalized:
        return result
    extracted = _extract_sub_question_result(result, question_no, index=None, total_count=1)
    if isinstance(extracted, dict) and extracted.get("extract_error"):
        return result
    return _clamp_score_result(extracted, max_score)


def _append_major_question_return_rule(question_text, sub_questions):
    if not sub_questions:
        return question_text
    question_nos = [
        _normalize_result_question_no(item.get("question_no"))
        for item in sub_questions
        if isinstance(item, dict) and item.get("question_no")
    ]
    question_nos = [item for item in question_nos if item]
    if not question_nos:
        return question_text
    example_items = ", ".join(
        f'"{question_no}": {{"score": 分数, "comment": "评语"}}'
        for question_no in question_nos
    )
    return (
        f"{question_text}\n\n"
        "返回要求：本次提交的是同一道大题下的多道小题。"
        "请分别依据每道小题给出的参考答案、评分标准和满分评分，"
        "并按小题题号返回 JSON 对象，不要只返回大题总分。"
        f"\n返回格式示例：{{{example_items}}}"
    )
def _sanitize_prompt_text(value):
    return str(value or "").replace('"""', "'''").strip()


def _inject_max_score_prompt(code, question_text, answer_text, max_score, multi_service=False):
    if max_score is None:
        return code

    if multi_service:
        # 多服务分工评阅：评分标准文本（已包含在题目内容里）标注了
        # 各部分的满分，让模型只对自己负责的部分按标准中的满分评分。
        extra_prompt = (
            "\n本次评阅由多个评分服务分工完成，题目内容中附带的评分标准"
            "标注了各部分的满分。请只针对你负责的部分作答评分，"
            "严格按照评分标准中该部分的满分打分，total_score 不得超过该部分满分。"
            f"\n题目内容：{_sanitize_prompt_text(question_text)}"
            f"\n学生作答：{_sanitize_prompt_text(answer_text)}"
        )
    else:
        extra_prompt = (
            f"\n本次只评分当前题目，满分为 {max_score} 分。"
            f"\n题目内容：{_sanitize_prompt_text(question_text)}"
            f"\n学生作答：{_sanitize_prompt_text(answer_text)}"
            f"\n请严格按照当前题目满分评分，total_score 必须在 0 到 {max_score} 之间，不能超过满分。"
        )
    marker = "你是一个严谨的专业阅卷智能体。请对这张试卷图片进行自动评分。"
    if marker in code:
        return code.replace(marker, marker + extra_prompt, 1)
    return code


def _select_grading_base_folder(absolute_base_folder, student_answer_folder, student_no, image_name):
    """选择评分模块使用的 base_media_folder。

    优先考生答案目录：数字媒体等文件作答类题目的考生成果（.sesx/.prproj/.fla/.max/
    Unity 工程/站点文件夹/海报文件等）都落在答案目录中；媒体目录里只有题干截图，
    若作为扫描根，服务会把 png 当作答案文件误解析。

    仅当答案目录为空（如纯截图作答题）时才回退到媒体目录，供基于上传截图的评分服务使用。
    """
    media_posix = (
        absolute_base_folder.as_posix()
        if hasattr(absolute_base_folder, "as_posix")
        else str(absolute_base_folder or "")
    )
    if student_answer_folder:
        try:
            with os.scandir(student_answer_folder) as entries:
                if any(True for _ in entries):
                    # 评分模块统一按 {base}/{学号}/KS/... 或 {base}/{学号}_KS/... 定位，
                    # base 必须是包含学号目录的父目录（全班根），不能是考生自己的 KS，
                    # 否则会拼出 KS/{学号}/KS/... 而找不到任何作答文件。
                    answer_path = Path(student_answer_folder)
                    class_root = answer_path.parent.parent
                    if class_root.is_dir():
                        return class_root.as_posix()
                    return student_answer_folder
        except OSError:
            pass
    try:
        if image_name and _resolve_grading_image_path(student_no, image_name, media_posix):
            return media_posix
    except Exception:
        pass
    return media_posix


def _resolve_grading_image_path(student_no, media_path, base_media_folder):
    """Resolve the local image path used by a grading request."""
    if not media_path or not base_media_folder:
        return None

    image_name = Path(str(media_path)).name
    candidates = (
        Path(base_media_folder) / f"{student_no}_KS" / image_name,
        Path(base_media_folder) / student_no / "KS" / image_name,
        Path(base_media_folder) / student_no / image_name,
        Path(base_media_folder) / image_name,
    )
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_file():
            return resolved
    return None


def _rewrite_multimodal_content(content, local_image_path):
    """Convert unsupported image data URLs to a local image path.

    DashScope's multimodal SDK can upload a local path itself, while the
    LangChain adapter does not accept a data:image URL for this request.
    """
    if isinstance(content, list):
        return [
            _rewrite_multimodal_content(item, local_image_path)
            for item in content
        ]
    if not isinstance(content, dict):
        return content

    rewritten = dict(content)
    image_url = rewritten.get("image_url")
    if isinstance(image_url, dict):
        image_url_value = image_url.get("url")
        if isinstance(image_url_value, str) and image_url_value.startswith("data:image/"):
            return {
                "type": "image",
                "image": str(local_image_path),
            }
    elif isinstance(image_url, str) and image_url.startswith("data:image/"):
        return {
            "type": "image",
            "image": str(local_image_path),
        }

    image_value = rewritten.get("image")
    if isinstance(image_value, str) and image_value.startswith("data:image/"):
        rewritten["image"] = str(local_image_path)

    for key, value in tuple(rewritten.items()):
        if isinstance(value, (dict, list)):
            rewritten[key] = _rewrite_multimodal_content(
                value,
                local_image_path,
            )
    return rewritten


def _install_multimodal_image_compatibility(module_obj, local_image_path):
    """Patch one dynamically loaded MCP module's Tongyi class only."""
    original_chat_tongyi = getattr(module_obj, "ChatTongyi", None)
    if original_chat_tongyi is None or local_image_path is None:
        return

    class CompatibleChatTongyi(original_chat_tongyi):
        def invoke(self, input, config=None, *, stop=None, **kwargs):
            if isinstance(input, list):
                rewritten_messages = []
                for message in input:
                    if hasattr(message, "content"):
                        message.content = _rewrite_multimodal_content(
                            message.content,
                            local_image_path,
                        )
                    elif isinstance(message, dict):
                        message = dict(message)
                        message["content"] = _rewrite_multimodal_content(
                            message.get("content"),
                            local_image_path,
                        )
                    rewritten_messages.append(message)
                input = rewritten_messages
            elif hasattr(input, "content"):
                input.content = _rewrite_multimodal_content(
                    input.content,
                    local_image_path,
                )
            elif isinstance(input, dict):
                input = dict(input)
                input["content"] = _rewrite_multimodal_content(
                    input.get("content"),
                    local_image_path,
                )
            return super().invoke(
                input,
                config,
                stop=stop,
                **kwargs,
            )

    module_obj.ChatTongyi = CompatibleChatTongyi


def _execute_evaluate_paper(
    module,
    service,
    student_no,
    media_path,
    base_media_folder,
    question_text="",
    answer_text="",
    max_score=None,
    multi_service=False,
    answer_folder=None,
    answer_images=None,
    setup_sql=None,
    adaptation_rules=None,
):
    if not module or not module.code:
        raise ValueError("所选MCP没有可执行代码")

    code = module.code
    config_params = _parse_json_dict(getattr(service, "config_params", None)) if service else {}
    if "${" in code and config_params:
        code = service_manager.replace_config_params(code, config_params)
    # 模块内 ChatTongyi 等模型依赖 DASHSCOPE_API_KEY 环境变量，
    # 服务配置里 api/api_key/DASHSCOPE_API_KEY 任一字段都视为模型 key，
    # 且配置值优先于进程已有变量，避免其他服务遗留的失效 key 污染本次评阅
    configured_api_key = str(
        (config_params or {}).get("api")
        or (config_params or {}).get("api_key")
        or (config_params or {}).get("DASHSCOPE_API_KEY")
        or ""
    ).strip()
    if configured_api_key:
        os.environ["DASHSCOPE_API_KEY"] = configured_api_key
    code = _inject_max_score_prompt(code, question_text, answer_text, max_score, multi_service=multi_service)

    temp_dir = Path(settings.MCP_BASE_DIR) / "data" / "script" / "teacher_scoring_mcp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    module_name = f"teacher_scoring_mcp_{module.id}_{uuid4().hex}"
    module_path = temp_dir / f"{module_name}.py"
    module_path.write_text(code, encoding="utf-8")

    try:
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if not spec or not spec.loader:
            raise ValueError("无法加载所选MCP模块")
        module_obj = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module_obj)
        local_image_path = _resolve_grading_image_path(
            student_no,
            media_path,
            base_media_folder,
        )
        _install_multimodal_image_compatibility(
            module_obj,
            local_image_path,
        )
        grading_func_name = _find_grading_function(module_obj)
        if not grading_func_name:
            raise ValueError(
                "所选MCP中没有可用于评分的函数，请提供 evaluate_paper、"
                "evaluate_short_answer 或其他 evaluate/grade 开头的函数"
            )
        grading_func = getattr(module_obj, grading_func_name)
        grading_prompt = question_text
        if multi_service:
            grading_prompt = (
                f"{question_text}\n\n"
                "评分要求：本题由多个评分服务分工评阅，请只针对你负责的部分，"
                "严格按照评分标准中该部分的满分评分，total_score 不得超过该部分满分。"
            )
        elif max_score is not None:
            grading_prompt = (
                f"{question_text}\n\n"
                f"评分要求：本题满分为 {max_score} 分。请严格按该满分评分，"
                f"最终 total_score 必须在 0 到 {max_score} 之间，不能超过满分。"
            )
        result = _call_grading_function(
            grading_func,
            student_no=student_no,
            media_path=media_path,
            base_media_folder=base_media_folder,
            question_text=grading_prompt,
            answer_text=answer_text,
            max_score=max_score,
            answer_folder=answer_folder,
            answer_images=answer_images,
            setup_sql=setup_sql,
            adaptation_rules=adaptation_rules,
        )
        return _clamp_score_result(result, max_score, multi_service=multi_service)
    finally:
        try:
            module_path.unlink(missing_ok=True)
        except TypeError:
            if module_path.exists():
                module_path.unlink()


GRADING_FUNCTION_PRIORITY = (
    # 数字媒体第二大题（视频/网页制作）唯一入口，优先于字母序兜底，
    # 避免误选到 grade_dw_exam 等分支函数
    "grade_exam_folder",
    "evaluate_paper",
    "evaluate_short_answer",
    "evaluate_short_answer_v2",
    "evaluate_sql_mcp_tool",
    "evaluate_project_mcp_tool",
    "evaluate_project_v2",
    "evaluate_fourth_question",
    "evaluate_fourth_question_v1",
    "evaluate_fourth_question_default",
)


def _find_grading_function(module_obj):
    functions = {
        name: func
        for name, func in inspect.getmembers(module_obj, inspect.isfunction)
        if not name.startswith("_")
        and getattr(func, "__module__", None) == getattr(module_obj, "__name__", None)
    }
    for name in GRADING_FUNCTION_PRIORITY:
        if name in functions:
            return name
    for name in sorted(functions):
        lowered = name.lower()
        if lowered.startswith(("evaluate", "grade", "score", "review")):
            return name
    return None


def _call_grading_function(
    grading_func,
    *,
    student_no,
    media_path,
    base_media_folder,
    question_text,
    answer_text,
    max_score,
    answer_folder=None,
    answer_images=None,
    setup_sql=None,
    adaptation_rules=None,
):
    """按评分函数签名映射统一评分上下文，兼容文本、SQL和图片评分模板。"""
    question_content, reference_answer, scoring_rubric = _split_grading_context(question_text)
    # 需要遍历考生提交文件（如第四大题项目文件夹）的模块，优先传真实答案目录
    effective_answer_folder = str(answer_folder) if answer_folder else base_media_folder
    values = {
        "student_id": student_no,
        "student_no": student_no,
        "question_text": question_content,
        "question_content": question_content,
        "question_stem": question_content,
        "stem": question_content,
        "question": question_content,
        "answer_text": answer_text,
        "student_answer": answer_text,
        "student_sql": answer_text,
        "student_code": answer_text,
        "student_text": answer_text,
        "reference_answer": reference_answer,
        "key_concept": reference_answer,
        "key_concepts": reference_answer,
        "expected_answer": reference_answer,
        "standard_answer": reference_answer,
        "reference_sql": reference_answer,
        "reference_code": reference_answer,
        "scoring_rubric": scoring_rubric,
        "scoring_rubrics": scoring_rubric,
        "rubric": scoring_rubric,
        "rubrics": scoring_rubric,
        "grading_criteria": scoring_rubric,
        "criteria": scoring_rubric,
        "setup_sql": setup_sql or "",
        "adaptation_rules": adaptation_rules or "",
        "max_score": max_score,
        "full_score": max_score,
        "total_score": max_score,
        "media_path": media_path,
        "base_media_folder": base_media_folder,
        "student_answer_path": effective_answer_folder,
        "answer_folder_path": effective_answer_folder,
        # 数字媒体第二大题等模块以 folder_path 作为考生答案目录形参
        "folder_path": effective_answer_folder,
        # 大题各小题的作答截图（题号 -> 绝对路径列表），供图片取证/存在性评分模块使用
        "answer_images_json": (
            json.dumps(answer_images, ensure_ascii=False)
            if isinstance(answer_images, dict) and answer_images else "{}"
        ),
    }
    signature = inspect.signature(grading_func)
    call_kwargs = {}
    missing = []
    accepts_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    for name, parameter in signature.parameters.items():
        if parameter.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        if name in values:
            call_kwargs[name] = values[name]
        elif parameter.default is inspect.Parameter.empty:
            missing.append(name)

    if missing:
        raise ValueError(
            f"评分函数 {getattr(grading_func, '__name__', 'unknown')} "
            f"缺少无法自动映射的参数: {', '.join(missing)}"
        )

    if accepts_kwargs:
        for name, value in values.items():
            call_kwargs.setdefault(name, value)
    return grading_func(**call_kwargs)


def _split_grading_context(question_text):
    """从统一题目文本中提取题目、参考答案和评分标准。"""
    text = str(question_text or "").strip()
    marker_match = re.search(r"\n\n【[^】]+】\n", text)
    if not marker_match:
        return text, "", ""

    question_content = text[:marker_match.start()].strip()
    standard_text = text[marker_match.end():].strip()
    reference_match = re.search(
        r"参考答案：(?P<reference>.*?)(?=\n评分标准：|$)",
        standard_text,
        flags=re.DOTALL,
    )
    rubric_match = re.search(r"评分标准：(?P<rubric>.*)$", standard_text, flags=re.DOTALL)
    reference_answer = reference_match.group("reference").strip() if reference_match else ""
    scoring_rubric = rubric_match.group("rubric").strip() if rubric_match else ""
    return question_content, reference_answer, scoring_rubric


def _load_standard_item_mcp_params(db, standard_id, question_no, question_version_no=None):
    """按评阅标准项查 mcp_params（如 SQL 评分的 setup_sql 建表语句）。

    先精确匹配题号；匹配不到时按大题号/首小题别名再试一次。
    """
    if not standard_id:
        return {}
    normalized = _normalize_result_question_no(question_no)
    if not normalized:
        return {}
    candidates = [normalized]
    if "." in normalized:
        candidates.append(normalized.split(".", 1)[0])
    else:
        candidates.append(f"{normalized}.1")
    for candidate in candidates:
        try:
            rows = db.execute(
                select(BizExamGradingStandardItem).where(
                    BizExamGradingStandardItem.standard_id == int(standard_id),
                    BizExamGradingStandardItem.question_no == candidate,
                )
            ).scalars().all()
        except Exception as exc:
            mcp_logger.error(f"查询评分标准项失败: standard_id={standard_id}, question_no={candidate}, error={str(exc)}")
            return {}
        if not rows:
            continue
        if question_version_no:
            matched = [row for row in rows if int(row.question_version_no or 0) == int(question_version_no)]
            rows = matched or rows
        row = sorted(rows, key=lambda item: int(item.question_version_no or 0))[-1]
        params = row.mcp_params if isinstance(row.mcp_params, dict) else {}
        return params
    return {}


def _extract_setup_sql(question_text, mcp_params=None):
    """组装 SQL 评分的沙盒建表语句。

    优先取评分标准项 mcp_params.setup_sql（教师配置的标准建库脚本）；
    未配置时退化为从参考答案里提取 CREATE TABLE / INSERT 语句。
    """
    if isinstance(mcp_params, dict):
        configured = str(mcp_params.get("setup_sql") or "").strip()
        if configured:
            return configured
    text = str(question_text or "")
    match = re.search(
        r"参考答案：(?P<reference>.*?)(?=\n评分标准：|$)",
        text,
        flags=re.DOTALL,
    )
    reference = match.group("reference") if match else ""
    statements = []
    for statement in re.split(r";\s*\n*", str(reference or "")):
        cleaned = statement.strip()
        if re.match(r"(?i)^(create\s+table|insert\s+into)\b", cleaned):
            statements.append(cleaned)
    return ";\n".join(statements)


async def _save_exam_file(upload_file, user_id, subject_id, file_type):
    if not upload_file or not getattr(upload_file, "filename", None):
        return None

    safe_name = _safe_upload_name(upload_file.filename)
    save_dir = (
        Path(settings.MCP_BASE_DIR)
        / "media"
        / "teacher_scoring"
        / "exams"
        / str(user_id)
        / str(subject_id)
    )
    save_dir.mkdir(parents=True, exist_ok=True)

    saved_name = f"{file_type}_{uuid4().hex}_{safe_name}"
    save_path = save_dir / saved_name
    await _save_upload_file_stream(upload_file, save_path)

    return save_path.relative_to(settings.MCP_BASE_DIR).as_posix()


async def _save_package_file(upload_file, user_id, subject_id, exam_id):
    if not upload_file or not getattr(upload_file, "filename", None):
        return None, None

    safe_name = _safe_upload_name(upload_file.filename)
    save_dir = (
        Path(settings.MCP_BASE_DIR)
        / "media"
        / "teacher_scoring"
        / "packages"
        / str(user_id)
        / str(subject_id)
        / str(exam_id)
    )
    save_dir.mkdir(parents=True, exist_ok=True)

    saved_name = f"package_{uuid4().hex}_{safe_name}"
    save_path = save_dir / saved_name
    await _save_upload_file_stream(upload_file, save_path)

    return save_path, save_path.relative_to(settings.MCP_BASE_DIR).as_posix()


def _student_no_from_file(path: Path):
    return path.stem[:50] or "unknown"


# _detect_upload_type / _is_student_answer_document / _is_answer_supporting_document /
# _is_blank_supporting_document 及其辅助函数已收敛至 file_role_recognition
# （第二步改造），经顶部 import 以原名称继续暴露，调用点无需改动。


def _zip_member_parts(member_name):
    normalized = member_name.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    if any(part == ".." for part in parts):
        return []
    return parts


def _safe_extract_zip(zip_path: Path, extract_dir: Path):
    extract_dir.mkdir(parents=True, exist_ok=True)
    extract_root = extract_dir.resolve()
    with zipfile.ZipFile(zip_path, "r") as zip_file:
        for member_batch in _iter_batches(zip_file.infolist()):
            for member in member_batch:
                parts = _zip_member_parts(member.filename)
                if not parts:
                    continue
                target_path = extract_dir.joinpath(*parts).resolve()
                try:
                    target_path.relative_to(extract_root)
                except ValueError:
                    continue
                if member.is_dir():
                    target_path.mkdir(parents=True, exist_ok=True)
                    continue
                target_path.parent.mkdir(parents=True, exist_ok=True)
                with zip_file.open(member) as source, target_path.open("wb") as target:
                    _copy_stream_in_chunks(source, target)


def _decode_json_bytes(raw_content):
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw_content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw_content.decode("utf-8", errors="ignore")


def _is_likely_student_object(obj):
    return isinstance(obj, dict) and (
        bool(obj.get("id")) or
        bool(obj.get("studentId")) or
        isinstance(obj.get("result"), list)
    )


def _student_no_from_json_entry(entry, fallback_index):
    for key in ("id", "studentId", "student_no", "studentNo", "student_id"):
        value = entry.get(key) if isinstance(entry, dict) else None
        if value is not None and str(value).strip():
            return str(value).strip()[:50]
    return f"student-{fallback_index + 1}"


def _extract_student_entries(json_obj):
    """Use the same student-detection approach as test.html."""
    entries = []
    if isinstance(json_obj, list):
        for item in json_obj:
            if _is_likely_student_object(item):
                entries.append(item)
    elif _is_likely_student_object(json_obj):
        entries.append(json_obj)
    elif isinstance(json_obj, dict):
        for value in json_obj.values():
            if _is_likely_student_object(value):
                entries.append(value)
    return entries


def _find_answer_json_in_zip(zip_path: Path):
    json_candidates = []
    parsed_json_count = 0
    with zipfile.ZipFile(zip_path, "r") as zip_file:
        for member_batch in _iter_batches(zip_file.infolist()):
            for member in member_batch:
                if member.is_dir():
                    continue
                parts = _zip_member_parts(member.filename)
                if not parts or Path(parts[-1]).suffix.lower() != ".json":
                    continue
                json_candidates.append(member)

        for member in sorted(json_candidates, key=lambda item: (len(_zip_member_parts(item.filename)), item.filename)):
            with zip_file.open(member) as json_file:
                text = _decode_json_bytes(json_file.read())
            try:
                json_obj = json.loads(text)
            except json.JSONDecodeError:
                continue
            parsed_json_count += 1
            entries = _extract_student_entries(json_obj)
            if entries:
                return (
                    json_obj,
                    entries,
                    "/".join(_zip_member_parts(member.filename)),
                    json_candidates,
                    parsed_json_count,
                )

    return None, [], None, json_candidates, parsed_json_count


def _build_package_image_files(extract_dir: Path, package_relative_path: str):
    files = []
    for file_path in extract_dir.rglob("*"):
        if not file_path.is_file() or file_path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        relative_path = file_path.relative_to(extract_dir).as_posix()
        storage_path = file_path.relative_to(settings.MCP_BASE_DIR).as_posix()
        files.append({
            "file_name": file_path.name,
            "relative_path": relative_path,
            "storage_path": storage_path,
            "file_type": _detect_upload_type(file_path),
            "file_size": file_path.stat().st_size,
            "package_relative_path": package_relative_path,
        })
    return files


def _normalize_image_ref(image_ref):
    normalized = str(image_ref).replace("\\", "/").strip().split("?")[0].split("#")[0]
    return normalized.lstrip("./").lower()


def _build_image_ref_lookup(image_refs):
    normalized_refs = {
        normalized_ref
        for image_ref in image_refs or []
        for normalized_ref in [_normalize_image_ref(image_ref)]
        if normalized_ref
    }
    ref_file_names = {
        normalized_ref.split("/")[-1]
        for normalized_ref in normalized_refs
        if normalized_ref.split("/")[-1]
    }
    return normalized_refs, ref_file_names


def _zip_member_matches_image_refs(parts, normalized_refs, ref_file_names):
    relative_path = "/".join(parts).lower()
    file_name = parts[-1].lower()
    if file_name in ref_file_names:
        return True
    return any(
        relative_path.endswith(normalized_ref) or normalized_ref.endswith(relative_path)
        for normalized_ref in normalized_refs
    )


def _extract_zip_media_files(zip_path: Path, package_id: int, image_refs=None):
    extract_dir = zip_path.parent / f"{zip_path.stem}_media_{package_id}"
    extract_dir.mkdir(parents=True, exist_ok=True)
    extract_root = extract_dir.resolve()
    files = []
    package_relative_path = zip_path.relative_to(settings.MCP_BASE_DIR).as_posix()
    restrict_to_refs = image_refs is not None
    normalized_refs, ref_file_names = _build_image_ref_lookup(image_refs)

    if restrict_to_refs and not normalized_refs:
        return files, extract_dir.relative_to(settings.MCP_BASE_DIR).as_posix()

    with zipfile.ZipFile(zip_path, "r") as zip_file:
        for member_batch in _iter_batches(zip_file.infolist()):
            for member in member_batch:
                if member.is_dir():
                    continue

                parts = _zip_member_parts(member.filename)
                if not parts:
                    continue

                source_relative_path = "/".join(parts)
                file_name = parts[-1]
                suffix = Path(file_name).suffix.lower()
                if suffix not in IMAGE_SUFFIXES:
                    continue
                if restrict_to_refs and not _zip_member_matches_image_refs(parts, normalized_refs, ref_file_names):
                    continue

                media_index = next(
                    (index for index, part in enumerate(parts) if part.lower() == "media"),
                    None,
                )
                useful_parts = parts[media_index:] if media_index is not None else parts
                target_path = extract_dir.joinpath(*useful_parts).resolve()
                try:
                    target_path.relative_to(extract_root)
                except ValueError:
                    continue

                target_path.parent.mkdir(parents=True, exist_ok=True)
                with zip_file.open(member) as source, target_path.open("wb") as target:
                    _copy_stream_in_chunks(source, target)

                files.append({
                    "file_name": file_name,
                    "relative_path": source_relative_path,
                    "storage_path": target_path.relative_to(settings.MCP_BASE_DIR).as_posix(),
                    "file_type": _detect_upload_type(target_path),
                    "file_size": member.file_size,
                    "package_relative_path": package_relative_path,
                })

    return files, extract_dir.relative_to(settings.MCP_BASE_DIR).as_posix()

def _image_matches_student(file_row, student_no):
    if not student_no:
        return False
    normalized_student = str(student_no).lower()
    relative_path = file_row["relative_path"].lower()
    file_name = file_row["file_name"].lower()
    return normalized_student in relative_path or normalized_student in file_name


def _collect_json_image_refs(value):
    refs = []
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).lower()
            if normalized_key in {"images", "image", "img", "imgs", "picture", "pictures", "image_url", "imageurl", "image_path", "imagepath", "path", "url", "src"}:
                refs.extend(_collect_json_image_refs(item))
            else:
                refs.extend(_collect_json_image_refs(item))
    elif isinstance(value, list):
        for item in value:
            refs.extend(_collect_json_image_refs(item))
    elif isinstance(value, str):
        normalized_value = value.replace("\\", "/").strip()
        suffix = Path(normalized_value.split("?")[0]).suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            refs.append(normalized_value)
    return refs


def _image_matches_refs(file_row, image_refs):
    relative_path = file_row["relative_path"].replace("\\", "/").lower()
    file_name = file_row["file_name"].lower()
    for image_ref in image_refs:
        normalized_ref = _normalize_image_ref(image_ref)
        ref_file_name = normalized_ref.split("/")[-1]
        if normalized_ref and (relative_path.endswith(normalized_ref) or normalized_ref.endswith(relative_path)):
            return True
        if ref_file_name and ref_file_name == file_name:
            return True
    return False


def _parse_json_package(zip_path: Path, package_id: int):
    json_obj, entries, json_relative_path, _, _ = _find_answer_json_in_zip(zip_path)
    if not entries:
        return [], None

    package_image_refs = []
    for entry in entries:
        package_image_refs.extend(_collect_json_image_refs(entry))
    image_files, extract_relative_path = _extract_zip_media_files(
        zip_path,
        package_id,
        package_image_refs,
    )

    answer_rows = []
    for index, entry in enumerate(entries):
        student_no = _student_no_from_json_entry(entry, index)
        image_refs = _collect_json_image_refs(entry)
        matched_files = [
            file_row for file_row in image_files
            if _image_matches_refs(file_row, image_refs)
        ]
        answer_rows.append({
            "student_no": student_no,
            "upload_type": "JSON",
            "text_content": json.dumps(entry, ensure_ascii=False),
            "page_mapping": entry,
            "json_relative_path": json_relative_path,
            "files": matched_files,
        })

    return answer_rows, extract_relative_path


def _student_no_from_zip_parts(parts, common_root=None):
    useful_parts = parts
    if common_root and useful_parts and useful_parts[0] == common_root:
        useful_parts = useful_parts[1:]

    if len(useful_parts) >= 2:
        return useful_parts[0][:50]
    if useful_parts:
        return Path(useful_parts[0]).stem[:50] or "unknown"
    return "unknown"


def _index_zip_answer_sheets(zip_path: Path):
    with zipfile.ZipFile(zip_path, "r") as zip_file:
        file_items = []
        for member in zip_file.infolist():
            if member.is_dir():
                continue

            parts = _zip_member_parts(member.filename)
            if not parts:
                continue

            file_items.append({
                "parts": parts,
                "zip_path": "/".join(parts),
                "file_name": parts[-1],
                "file_size": member.file_size,
            })

    if not file_items:
        return []

    first_parts = {item["parts"][0] for item in file_items if item["parts"]}
    common_root = None
    if len(first_parts) == 1 and any(len(item["parts"]) >= 3 for item in file_items):
        common_root = next(iter(first_parts))

    answers = {}
    for item in file_items:
        parts = item["parts"]
        student_no = _student_no_from_zip_parts(parts, common_root)
        if student_no not in answers:
            answers[student_no] = {
                "student_no": student_no,
                "upload_type": _detect_upload_type(Path(parts[-1])),
                "files": [],
            }
        answers[student_no]["files"].append({
            "file_name": item["file_name"],
            "relative_path": item["zip_path"],
            "file_type": _detect_upload_type(Path(item["file_name"])),
            "file_size": item["file_size"],
        })

    return list(answers.values())


def _parse_package_answers_background(package_id, exam_id, saved_path_str):
    try:
        saved_path = Path(saved_path_str)
        answer_rows = []
        indexed_path = None

        if saved_path.suffix.lower() == ".zip":
            answer_rows, indexed_path = _parse_json_package(saved_path, package_id)
            if not answer_rows:
                mcp_logger.warning(f"试卷包未解析出考生答题 JSON 数据: package_id={package_id}")
                return
        elif saved_path.suffix.lower() == ".json":
            text = _decode_json_bytes(saved_path.read_bytes())
            json_obj = json.loads(text)
            entries = _extract_student_entries(json_obj)
            answer_rows = [{
                "student_no": _student_no_from_json_entry(entry, index),
                "upload_type": "JSON",
                "text_content": json.dumps(entry, ensure_ascii=False),
                "page_mapping": entry,
                "files": [{
                    "file_name": saved_path.name,
                    "relative_path": saved_path.relative_to(settings.MCP_BASE_DIR).as_posix(),
                    "storage_path": saved_path.relative_to(settings.MCP_BASE_DIR).as_posix(),
                    "file_type": _detect_upload_type(saved_path),
                    "file_size": saved_path.stat().st_size if saved_path.exists() else None,
                }],
            } for index, entry in enumerate(entries)]
            indexed_path = saved_path.relative_to(settings.MCP_BASE_DIR).as_posix()
        else:
            answer_rows = [{
                "student_no": _student_no_from_file(saved_path),
                "upload_type": _detect_upload_type(saved_path),
                "files": [{
                    "file_name": saved_path.name,
                    "relative_path": saved_path.relative_to(settings.MCP_BASE_DIR).as_posix(),
                    "storage_path": saved_path.relative_to(settings.MCP_BASE_DIR).as_posix(),
                    "file_type": _detect_upload_type(saved_path),
                    "file_size": saved_path.stat().st_size if saved_path.exists() else None,
                }],
            }]

        with get_db() as db:
            package = db.execute(
                select(BizExamPackage).where(BizExamPackage.package_id == package_id)
            ).scalar_one_or_none()
            if not package:
                return

            if indexed_path:
                package.extract_path = indexed_path

            inserted_count = 0
            batch_size = DB_INSERT_BATCH_SIZE
            pending_count = 0
            for answer_row in answer_rows:
                answer = BizAnswerSheet(
                    student_no=answer_row["student_no"],
                    exam_id=exam_id,
                    package_id=package_id,
                    upload_type=answer_row["upload_type"],
                    file_path=package.file_path,
                    text_content=answer_row.get("text_content"),
                    page_mapping=answer_row.get("page_mapping"),
                )
                db.add(answer)
                db.flush()

                for file_row in answer_row.get("files", []):
                    answer_file = BizAnswerFile(
                        answer_id=answer.answer_id,
                        exam_id=exam_id,
                        package_id=package_id,
                        student_no=answer_row["student_no"],
                        file_name=file_row["file_name"][:255],
                        file_path=file_row.get("storage_path") or package.file_path,
                        file_type=file_row["file_type"],
                        relative_path=file_row["relative_path"][:500],
                        file_size=file_row.get("file_size"),
                    )
                    db.add(answer_file)

                pending_count += 1
                inserted_count += 1

                if pending_count >= batch_size:
                    db.commit()
                    pending_count = 0

            db.commit()
            mcp_logger.info(
                f"试卷包 JSON 解析完成: package_id={package_id}, answers={inserted_count}"
            )
    except Exception as e:
        mcp_logger.error(f"后台解析试卷包失败: package_id={package_id}, error={str(e)}")


async def _get_owned_exam(db, user_id, subject_id, exam_id):
    subject = db.execute(
        select(BizSubject).where(
            BizSubject.subject_id == subject_id,
            BizSubject.creator_id == int(user_id),
        )
    ).scalar_one_or_none()
    if not subject:
        return None

    return db.execute(
        select(BizExam).where(
            BizExam.id == exam_id,
            BizExam.subject_id == subject_id,
        )
    ).scalar_one_or_none()


async def _get_owned_package(db, user_id, subject_id, exam_id, package_id):
    exam = await _get_owned_exam(db, user_id, subject_id, exam_id)
    if not exam:
        return None

    return db.execute(
        select(BizExamPackage).where(
            BizExamPackage.package_id == package_id,
            BizExamPackage.exam_id == exam_id,
        )
    ).scalar_one_or_none()



async def _get_owned_local_package(db, user_id, subject_id, exam_id, local_package_id):
    exam = await _get_owned_exam(db, user_id, subject_id, exam_id)
    if not exam:
        return None

    return db.execute(
        select(BizLocalExamPackage).where(
            BizLocalExamPackage.local_package_id == int(local_package_id),
            BizLocalExamPackage.subject_id == subject_id,
            BizLocalExamPackage.exam_id == exam_id,
            BizLocalExamPackage.creator_id == int(user_id),
            BizLocalExamPackage.status == "active",
        )
    ).scalar_one_or_none()


async def _resolve_owned_local_package_for_grading(
    db,
    user_id,
    subject_id,
    exam_id,
    local_package_id=None,
    *,
    package_fingerprint="",
    quick_fingerprint="",
    student_no_hash="",
):
    package = await _get_owned_local_package(db, user_id, subject_id, exam_id, local_package_id) if local_package_id else None
    if package:
        return package, ""

    base_filters = [
        BizLocalExamPackage.subject_id == subject_id,
        BizLocalExamPackage.exam_id == exam_id,
        BizLocalExamPackage.creator_id == int(user_id),
        BizLocalExamPackage.status == "active",
    ]
    lookup_options = [
        ("package_fingerprint", str(package_fingerprint or "").strip()),
        ("quick_fingerprint", str(quick_fingerprint or "").strip()),
        ("student_no_hash", str(student_no_hash or "").strip()),
    ]
    for field_name, field_value in lookup_options:
        if not field_value:
            continue
        column = getattr(BizLocalExamPackage, field_name)
        package = db.execute(
            select(BizLocalExamPackage)
            .where(*base_filters, column == field_value)
            .order_by(BizLocalExamPackage.updated_at.desc(), BizLocalExamPackage.local_package_id.desc())
        ).scalars().first()
        if package:
            return package, f"已通过 {field_name} 匹配本地试卷包"

    reason_parts = []
    if local_package_id:
        reason_parts.append(f"local_package_id={local_package_id}")
    for field_name, field_value in lookup_options:
        if field_value:
            reason_parts.append(f"{field_name}={field_value[:16]}...")
    detail = "，".join(reason_parts) or "未提供本地试卷包ID或指纹"
    return None, f"未找到可保存的本地试卷包（{detail}）"


def _normalize_local_package_id(value):
    text = str(value or "").strip()
    if text.startswith("local_"):
        text = text.split("local_", 1)[1]
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _resolve_student_answer_folder(local_package, student_no):
    """定位考生在试卷包 input 目录下的答案文件夹（如 input/32A/<学号>）。

    供需要遍历考生提交文件的大题级评分模块（如第四大题项目评分）使用。
    优先返回考生的 KS 文件夹本身（C:\\KS 提交约定）：第四大题评分服务在
    KS 路径层级上做 src/views 等低代码工程特征检测，传考生根目录会导致
    检测落空；传 KS 目录对第二大题编排服务同样兼容（其内部会识别 KS 层级）。
    """
    if not local_package or not getattr(local_package, "parsed_storage_path", None):
        return None
    safe_student_no = _safe_upload_name(student_no)
    if not safe_student_no:
        return None
    root = _resolve_local_package_storage_root(local_package)
    if root is None:
        return None
    input_root = root / "input"
    if not input_root.is_dir():
        return None
    candidates = [input_root / safe_student_no]
    try:
        for paper_dir in input_root.iterdir():
            if paper_dir.is_dir():
                candidates.append(paper_dir / safe_student_no)
    except OSError:
        pass

    seen_candidates = set()

    def _usable_candidate(candidate):
        try:
            normalized_candidate = candidate.resolve()
            if normalized_candidate in seen_candidates:
                return None
            seen_candidates.add(normalized_candidate)
            normalized_candidate.relative_to(input_root.resolve())
            if not candidate.is_dir():
                return None
            ks_folder = candidate / "KS"
            candidate_entries = list(candidate.iterdir())
            if ks_folder.is_dir():
                if any(ks_folder.iterdir()):
                    return ks_folder
                if any(item.name.casefold() != "ks" for item in candidate_entries):
                    return candidate
                return None
            if candidate_entries:
                return candidate
        except OSError:
            return None
        except ValueError:
            return None
        return None

    for candidate in candidates:
        answer_folder = _usable_candidate(candidate)
        if answer_folder is not None:
            return answer_folder

    # Uploaded folders may contain extra grouping levels (for example
    # input/batch/section/student/KS). Keep the common shallow lookup fast,
    # then walk only when the expected locations did not contain the student.
    try:
        for current_root, directory_names, _ in os.walk(input_root):
            if safe_student_no not in directory_names:
                continue
            answer_folder = _usable_candidate(Path(current_root) / safe_student_no)
            if answer_folder is not None:
                return answer_folder
    except OSError:
        pass
    return None


def _resolve_student_answer_images(local_package, student_no, topic_index):
    """取出某考生某大题各小题的作答截图，返回 {小题题号: [图片绝对路径, ...]}。

    供大题编排评分模块（如第二大题的关系图/表设计截图取证）使用。
    """
    if not local_package or not getattr(local_package, "parsed_storage_path", None):
        return {}
    if topic_index is None or topic_index < 0:
        return {}
    students = local_package.parsed_students
    if not isinstance(students, list):
        return {}
    entry = next(
        (item for item in students if isinstance(item, dict) and str(item.get("id") or "") == str(student_no)),
        None,
    )
    if not entry:
        return {}
    parts = entry.get("result") or []
    if topic_index >= len(parts):
        return {}
    package_root = _resolve_local_package_storage_root(local_package)
    if package_root is None:
        return {}
    result = {}
    sub_questions = parts[topic_index].get("sub_question") or []
    for sub_index, sub_question in enumerate(sub_questions, 1):
        if not isinstance(sub_question, dict):
            continue
        paths = []
        for relative in sub_question.get("images") or []:
            # media/blank_paper/ 下是空白卷自带的题干参考图，不是考生作答：
            # 空卷考生的“作答截图”里只剩参考图时应视为未作答（记0分兜底），
            # 否则图像类评分服务会把参考图当作品打出满分
            if "media/blank_paper/" in str(relative).replace("\\", "/").lower():
                continue
            candidate = (package_root / "parsed" / str(relative)).resolve()
            if candidate.is_file():
                paths.append(str(candidate))
        if paths:
            result[f"{topic_index + 1}.{sub_index}"] = paths
    return result


def _to_int_or_none(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_decimal_or_none(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_result_json(value):
    """把模型常见的字符串 JSON、Markdown JSON 和消息对象统一成 Python 值。"""
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return None

    content = getattr(value, "content", None)
    if content is not None and content is not value:
        parsed_content = _parse_result_json(content)
        if parsed_content is not None:
            return parsed_content

    if isinstance(value, str):
        text_value = value.strip()
        if text_value.startswith("```") and text_value.endswith("```"):
            text_value = re.sub(
                r"^```(?:json)?\s*|\s*```$",
                "",
                text_value,
                flags=re.IGNORECASE,
            ).strip()
        try:
            return json.loads(text_value)
        except (TypeError, json.JSONDecodeError):
            fenced_match = re.search(
                r"```(?:json)?\s*([\s\S]*?)\s*```",
                text_value,
                flags=re.IGNORECASE,
            )
            if fenced_match:
                try:
                    return json.loads(fenced_match.group(1).strip())
                except (TypeError, json.JSONDecodeError):
                    pass
            object_match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text_value)
            if object_match:
                try:
                    return json.loads(object_match.group(1).strip())
                except (TypeError, json.JSONDecodeError):
                    pass
            return value
    return value


def _iter_result_dicts(result):
    """按评分结果优先级遍历嵌套字典，兼容 MCP 自定义返回结构。"""
    normalized = _parse_result_json(result)
    visited = set()

    def walk(value):
        value = _parse_result_json(value)
        if isinstance(value, dict):
            marker = id(value)
            if marker in visited:
                return
            visited.add(marker)
            yield value
            preferred_keys = (
                "score_data",
                "result",
                "data",
                "output",
                "evaluation",
                "response",
                "content",
            )
            yielded = set()
            for key in preferred_keys:
                if key in value:
                    yielded.add(key)
                    yield from walk(value[key])
            for key, child in value.items():
                if key not in yielded:
                    yield from walk(child)
        elif isinstance(value, list):
            marker = id(value)
            if marker in visited:
                return
            visited.add(marker)
            for child in value:
                yield from walk(child)
        elif isinstance(value, str):
            parsed = _parse_result_json(value)
            if parsed is not value:
                yield from walk(parsed)

    yield from walk(normalized)


def _iter_result_texts(result):
    """Yield plain-text leaves so score-only MCP replies can be normalized too."""
    visited = set()

    def walk(value):
        value = _parse_result_json(value)
        if isinstance(value, dict):
            marker = id(value)
            if marker in visited:
                return
            visited.add(marker)
            for child in value.values():
                yield from walk(child)
        elif isinstance(value, list):
            marker = id(value)
            if marker in visited:
                return
            visited.add(marker)
            for child in value:
                yield from walk(child)
        elif isinstance(value, str):
            yield value

    yield from walk(result)


def _to_result_score(value):
    score = _to_decimal_or_none(value)
    if score is not None:
        return score
    if not isinstance(value, str):
        return None

    text_value = value.strip()
    fraction_match = re.search(
        r"^\s*(-?\d+(?:\.\d+)?)\s*/\s*-?\d+(?:\.\d+)?\s*$",
        text_value,
    )
    if fraction_match:
        return _to_decimal_or_none(fraction_match.group(1))
    score_match = re.search(r"-?\d+(?:\.\d+)?", text_value)
    return _to_decimal_or_none(score_match.group(0)) if score_match else None


def _extract_result_source(result):
    for source in _iter_result_dicts(result):
        if any(
            key in source
            for key in (
                "total_score",
                "score",
                "final_score",
                "obtained_score",
                "earned_score",
                "actual_score",
                "得分",
                "总分",
            )
        ):
            return source
    return next(_iter_result_dicts(result), {})


def _extract_result_score(result):
    # 小题提取失败的兜底结构会携带 raw_major_result（含大题总分），
    # 此时不得把大题总分当成该小题得分。
    if isinstance(result, dict) and result.get("extract_error"):
        return None
    score_keys = (
        "total_score",
        "score",
        "final_score",
        "obtained_score",
        "earned_score",
        "actual_score",
        "得分",
        "总分",
    )
    for source in _iter_result_dicts(result):
        for key in score_keys:
            score = _to_result_score(source.get(key))
            if score is not None:
                return score

    # Some legacy MCP services return only a text block (for example
    # "本题得分：8/10分") instead of a JSON score field. Preserve the raw result,
    # but still extract a score when the text labels it explicitly.
    labeled_score = re.compile(
        r"(?:total\s*score|final\s*score|earned\s*score|obtained\s*score|"
        r"最终得分|得分|总分|评分结果|评分)\s*(?:为|是|[:：=])?\s*"
        r"(-?\d+(?:\.\d+)?)(?:\s*/\s*-?\d+(?:\.\d+)?)?",
        flags=re.IGNORECASE,
    )
    for text_value in _iter_result_texts(result):
        match = labeled_score.search(text_value)
        if match:
            score = _to_decimal_or_none(match.group(1))
            if score is not None and score >= 0:
                return score
    return None


def _extract_result_comment(result):
    comment_keys = (
        "grading_desc",
        "grading_reason",
        "score_reason",
        "reasoning",
        "explanation",
        "comment",
        "comments",
        "remark",
        "feedback",
        "evaluation",
        "reason",
        "评价",
        "评语",
        "评分说明",
        "评分理由",
    )
    for source in _iter_result_dicts(result):
        for key in comment_keys:
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _set_formal_score_on_student_json(
    local_package: BizLocalExamPackage,
    *,
    student_no: str,
    question_no: str,
    score,
    comment: str | None,
):
    """把正式评分结果同步回持久化学生 JSON，供详情页和再次进入时使用。"""
    students = local_package.parsed_students
    if not isinstance(students, list):
        return

    normalized_question_no = str(question_no or "").strip().upper()
    if normalized_question_no.startswith("Q"):
        normalized_question_no = normalized_question_no[1:].strip()
    question_parts = normalized_question_no.split(".", 1)
    try:
        major_index = int(question_parts[0]) - 1
        sub_index = int(question_parts[1]) - 1 if len(question_parts) > 1 else None
    except (TypeError, ValueError):
        return
    if major_index < 0 or (sub_index is not None and sub_index < 0):
        return

    def student_number(student):
        if not isinstance(student, dict):
            return ""
        return str(
            student.get("id")
            or student.get("studentId")
            or student.get("student_no")
            or student.get("studentNo")
            or ""
        ).strip()

    target_student = next(
        (
            student for student in students
            if isinstance(student, dict) and student_number(student) == str(student_no).strip()
        ),
        None,
    )
    if not isinstance(target_student, dict):
        return
    result = target_student.get("result")
    if not isinstance(result, list) or major_index >= len(result):
        return
    major = result[major_index]
    if not isinstance(major, dict):
        return

    target = major
    sub_questions = major.get("sub_question")
    if sub_index is not None and isinstance(sub_questions, list) and sub_index < len(sub_questions):
        target = sub_questions[sub_index]
    elif sub_index is not None and isinstance(sub_questions, list):
        return
    if not isinstance(target, dict):
        return

    if score is not None:
        target["score"] = score
    if comment:
        target["comment"] = comment

    def numeric(value):
        try:
            number = float(value)
            return number if number == number else None
        except (TypeError, ValueError):
            return None

    total = 0.0
    has_score = False
    for item in result:
        if not isinstance(item, dict):
            continue
        children = item.get("sub_question")
        score_items = children if isinstance(children, list) and children else [item]
        for score_item in score_items:
            if not isinstance(score_item, dict):
                continue
            value = numeric(score_item.get("score"))
            if value is not None:
                total += value
                has_score = True
    if has_score:
        target_student["total_score"] = int(total) if total.is_integer() else round(total, 2)

    # 重新赋值而不是原地修改，确保 SQLAlchemy JSON 字段检测到变化。
    local_package.parsed_students = list(students)
    answer_index = local_package.answer_index
    if isinstance(answer_index, list):
        updated_index = [dict(item) if isinstance(item, dict) else item for item in answer_index]
        for item in updated_index:
            if isinstance(item, dict) and str(item.get("student_no") or "").strip() == str(student_no).strip():
                item["total_score"] = target_student.get("total_score", item.get("total_score"))
                break
        local_package.answer_index = updated_index


def _question_no_from_label(value):
    text = str(value or "").strip()
    if not text:
        return "paper"
    if text.upper().startswith("Q"):
        return text[1:].strip() or text
    return text


def _latest_standard_for_exam(db, subject_id, exam_id, user_id):
    return db.execute(
        select(BizExamGradingStandard)
        .where(
            BizExamGradingStandard.subject_id == subject_id,
            BizExamGradingStandard.exam_id == exam_id,
            BizExamGradingStandard.creator_id == int(user_id),
        )
        .order_by(BizExamGradingStandard.version_no.desc(), BizExamGradingStandard.standard_id.desc())
    ).scalars().first()


def _save_local_grading_record(db, *, user_id, subject_id, exam_id, local_package, payload, apply_student_json=True):
    question_no = str(payload.get("question_no") or "").strip()
    question_label = str(payload.get("question_label") or question_no or "").strip()
    if not question_no:
        question_no = _question_no_from_label(question_label)
    student_no = str(payload.get("student_no") or "").strip()
    if not student_no:
        raise ValueError("缺少学生编号")

    result = payload.get("result")
    standard_id = _to_int_or_none(payload.get("standard_id"))
    standard_version_no = _to_int_or_none(payload.get("standard_version_no"))
    if not standard_id or not standard_version_no:
        standard = _latest_standard_for_exam(db, subject_id, exam_id, user_id)
        if standard:
            standard_id = standard_id or standard.standard_id
            standard_version_no = standard_version_no or standard.version_no

    is_trial = bool(payload.get("is_trial", False))
    # 正式评阅与试阅一致：每次评阅都追加新记录，保留历史轮次供前端切换查看。
    existing = None

    record = existing or BizLocalGradingRecord(
        local_package_id=local_package.local_package_id,
        subject_id=subject_id,
        exam_id=exam_id,
        creator_id=int(user_id),
        package_fingerprint=local_package.package_fingerprint,
        student_no=student_no,
        question_no=question_no,
    )

    record.question_label = question_label or question_no
    record.grading_mode = str(payload.get("grading_mode") or "").strip() or None
    record.mcp_service_id = str(payload.get("mcp_service_id") or payload.get("service_uuid") or "").strip() or None
    record.mcp_service_name = str(payload.get("mcp_service_name") or "").strip() or None
    record.template_id = _to_int_or_none(payload.get("template_id"))
    record.template_name = str(payload.get("template_name") or "").strip() or None
    record.standard_label = str(payload.get("standard_label") or "").strip() or None
    record.standard_id = standard_id
    record.standard_version_no = standard_version_no
    record.question_version_no = _to_int_or_none(payload.get("question_version_no"))
    record.score = _to_decimal_or_none(payload.get("score"))
    if record.score is None:
        record.score = _extract_result_score(result)
    record.max_score = _to_decimal_or_none(payload.get("max_score"))
    # 长文本截断保护：单条记录超长会让 MySQL 报 DataError，
    # 并毒化整个事务导致同批所有评分记录一起回滚（界面显示成功但查无分数）
    def _clip_long_text(value, limit=4_000_000):
        text = str(value or "")
        return text[:limit] if len(text) > limit else (text or None)

    record.grading_desc = _clip_long_text(str(payload.get("grading_desc") or _extract_result_comment(result) or "").strip())
    record.question_text = _clip_long_text(str(payload.get("question_text") or "").strip())
    record.answer_text = _clip_long_text(str(payload.get("answer_text") or "").strip())
    record.image_name = str(payload.get("image_name") or "").strip() or None
    record.mcp_raw_json = result if isinstance(result, (dict, list)) else {"value": result}
    record.is_trial = is_trial

    if not existing:
        db.add(record)
    db.flush()
    # 批量评阅时（apply_student_json=False）跳过逐条写回：
    # 每条记录都整体重写 3MB+ 的 parsed_students JSON 列会产生
    # GB 级数据库写入，改为评完整批后一次性应用
    if apply_student_json and not is_trial:
        _set_formal_score_on_student_json(
            local_package,
            student_no=student_no,
            question_no=question_no,
            score=record.score,
            comment=record.grading_desc,
        )
    return record
async def list_subjects(request: Request):
    """获取当前教师创建的科目。"""
    user, error = _current_teacher(request)
    if error:
        return error

    keyword = (request.query_params.get("keyword") or "").strip()

    with get_db() as db:
        query = select(BizSubject).where(BizSubject.creator_id == int(user["user_id"]))
        if keyword:
            pattern = f"%{keyword}%"
            query = query.where(
                or_(
                    BizSubject.subject_no.like(pattern),
                    BizSubject.subject_name.like(pattern),
                )
            )
        query = query.order_by(BizSubject.created_at.desc(), BizSubject.subject_id.desc())
        subjects = db.execute(query).scalars().all()

    return success_response([_subject_to_dict(subject) for subject in subjects])


async def get_subject(request: Request):
    """获取当前教师创建的单个科目。"""
    user, error = _current_teacher(request)
    if error:
        return error

    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error

    with get_db() as db:
        subject = db.execute(
            select(BizSubject).where(
                BizSubject.subject_id == subject_id,
                BizSubject.creator_id == int(user["user_id"]),
            )
        ).scalar_one_or_none()

    if not subject:
        return error_response("科目不存在或无权访问", code=404, http_status_code=404)

    return success_response(_subject_to_dict(subject))


async def subject_detail(request: Request):
    if request.method == "GET":
        return await get_subject(request)
    if request.method == "PUT":
        return await update_subject(request)
    if request.method == "DELETE":
        return await delete_subject(request)

    return error_response("请求方法不支持", code=405, http_status_code=405)


async def create_subject(request: Request):
    """创建科目。"""
    user, error = _current_teacher(request)
    if error:
        return error

    data = await request.json()
    payload, payload_error = _parse_subject_payload(data)
    if payload_error:
        return payload_error

    try:
        with get_db() as db:
            existing = db.execute(
                select(BizSubject).where(
                    BizSubject.creator_id == int(user["user_id"]),
                    BizSubject.subject_no == payload["subject_no"],
                )
            ).scalar_one_or_none()
            if existing:
                return error_response("当前教师已创建相同科目号")

            subject = BizSubject(
                subject_no=payload["subject_no"],
                subject_name=payload["subject_name"],
                creator_id=int(user["user_id"]),
                created_at=payload["created_at"],
                remark=payload["remark"],
            )
            db.add(subject)
            db.commit()
            db.refresh(subject)
            result = _subject_to_dict(subject)
    except SQLAlchemyError as e:
        mcp_logger.error(f"创建科目数据库错误: {str(e)}")
        return error_response(
            f"创建科目数据库错误: {str(e)}",
            code=500,
            http_status_code=500,
        )

    return success_response(result, "科目创建成功", code=0)


async def update_subject(request: Request):
    """修改当前教师创建的科目。"""
    user, error = _current_teacher(request)
    if error:
        return error

    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error
    data = await request.json()
    payload, payload_error = _parse_subject_payload(data)
    if payload_error:
        return payload_error

    try:
        with get_db() as db:
            subject = db.execute(
                select(BizSubject).where(
                    BizSubject.subject_id == subject_id,
                    BizSubject.creator_id == int(user["user_id"]),
                )
            ).scalar_one_or_none()
            if not subject:
                return error_response("科目不存在或无权修改", code=404, http_status_code=404)

            existing = db.execute(
                select(BizSubject).where(
                    BizSubject.creator_id == int(user["user_id"]),
                    BizSubject.subject_no == payload["subject_no"],
                    BizSubject.subject_id != subject_id,
                )
            ).scalar_one_or_none()
            if existing:
                return error_response("当前教师已创建相同科目号")

            subject.subject_no = payload["subject_no"]
            subject.subject_name = payload["subject_name"]
            subject.created_at = payload["created_at"]
            subject.remark = payload["remark"]
            db.commit()
            db.refresh(subject)
            result = _subject_to_dict(subject)
    except SQLAlchemyError as e:
        mcp_logger.error(f"修改科目数据库错误: {str(e)}")
        return error_response(
            f"修改科目数据库错误: {str(e)}",
            code=500,
            http_status_code=500,
        )

    return success_response(result, "科目修改成功", code=0)


async def delete_subject(request: Request):
    """删除当前教师创建的科目。"""
    user, error = _current_teacher(request)
    if error:
        return error

    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error

    try:
        with get_db() as db:
            subject = db.execute(
                select(BizSubject).where(
                    BizSubject.subject_id == subject_id,
                    BizSubject.creator_id == int(user["user_id"]),
                )
            ).scalar_one_or_none()
            if not subject:
                return error_response("科目不存在或无权删除", code=404, http_status_code=404)

            local_package_count = db.execute(
                select(func.count(BizLocalExamPackage.local_package_id)).where(
                    BizLocalExamPackage.subject_id == subject_id,
                    BizLocalExamPackage.creator_id == int(user["user_id"]),
                )
            ).scalar() or 0
            package_count = db.execute(
                select(func.count(BizExamPackage.package_id))
                .select_from(BizExamPackage)
                .join(BizExam, BizExamPackage.exam_id == BizExam.id)
                .where(
                    BizExam.subject_id == subject_id,
                    BizExam.creator_id == int(user["user_id"]),
                )
            ).scalar() or 0
            grading_standard_count = db.execute(
                select(func.count(BizExamGradingStandard.standard_id)).where(
                    BizExamGradingStandard.subject_id == subject_id,
                    BizExamGradingStandard.creator_id == int(user["user_id"]),
                )
            ).scalar() or 0
            grading_template_count = db.execute(
                select(func.count(BizExamGradingTemplate.template_id)).where(
                    BizExamGradingTemplate.subject_id == subject_id,
                    BizExamGradingTemplate.creator_id == int(user["user_id"]),
                )
            ).scalar() or 0
            local_grading_record_count = db.execute(
                select(func.count(BizLocalGradingRecord.record_id)).where(
                    BizLocalGradingRecord.subject_id == subject_id,
                    BizLocalGradingRecord.creator_id == int(user["user_id"]),
                )
            ).scalar() or 0
            if (
                local_package_count > 0
                or package_count > 0
                or grading_standard_count > 0
                or grading_template_count > 0
                or local_grading_record_count > 0
            ):
                return error_response(
                    "当前科目已关联试卷包，不能删除。请先删除该科目下的试卷包。",
                    code=400,
                    http_status_code=400,
                )

            exam_count = db.execute(
                select(func.count(BizExam.id)).where(
                    BizExam.subject_id == subject_id,
                    BizExam.creator_id == int(user["user_id"]),
                )
            ).scalar() or 0
            if exam_count > 0:
                return error_response(
                    "当前科目已关联考试，不能删除。请先删除该科目下的考试。",
                    code=400,
                    http_status_code=400,
                )

            db.delete(subject)
            db.commit()
    except IntegrityError as e:
        mcp_logger.error(f"删除科目存在关联数据: {str(e)}")
        return error_response(
            "当前科目仍有关联数据，不能删除。请先删除关联考试或试卷包。",
            code=400,
            http_status_code=400,
        )
    except SQLAlchemyError as e:
        mcp_logger.error(f"删除科目数据库错误: {str(e)}")
        return error_response(
            f"删除科目数据库错误: {str(e)}",
            code=500,
            http_status_code=500,
        )

    return success_response(None, "科目删除成功", code=0)


async def list_exams(request: Request):
    """获取当前教师在指定科目下创建的考试。"""
    user, error = _current_teacher(request)
    if error:
        return error

    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error
    keyword = (request.query_params.get("keyword") or "").strip()

    with get_db() as db:
        subject = db.execute(
            select(BizSubject).where(
                BizSubject.subject_id == subject_id,
                BizSubject.creator_id == int(user["user_id"]),
            )
        ).scalar_one_or_none()
        if not subject:
            return error_response("科目不存在或无权访问", code=404, http_status_code=404)

        query = select(BizExam).where(
            BizExam.subject_id == subject_id,
            BizExam.creator_id == int(user["user_id"]),
        )
        if keyword:
            query = query.where(BizExam.exam_name.like(f"%{keyword}%"))
        query = query.order_by(BizExam.exam_time.desc(), BizExam.id.desc())
        exams = db.execute(query).scalars().all()

    return success_response([_exam_to_dict(exam) for exam in exams])


async def list_all_exams(request: Request):
    """获取当前教师创建的全部考试，并附带所属科目信息。"""
    user, error = _current_teacher(request)
    if error:
        return error

    keyword = (request.query_params.get("keyword") or "").strip()
    with get_db() as db:
        query = (
            select(BizExam, BizSubject.subject_no, BizSubject.subject_name)
            .join(BizSubject, BizSubject.subject_id == BizExam.subject_id)
            .where(
                BizExam.creator_id == int(user["user_id"]),
                BizSubject.creator_id == int(user["user_id"]),
            )
        )
        if keyword:
            query = query.where(BizExam.exam_name.like(f"%{keyword}%"))
        query = query.order_by(BizExam.exam_time.desc(), BizExam.id.desc())
        rows = db.execute(query).all()

    exams = []
    for exam, subject_no, subject_name in rows:
        item = _exam_to_dict(exam)
        item["subject_no"] = subject_no
        item["subject_name"] = subject_name
        exams.append(item)
    return success_response(exams)


async def create_exam(request: Request):
    """在指定科目下创建考试。"""
    user, error = _current_teacher(request)
    if error:
        return error

    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error
    form = await request.form()
    exam_name = (form.get("exam_name") or "").strip()
    exam_time, time_error = _parse_exam_time(form.get("exam_time") or "")
    blank_paper = form.get("blank_paper")
    answer_key = form.get("answer_key")

    if not exam_name:
        return error_response("考试名称不能为空")
    if time_error:
        return time_error

    try:
        with get_db() as db:
            subject = db.execute(
                select(BizSubject).where(
                    BizSubject.subject_id == subject_id,
                    BizSubject.creator_id == int(user["user_id"]),
                )
            ).scalar_one_or_none()
            if not subject:
                return error_response("科目不存在或无权访问", code=404, http_status_code=404)

            blank_paper_path = await _save_exam_file(
                blank_paper,
                user["user_id"],
                subject_id,
                "blank_paper",
            )
            answer_key_path = await _save_exam_file(
                answer_key,
                user["user_id"],
                subject_id,
                "answer_key",
            )

            exam = BizExam(
                subject_id=subject_id,
                exam_name=exam_name,
                exam_time=exam_time,
                answer_key_path=answer_key_path,
                blank_paper_path=blank_paper_path,
                creator_id=int(user["user_id"]),
            )
            db.add(exam)
            db.commit()
            db.refresh(exam)
            result = _exam_to_dict(exam)
    except SQLAlchemyError as e:
        mcp_logger.error(f"创建考试数据库错误: {str(e)}")
        return error_response(
            f"创建考试数据库错误: {str(e)}",
            code=500,
            http_status_code=500,
        )

    return success_response(result, "考试创建成功", code=0)


async def get_exam(request: Request):
    """获取当前教师创建的单个考试。"""
    user, error = _current_teacher(request)
    if error:
        return error

    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error
    exam_id, exam_id_error = _get_exam_id(request)
    if exam_id_error:
        return exam_id_error

    with get_db() as db:
        exam = await _get_owned_exam(db, user["user_id"], subject_id, exam_id)

    if not exam:
        return error_response("考试不存在或无权访问", code=404, http_status_code=404)

    return success_response(_exam_to_dict(exam))


async def update_exam(request: Request):
    """修改当前教师创建的考试。"""
    user, error = _current_teacher(request)
    if error:
        return error

    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error
    exam_id, exam_id_error = _get_exam_id(request)
    if exam_id_error:
        return exam_id_error

    form = await request.form()
    exam_name = (form.get("exam_name") or "").strip()
    exam_time, time_error = _parse_exam_time(form.get("exam_time") or "")
    blank_paper = form.get("blank_paper")
    answer_key = form.get("answer_key")

    if not exam_name:
        return error_response("考试名称不能为空")
    if time_error:
        return time_error

    try:
        with get_db() as db:
            exam = await _get_owned_exam(db, user["user_id"], subject_id, exam_id)
            if not exam:
                return error_response("考试不存在或无权修改", code=404, http_status_code=404)

            blank_paper_path = await _save_exam_file(
                blank_paper,
                user["user_id"],
                subject_id,
                "blank_paper",
            )
            answer_key_path = await _save_exam_file(
                answer_key,
                user["user_id"],
                subject_id,
                "answer_key",
            )

            exam.exam_name = exam_name
            exam.exam_time = exam_time
            if blank_paper_path:
                exam.blank_paper_path = blank_paper_path
            if answer_key_path:
                exam.answer_key_path = answer_key_path

            db.commit()
            db.refresh(exam)
            result = _exam_to_dict(exam)
    except SQLAlchemyError as e:
        mcp_logger.error(f"修改考试数据库错误: {str(e)}")
        return error_response(
            f"修改考试数据库错误: {str(e)}",
            code=500,
            http_status_code=500,
        )

    return success_response(result, "考试修改成功", code=0)


async def delete_exam(request: Request):
    """删除当前教师创建的考试。"""
    user, error = _current_teacher(request)
    if error:
        return error

    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error
    exam_id, exam_id_error = _get_exam_id(request)
    if exam_id_error:
        return exam_id_error

    try:
        with get_db() as db:
            exam = await _get_owned_exam(db, user["user_id"], subject_id, exam_id)
            if not exam:
                return error_response("考试不存在或无权删除", code=404, http_status_code=404)

            answer_count = db.execute(
                select(func.count(BizAnswerSheet.answer_id)).where(
                    BizAnswerSheet.exam_id == exam_id,
                )
            ).scalar() or 0
            answer_file_count = db.execute(
                select(func.count(BizAnswerFile.id)).where(
                    BizAnswerFile.exam_id == exam_id,
                )
            ).scalar() or 0
            if answer_count > 0 or answer_file_count > 0:
                return error_response(
                    "当前考试已关联答卷，不能删除。请先删除该考试下的试卷包或答卷。",
                    code=400,
                    http_status_code=400,
                )

            package_count = db.execute(
                select(func.count(BizExamPackage.package_id)).where(
                    BizExamPackage.exam_id == exam_id,
                )
            ).scalar() or 0
            local_package_count = db.execute(
                select(func.count(BizLocalExamPackage.local_package_id)).where(
                    BizLocalExamPackage.subject_id == subject_id,
                    BizLocalExamPackage.exam_id == exam_id,
                    BizLocalExamPackage.creator_id == int(user["user_id"]),
                )
            ).scalar() or 0
            if package_count > 0 or local_package_count > 0:
                return error_response(
                    "当前考试已关联试卷包，不能删除。请先删除该考试下的试卷包。",
                    code=400,
                    http_status_code=400,
                )

            grading_standard_count = db.execute(
                select(func.count(BizExamGradingStandard.standard_id)).where(
                    BizExamGradingStandard.subject_id == subject_id,
                    BizExamGradingStandard.exam_id == exam_id,
                    BizExamGradingStandard.creator_id == int(user["user_id"]),
                )
            ).scalar() or 0
            local_grading_record_count = db.execute(
                select(func.count(BizLocalGradingRecord.record_id)).where(
                    BizLocalGradingRecord.subject_id == subject_id,
                    BizLocalGradingRecord.exam_id == exam_id,
                    BizLocalGradingRecord.creator_id == int(user["user_id"]),
                )
            ).scalar() or 0
            grading_template_count = db.execute(
                select(func.count(BizExamGradingTemplate.template_id)).where(
                    BizExamGradingTemplate.subject_id == subject_id,
                    BizExamGradingTemplate.exam_id == exam_id,
                    BizExamGradingTemplate.creator_id == int(user["user_id"]),
                )
            ).scalar() or 0
            if grading_standard_count > 0 or local_grading_record_count > 0 or grading_template_count > 0:
                return error_response(
                    "当前考试已关联评分数据，不能删除。请先删除该考试下的试卷包或评分记录。",
                    code=400,
                    http_status_code=400,
                )

            db.delete(exam)
            db.commit()
    except IntegrityError as e:
        mcp_logger.error(f"删除考试存在关联数据: {str(e)}")
        return error_response(
            "当前考试仍有关联数据，不能删除。请先删除关联答卷、试卷包或评分数据。",
            code=400,
            http_status_code=400,
        )
    except SQLAlchemyError as e:
        mcp_logger.error(f"删除考试数据库错误: {str(e)}")
        return error_response(
            f"删除考试数据库错误: {str(e)}",
            code=500,
            http_status_code=500,
        )

    return success_response(None, "考试删除成功", code=0)


async def exam_detail(request: Request):
    if request.method == "GET":
        return await get_exam(request)
    if request.method == "PUT":
        return await update_exam(request)
    if request.method == "DELETE":
        return await delete_exam(request)

    return error_response("请求方法不支持", code=405, http_status_code=405)


async def get_latest_grading_standard(request: Request):
    """获取当前考试最新阅卷标准版本。"""
    user, error = _current_teacher(request)
    if error:
        return error

    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error
    exam_id, exam_id_error = _get_exam_id(request)
    if exam_id_error:
        return exam_id_error

    with get_db() as db:
        exam = await _get_owned_exam(db, user["user_id"], subject_id, exam_id)
        if not exam:
            return error_response("考试不存在或无权访问", code=404, http_status_code=404)

        standard = db.execute(
            select(BizExamGradingStandard)
            .where(
                BizExamGradingStandard.subject_id == subject_id,
                BizExamGradingStandard.exam_id == exam_id,
                BizExamGradingStandard.creator_id == int(user["user_id"]),
            )
            .order_by(BizExamGradingStandard.version_no.desc(), BizExamGradingStandard.standard_id.desc())
            .limit(1)
        ).scalars().first()
        if standard:
            _resolve_standards_for_response(db, [standard])

    return _success_response_with_data(_grading_standard_to_dict(standard) if standard else None)


async def create_grading_standard(request: Request):
    """Create a new exam grading standard snapshot."""
    user, error = _current_teacher(request)
    if error:
        return error

    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error
    exam_id, exam_id_error = _get_exam_id(request)
    if exam_id_error:
        return exam_id_error

    try:
        data = await request.json()
    except Exception:
        return error_response("invalid grading standard payload", code=400, http_status_code=400)

    standard_items = data.get("standard_items")
    remark = (data.get("remark") or "").strip() or None
    if not isinstance(standard_items, list) or not standard_items:
        return error_response("at least one grading standard item is required", code=400, http_status_code=400)

    normalized_items = []
    try:
        for index, item in enumerate(standard_items, start=1):
            normalized_items.append(_normalize_standard_item_payload(item, index))
    except ValueError as exc:
        return error_response(str(exc), code=400, http_status_code=400)

    with get_db() as db:
        exam = await _get_owned_exam(db, user["user_id"], subject_id, exam_id)
        if not exam:
            return error_response("exam not found", code=404, http_status_code=404)

        existing_standards = db.execute(
            select(BizExamGradingStandard).where(
                BizExamGradingStandard.subject_id == subject_id,
                BizExamGradingStandard.exam_id == exam_id,
                BizExamGradingStandard.creator_id == int(user["user_id"]),
            )
        ).scalars().all()
        _migrate_standard_items_if_needed(db, existing_standards)

        # 若内容与最新版本完全一致，则不再生成重复版本，直接沿用最新版本
        latest_existing = max(
            existing_standards,
            key=lambda standard: (int(standard.version_no or 0), int(standard.standard_id or 0)),
            default=None,
        )
        if latest_existing is not None and (remark or None) == (latest_existing.remark or None):
            _resolve_all_standard_items_for_versions(db, [latest_existing], subject_id, exam_id, user["user_id"])
            latest_items = getattr(latest_existing, "_resolved_standard_items", None) or []
            if _standard_items_equivalent(latest_items, normalized_items):
                return success_response(_grading_standard_to_dict(latest_existing), message="内容无变化，沿用当前版本")

        max_version = max([int(standard.version_no or 0) for standard in existing_standards] or [0])
        next_version_no = int(max_version) + 1
        for item in normalized_items:
            if not item.get("source_standard_version_no"):
                item["source_standard_version_no"] = next_version_no

        standard = BizExamGradingStandard(
            subject_id=subject_id,
            exam_id=exam_id,
            creator_id=int(user["user_id"]),
            version_no=next_version_no,
            standard_items=[],
            remark=remark,
        )
        db.add(standard)
        db.flush()

        resolved_items = []
        summary_items = []
        for item in normalized_items:
            existing_item = None
            item_id = item.get("item_id")
            if item_id:
                existing_item = db.execute(
                    select(BizExamGradingStandardItem).where(
                        BizExamGradingStandardItem.item_id == item_id,
                        BizExamGradingStandardItem.subject_id == subject_id,
                        BizExamGradingStandardItem.exam_id == exam_id,
                        BizExamGradingStandardItem.creator_id == int(user["user_id"]),
                        BizExamGradingStandardItem.question_no == item["question_no"],
                        BizExamGradingStandardItem.question_version_no == item["question_version_no"],
                    )
                ).scalar_one_or_none()
            if not existing_item:
                existing_item = db.execute(
                    select(BizExamGradingStandardItem)
                    .where(
                        BizExamGradingStandardItem.subject_id == subject_id,
                        BizExamGradingStandardItem.exam_id == exam_id,
                        BizExamGradingStandardItem.creator_id == int(user["user_id"]),
                        BizExamGradingStandardItem.question_no == item["question_no"],
                        BizExamGradingStandardItem.question_version_no == item["question_version_no"],
                    )
                    .order_by(BizExamGradingStandardItem.item_id.desc())
                ).scalar_one_or_none()

            if existing_item and not _standard_item_matches_payload(existing_item, item):
                existing_item = None

            if existing_item:
                existing_item.max_score = item.get("max_score")
                existing_item.remark = item.get("remark")
                item_dict = _grading_standard_item_to_dict(existing_item)
            else:
                existing_item = BizExamGradingStandardItem(
                    standard_id=standard.standard_id,
                    subject_id=subject_id,
                    exam_id=exam_id,
                    creator_id=int(user["user_id"]),
                    question_no=item["question_no"],
                    question_type=item["question_type"],
                    mcp_service_id=item["mcp_service_id"],
                    mcp_service_name=item["mcp_service_name"],
                    question_text=item["question_text"],
                    reference_answer=item["reference_answer"],
                    grading_criteria=item["grading_criteria"],
                    question_version_no=item["question_version_no"],
                    source_standard_version_no=item["source_standard_version_no"],
                    mcp_params=item["mcp_params"],
                    max_score=item.get("max_score"),
                    remark=item.get("remark"),
                )
                db.add(existing_item)
                db.flush()
                item_dict = _grading_standard_item_to_dict(existing_item)

            item_dict["source_standard_version_no"] = item_dict.get("source_standard_version_no") or item["source_standard_version_no"]
            resolved_items.append(item_dict)
            summary_items.append({
                "question_no": item_dict["question_no"],
                "item_id": item_dict["item_id"],
                "question_version_no": item_dict["question_version_no"],
                "source_standard_version_no": item_dict.get("source_standard_version_no"),
            })

        standard.standard_items = summary_items
        standard._resolved_standard_items = resolved_items
        db.commit()
        db.refresh(standard)
        standard._resolved_standard_items = resolved_items
        result = _grading_standard_to_dict(standard)

    return success_response(result, message="grading standard saved")

async def list_grading_standard_versions(request: Request):
    """获取考试下所有阅卷标准版本，用于组装阅卷模板。"""
    user, error = _current_teacher(request)
    if error:
        return error

    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error
    exam_id, exam_id_error = _get_exam_id(request)
    if exam_id_error:
        return exam_id_error

    with get_db() as db:
        exam = await _get_owned_exam(db, user["user_id"], subject_id, exam_id)
        if not exam:
            return error_response("考试不存在或无权访问", code=404, http_status_code=404)

        standards = db.execute(
            select(BizExamGradingStandard)
            .where(
                BizExamGradingStandard.subject_id == subject_id,
                BizExamGradingStandard.exam_id == exam_id,
                BizExamGradingStandard.creator_id == int(user["user_id"]),
            )
            .order_by(BizExamGradingStandard.version_no.desc(), BizExamGradingStandard.standard_id.desc())
        ).scalars().all()
        _resolve_all_standard_items_for_versions(db, standards, subject_id, exam_id, user["user_id"])

    return success_response([_grading_standard_to_dict(standard) for standard in standards])


async def update_grading_standard(request: Request):
    """Update one complete grading-standard version without creating a new version."""
    user, error = _current_teacher(request)
    if error:
        return error
    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error
    exam_id, exam_id_error = _get_exam_id(request)
    if exam_id_error:
        return exam_id_error
    try:
        standard_id = int(request.path_params["standard_id"])
        data = await request.json()
    except (KeyError, TypeError, ValueError):
        return error_response("阅卷标准ID不正确", code=400, http_status_code=400)
    except Exception:
        return error_response("阅卷标准内容格式不正确", code=400, http_status_code=400)

    standard_items = data.get("standard_items")
    if not isinstance(standard_items, list) or not standard_items:
        return error_response("至少需要一道阅卷标准题目", code=400, http_status_code=400)
    try:
        normalized_items = [_normalize_standard_item_payload(item, index) for index, item in enumerate(standard_items, 1)]
    except ValueError as exc:
        return error_response(str(exc), code=400, http_status_code=400)

    with get_db() as db:
        exam = await _get_owned_exam(db, user["user_id"], subject_id, exam_id)
        if not exam:
            return error_response("考试不存在或无权访问", code=404, http_status_code=404)
        standard = db.execute(select(BizExamGradingStandard).where(
            BizExamGradingStandard.standard_id == standard_id,
            BizExamGradingStandard.subject_id == subject_id,
            BizExamGradingStandard.exam_id == exam_id,
            BizExamGradingStandard.creator_id == int(user["user_id"]),
        )).scalar_one_or_none()
        if not standard:
            return error_response("阅卷标准不存在或无权修改", code=404, http_status_code=404)

        current_items = db.execute(select(BizExamGradingStandardItem).where(
            BizExamGradingStandardItem.standard_id == standard_id,
            BizExamGradingStandardItem.subject_id == subject_id,
            BizExamGradingStandardItem.exam_id == exam_id,
            BizExamGradingStandardItem.creator_id == int(user["user_id"]),
        )).scalars().all()
        current_by_id = {item.item_id: item for item in current_items}
        current_by_question = {item.question_no: item for item in current_items}
        keep_ids = set()
        summaries = []
        resolved = []
        for item in normalized_items:
            row = current_by_id.get(item.get("item_id")) or current_by_question.get(item["question_no"])
            if row:
                row.question_no = item["question_no"]
                row.question_type = item["question_type"]
                row.mcp_service_id = item["mcp_service_id"]
                row.mcp_service_name = item["mcp_service_name"]
                row.question_text = item["question_text"]
                row.reference_answer = item["reference_answer"]
                row.grading_criteria = item["grading_criteria"]
                row.question_version_no = item["question_version_no"]
                row.source_standard_version_no = standard.version_no
                row.mcp_params = item["mcp_params"]
                row.max_score = item["max_score"]
                row.remark = item["remark"]
            else:
                row = BizExamGradingStandardItem(
                    standard_id=standard_id, subject_id=subject_id, exam_id=exam_id,
                    creator_id=int(user["user_id"]), question_no=item["question_no"],
                    question_type=item["question_type"], mcp_service_id=item["mcp_service_id"],
                    mcp_service_name=item["mcp_service_name"], question_text=item["question_text"],
                    reference_answer=item["reference_answer"], grading_criteria=item["grading_criteria"],
                    question_version_no=item["question_version_no"], source_standard_version_no=standard.version_no,
                    mcp_params=item["mcp_params"], max_score=item["max_score"], remark=item["remark"],
                )
                db.add(row)
                db.flush()
            keep_ids.add(row.item_id)
            item_data = _grading_standard_item_to_dict(row)
            summaries.append({"question_no": row.question_no, "item_id": row.item_id,
                              "question_version_no": row.question_version_no,
                              "source_standard_version_no": row.source_standard_version_no})
            resolved.append(item_data)
        for item in current_items:
            if item.item_id not in keep_ids:
                db.delete(item)
        standard.standard_items = summaries
        standard._resolved_standard_items = resolved
        db.commit()
        db.refresh(standard)
        standard._resolved_standard_items = resolved
        result = _grading_standard_to_dict(standard)
    return success_response(result, message="阅卷标准已更新", code=0)


async def delete_grading_standard(request: Request):
    """Delete one complete standard version when no grading data depends on it."""
    user, error = _current_teacher(request)
    if error:
        return error
    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error
    exam_id, exam_id_error = _get_exam_id(request)
    if exam_id_error:
        return exam_id_error
    try:
        standard_id = int(request.path_params["standard_id"])
    except (KeyError, TypeError, ValueError):
        return error_response("阅卷标准ID不正确", code=400, http_status_code=400)

    with get_db() as db:
        exam = await _get_owned_exam(db, user["user_id"], subject_id, exam_id)
        if not exam:
            return error_response("考试不存在或无权访问", code=404, http_status_code=404)
        standard = db.execute(select(BizExamGradingStandard).where(
            BizExamGradingStandard.standard_id == standard_id,
            BizExamGradingStandard.subject_id == subject_id,
            BizExamGradingStandard.exam_id == exam_id,
            BizExamGradingStandard.creator_id == int(user["user_id"]),
        )).scalar_one_or_none()
        if not standard:
            return error_response("阅卷标准不存在或无权删除", code=404, http_status_code=404)
        if db.execute(select(BizLocalGradingRecord.record_id).where(
            BizLocalGradingRecord.standard_id == standard_id
        ).limit(1)).first():
            return error_response("该标准版本已有阅卷记录，不能删除", code=409, http_status_code=409)
        templates = db.execute(select(BizExamGradingTemplate).where(
            BizExamGradingTemplate.subject_id == subject_id,
            BizExamGradingTemplate.exam_id == exam_id,
            BizExamGradingTemplate.creator_id == int(user["user_id"]),
        )).scalars().all()
        if any(any(int(item.get("standard_id") or 0) == standard_id for item in _ensure_list_payload(template.template_items) if isinstance(item, dict)) for template in templates):
            return error_response("该标准版本已被阅卷模板引用，请先删除相关模板", code=409, http_status_code=409)

        item_ids = {int(item.get("item_id")) for item in _ensure_list_payload(standard.standard_items)
                    if isinstance(item, dict) and item.get("item_id")}
        other_standards = db.execute(select(BizExamGradingStandard).where(
            BizExamGradingStandard.subject_id == subject_id,
            BizExamGradingStandard.exam_id == exam_id,
            BizExamGradingStandard.creator_id == int(user["user_id"]),
            BizExamGradingStandard.standard_id != standard_id,
        )).scalars().all()
        if any(any(int(item.get("item_id") or 0) in item_ids for item in _ensure_list_payload(other.standard_items) if isinstance(item, dict)) for other in other_standards):
            return error_response("该标准题目已被其他版本复用，不能删除此版本", code=409, http_status_code=409)

        owned_items = db.execute(select(BizExamGradingStandardItem).where(
            BizExamGradingStandardItem.standard_id == standard_id,
            BizExamGradingStandardItem.subject_id == subject_id,
            BizExamGradingStandardItem.exam_id == exam_id,
            BizExamGradingStandardItem.creator_id == int(user["user_id"]),
        )).scalars().all()
        for item in owned_items:
            db.delete(item)
        db.delete(standard)
        db.commit()
    return success_response({"standard_id": standard_id}, message="阅卷标准已删除", code=0)


async def list_grading_templates(request: Request):
    """获取当前考试已组装的阅卷模板。"""
    user, error = _current_teacher(request)
    if error:
        return error

    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error
    exam_id, exam_id_error = _get_exam_id(request)
    if exam_id_error:
        return exam_id_error

    with get_db() as db:
        exam = await _get_owned_exam(db, user["user_id"], subject_id, exam_id)
        if not exam:
            return error_response("考试不存在或无权访问", code=404, http_status_code=404)

        templates = db.execute(
            select(BizExamGradingTemplate)
            .where(
                BizExamGradingTemplate.subject_id == subject_id,
                BizExamGradingTemplate.exam_id == exam_id,
                BizExamGradingTemplate.creator_id == int(user["user_id"]),
            )
            .order_by(BizExamGradingTemplate.created_at.desc(), BizExamGradingTemplate.template_id.desc())
        ).scalars().all()
        standards = db.execute(
            select(BizExamGradingStandard)
            .where(
                BizExamGradingStandard.subject_id == subject_id,
                BizExamGradingStandard.exam_id == exam_id,
                BizExamGradingStandard.creator_id == int(user["user_id"]),
            )
        ).scalars().all()
        _resolve_all_standard_items_for_versions(db, standards, subject_id, exam_id, user["user_id"])

    return success_response(_fill_template_question_versions(templates, standards))


def _standard_item_key(item):
    return str(item.get("question_no") or "").strip()


def _standard_items_for_standard(standard):
    items = getattr(standard, "_resolved_standard_items", None)
    if items is None:
        items = standard.standard_items
    return _ensure_list_payload(items)


def _build_standard_item_index(standards):
    index = {}
    parent_candidates = {}
    for standard in standards:
        for item in _standard_items_for_standard(standard):
            if not isinstance(item, dict):
                continue
            question_no = _standard_item_key(item)
            if not question_no:
                continue
            standard_id = standard.standard_id
            index[(standard_id, question_no)] = item
            parent_question_no = _parent_question_no(question_no)
            if parent_question_no and parent_question_no != question_no:
                parent_candidates.setdefault((standard_id, parent_question_no), []).append(item)

    for key, candidates in parent_candidates.items():
        unique_candidates = {
            _to_int_or_none(candidate.get("item_id")) or _standard_item_key(candidate)
            for candidate in candidates
        }
        if key not in index and len(unique_candidates) == 1:
            index[key] = candidates[0]
    return index


def _standard_item_version_signature(item):
    if not isinstance(item, dict):
        return ""
    payload = {
        "question_type": str(item.get("question_type") or "").strip(),
        "mcp_service_id": str(item.get("mcp_service_id") or "").strip(),
        "mcp_service_name": str(item.get("mcp_service_name") or "").strip(),
        "question_text": str(item.get("question_text") or "").strip(),
        "reference_answer": str(item.get("reference_answer") or "").strip(),
        "grading_criteria": str(item.get("grading_criteria") or "").strip(),
        "mcp_params": item.get("mcp_params") if isinstance(item.get("mcp_params"), dict) else {},
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _build_question_version_lookup(standards):
    lookup = {}
    parent_candidates = {}
    version_state = {}
    sorted_standards = sorted(
        standards,
        key=lambda standard: (int(standard.version_no or 0), int(standard.standard_id or 0)),
    )
    for standard in sorted_standards:
        for item in _standard_items_for_standard(standard):
            if not isinstance(item, dict):
                continue
            question_no = _standard_item_key(item)
            if not question_no:
                continue
            signature = _standard_item_version_signature(item)
            current_state = version_state.get(question_no)
            if not current_state:
                version_no = 1
            elif current_state["signature"] == signature:
                version_no = current_state["version_no"]
            else:
                version_no = current_state["version_no"] + 1
            version_state[question_no] = {"signature": signature, "version_no": version_no}
            lookup[(standard.standard_id, question_no)] = version_no

            parent_question_no = _parent_question_no(question_no)
            if parent_question_no and parent_question_no != question_no:
                parent_candidates.setdefault((standard.standard_id, parent_question_no), set()).add(version_no)

    for key, versions in parent_candidates.items():
        if key not in lookup and len(versions) == 1:
            lookup[key] = next(iter(versions))
    return lookup


def _apply_question_version_lookup(standards, version_lookup):
    for standard in standards:
        standard_id = standard.standard_id
        if not standard_id:
            continue
        resolved_items = getattr(standard, "_resolved_standard_items", None)
        if isinstance(resolved_items, list):
            for item in resolved_items:
                if not isinstance(item, dict):
                    continue
                question_no = _standard_item_key(item)
                if not question_no:
                    continue
                version_no = version_lookup.get((standard_id, question_no))
                if version_no:
                    item["question_version_no"] = version_no
        summary_items = standard.standard_items if isinstance(standard.standard_items, list) else []
        for item in summary_items:
            if not isinstance(item, dict):
                continue
            question_no = _standard_item_key(item)
            if not question_no:
                continue
            version_no = version_lookup.get((standard_id, question_no))
            if version_no:
                item["question_version_no"] = version_no


def _merge_standard_item_payload(target, source):
    if not isinstance(target, dict) or not isinstance(source, dict):
        return target
    for key, value in source.items():
        if target.get(key) in (None, "", []):
            target[key] = value
    return target


def _fill_template_question_versions(templates, standards):
    version_lookup = _build_question_version_lookup(standards)
    _apply_question_version_lookup(standards, version_lookup)
    standard_item_index = _build_standard_item_index(standards)
    standard_version_lookup = {
        standard.standard_id: standard.version_no
        for standard in standards
        if standard.standard_id
    }
    results = []
    for template in templates:
        data = _grading_template_to_dict(template)
        for item in data.get("template_items") or []:
            if not isinstance(item, dict):
                continue
            question_no = _standard_item_key(item)
            standard_id = _to_int_or_none(item.get("standard_id"))
            standard_item = item.get("standard_item") if isinstance(item.get("standard_item"), dict) else {}
            resolved_standard_item = None
            if standard_id and question_no:
                resolved_standard_item = standard_item_index.get((standard_id, question_no))
            if resolved_standard_item:
                if standard_item:
                    item["standard_item"] = _merge_standard_item_payload(standard_item, resolved_standard_item)
                else:
                    item["standard_item"] = dict(resolved_standard_item)
                standard_item = item["standard_item"]

            if not _to_int_or_none(item.get("standard_version_no")) and standard_id in standard_version_lookup:
                item["standard_version_no"] = standard_version_lookup[standard_id]

            question_version_no = _to_int_or_none(item.get("question_version_no"))
            if not question_version_no:
                question_version_no = _to_int_or_none(standard_item.get("question_version_no"))
            if not question_version_no and standard_id and question_no:
                question_version_no = version_lookup.get((standard_id, question_no))
            if question_version_no:
                item["question_version_no"] = question_version_no
                if isinstance(standard_item, dict) and not _to_int_or_none(standard_item.get("question_version_no")):
                    standard_item["question_version_no"] = question_version_no
        results.append(data)
    return results

async def create_grading_template(request: Request):
    """选择各题阅卷标准版本并组装成考试阅卷模板。"""
    user, error = _current_teacher(request)
    if error:
        return error

    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error
    exam_id, exam_id_error = _get_exam_id(request)
    if exam_id_error:
        return exam_id_error

    try:
        data = await request.json()
    except Exception:
        return error_response("阅卷模板数据格式不正确", code=400, http_status_code=400)

    template_name = str(data.get("template_name") or "科目考试阅卷模板").strip() or "科目考试阅卷模板"
    description = str(data.get("description") or "").strip() or None
    template_items = data.get("template_items")
    if not isinstance(template_items, list) or not template_items:
        return error_response("请至少选择一条题目评阅标准", code=400, http_status_code=400)

    normalized_requests = []
    seen_questions = set()
    standard_ids = set()
    for index, item in enumerate(template_items, start=1):
        if not isinstance(item, dict):
            return error_response(f"第 {index} 条模板明细格式不正确", code=400, http_status_code=400)
        question_no = str(item.get("question_no") or "").strip()
        standard_id = _to_int_or_none(item.get("standard_id"))
        item_id = _to_int_or_none(item.get("item_id"))
        if not question_no:
            return error_response(f"第 {index} 条模板明细缺少试题号", code=400, http_status_code=400)
        if not standard_id:
            return error_response(f"第 {index} 条模板明细缺少阅卷标准版本", code=400, http_status_code=400)
        if question_no in seen_questions:
            return error_response(f"试题 {question_no} 重复选择", code=400, http_status_code=400)
        seen_questions.add(question_no)
        standard_ids.add(standard_id)
        normalized_requests.append({"question_no": question_no, "standard_id": standard_id, "item_id": item_id})

    with get_db() as db:
        exam = await _get_owned_exam(db, user["user_id"], subject_id, exam_id)
        if not exam:
            return error_response("考试不存在或无权访问", code=404, http_status_code=404)

        standards = db.execute(
            select(BizExamGradingStandard).where(
                BizExamGradingStandard.subject_id == subject_id,
                BizExamGradingStandard.exam_id == exam_id,
                BizExamGradingStandard.creator_id == int(user["user_id"]),
                BizExamGradingStandard.standard_id.in_(standard_ids),
            )
        ).scalars().all()
        _resolve_all_standard_items_for_versions(db, standards, subject_id, exam_id, user["user_id"])
        if len(standards) != len(standard_ids):
            return error_response("存在无效的阅卷标准版本", code=400, http_status_code=400)

        standard_map = {standard.standard_id: standard for standard in standards}
        item_index = _build_standard_item_index(standards)
        normalized_items = []
        for request_item in normalized_requests:
            standard_id = request_item["standard_id"]
            question_no = request_item["question_no"]
            item_id = request_item.get("item_id")
            standard = standard_map[standard_id]
            standard_item = None
            if item_id:
                standard_item = next(
                    (
                        item for item in _standard_items_for_standard(standard)
                        if _to_int_or_none(item.get("item_id")) == item_id
                    ),
                    None,
                )
            if not standard_item:
                standard_item = item_index.get((standard_id, question_no))
            if not standard_item:
                return error_response(
                    f"V{standard.version_no} 中未找到试题 {question_no} 的评阅标准",
                    code=400,
                    http_status_code=400,
                )
            normalized_items.append({
                "question_no": question_no,
                "standard_id": standard.standard_id,
                "standard_version_no": standard.version_no,
                "question_version_no": standard_item.get("question_version_no") or 1,
                "standard_item": standard_item,
            })

        template = BizExamGradingTemplate(
            subject_id=subject_id,
            exam_id=exam_id,
            creator_id=int(user["user_id"]),
            template_name=template_name,
            description=description,
            template_items=normalized_items,
        )
        db.add(template)
        db.commit()
        db.refresh(template)
        result = _grading_template_to_dict(template)

    return success_response(result, message="科目考试阅卷模板保存成功", code=0)


async def update_grading_template(request: Request):
    """更新教师已组装的阅卷模板，不创建新的模板记录。"""
    user, error = _current_teacher(request)
    if error:
        return error

    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error
    exam_id, exam_id_error = _get_exam_id(request)
    if exam_id_error:
        return exam_id_error
    try:
        template_id = int(request.path_params["template_id"])
        data = await request.json()
    except (KeyError, TypeError, ValueError):
        return error_response("阅卷模板ID或数据格式不正确", code=400, http_status_code=400)
    except Exception:
        return error_response("阅卷模板数据格式不正确", code=400, http_status_code=400)

    template_name = str(data.get("template_name") or "科目考试阅卷模板").strip() or "科目考试阅卷模板"
    description = str(data.get("description") or "").strip() or None
    template_items = data.get("template_items")
    if not isinstance(template_items, list) or not template_items:
        return error_response("请至少选择一条题目评阅标准", code=400, http_status_code=400)

    normalized_requests = []
    seen_questions = set()
    standard_ids = set()
    for index, item in enumerate(template_items, start=1):
        if not isinstance(item, dict):
            return error_response(f"第 {index} 条模板明细格式不正确", code=400, http_status_code=400)
        question_no = str(item.get("question_no") or "").strip()
        standard_id = _to_int_or_none(item.get("standard_id"))
        item_id = _to_int_or_none(item.get("item_id"))
        if not question_no:
            return error_response(f"第 {index} 条模板明细缺少试题号", code=400, http_status_code=400)
        if not standard_id:
            return error_response(f"第 {index} 条模板明细缺少阅卷标准版本", code=400, http_status_code=400)
        if question_no in seen_questions:
            return error_response(f"试题 {question_no} 重复选择", code=400, http_status_code=400)
        seen_questions.add(question_no)
        standard_ids.add(standard_id)
        normalized_requests.append({"question_no": question_no, "standard_id": standard_id, "item_id": item_id})

    with get_db() as db:
        exam = await _get_owned_exam(db, user["user_id"], subject_id, exam_id)
        if not exam:
            return error_response("考试不存在或无权访问", code=404, http_status_code=404)

        template = db.execute(
            select(BizExamGradingTemplate).where(
                BizExamGradingTemplate.template_id == template_id,
                BizExamGradingTemplate.subject_id == subject_id,
                BizExamGradingTemplate.exam_id == exam_id,
                BizExamGradingTemplate.creator_id == int(user["user_id"]),
            )
        ).scalar_one_or_none()
        if not template:
            return error_response("阅卷模板不存在或无权编辑", code=404, http_status_code=404)

        standards = db.execute(
            select(BizExamGradingStandard).where(
                BizExamGradingStandard.subject_id == subject_id,
                BizExamGradingStandard.exam_id == exam_id,
                BizExamGradingStandard.creator_id == int(user["user_id"]),
                BizExamGradingStandard.standard_id.in_(standard_ids),
            )
        ).scalars().all()
        _resolve_all_standard_items_for_versions(db, standards, subject_id, exam_id, user["user_id"])
        if len(standards) != len(standard_ids):
            return error_response("存在无效的阅卷标准版本", code=400, http_status_code=400)

        standard_map = {standard.standard_id: standard for standard in standards}
        item_index = _build_standard_item_index(standards)
        normalized_items = []
        for request_item in normalized_requests:
            standard_id = request_item["standard_id"]
            question_no = request_item["question_no"]
            item_id = request_item.get("item_id")
            standard = standard_map[standard_id]
            standard_item = None
            if item_id:
                standard_item = next(
                    (
                        item for item in _standard_items_for_standard(standard)
                        if _to_int_or_none(item.get("item_id")) == item_id
                    ),
                    None,
                )
            if not standard_item:
                standard_item = item_index.get((standard_id, question_no))
            if not standard_item:
                return error_response(
                    f"V{standard.version_no} 中未找到试题 {question_no} 的评阅标准",
                    code=400,
                    http_status_code=400,
                )
            normalized_items.append({
                "question_no": question_no,
                "standard_id": standard.standard_id,
                "standard_version_no": standard.version_no,
                "question_version_no": standard_item.get("question_version_no") or 1,
                "standard_item": standard_item,
            })

        template.template_name = template_name
        template.description = description
        template.template_items = normalized_items
        db.commit()
        db.refresh(template)
        result = _grading_template_to_dict(template)

    return success_response(result, message="科目考试阅卷模板更新成功", code=0)


async def delete_grading_template(request: Request):
    """删除教师已组装的阅卷模板。"""
    user, error = _current_teacher(request)
    if error:
        return error

    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error
    exam_id, exam_id_error = _get_exam_id(request)
    if exam_id_error:
        return exam_id_error
    try:
        template_id = int(request.path_params["template_id"])
    except (KeyError, TypeError, ValueError):
        return error_response("阅卷模板ID不正确", code=400, http_status_code=400)

    try:
        with get_db() as db:
            exam = await _get_owned_exam(db, user["user_id"], subject_id, exam_id)
            if not exam:
                return error_response("考试不存在或无权访问", code=404, http_status_code=404)

            template = db.execute(
                select(BizExamGradingTemplate).where(
                    BizExamGradingTemplate.template_id == template_id,
                    BizExamGradingTemplate.subject_id == subject_id,
                    BizExamGradingTemplate.exam_id == exam_id,
                    BizExamGradingTemplate.creator_id == int(user["user_id"]),
                )
            ).scalar_one_or_none()
            if not template:
                return error_response("阅卷模板不存在或无权删除", code=404, http_status_code=404)

            db.delete(template)
            db.commit()
    except SQLAlchemyError as exc:
        mcp_logger.error(f"删除阅卷模板失败: {str(exc)}")
        return error_response(f"删除阅卷模板失败: {str(exc)}", code=500, http_status_code=500)

    return success_response({"template_id": template_id}, message="阅卷模板已删除", code=0)


async def copy_grading_template(request: Request):
    """将一场考试的阅卷模板复制到同科目下另一场考试。"""
    user, error = _current_teacher(request)
    if error:
        return error

    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error
    exam_id, exam_id_error = _get_exam_id(request)
    if exam_id_error:
        return exam_id_error
    try:
        template_id = int(request.path_params["template_id"])
    except (KeyError, TypeError, ValueError):
        return error_response("阅卷模板ID不正确", code=400, http_status_code=400)

    try:
        data = await request.json()
    except Exception:
        return error_response("请求体格式不正确", code=400, http_status_code=400)

    target_exam_id = _to_int_or_none(data.get("target_exam_id"))
    if not target_exam_id:
        return error_response("目标考试ID不能为空", code=400, http_status_code=400)
    if target_exam_id == exam_id:
        return error_response("目标考试与源考试相同", code=400, http_status_code=400)

    template_name = str(data.get("template_name") or "").strip() or None

    try:
        with get_db() as db:
            source_exam = await _get_owned_exam(db, user["user_id"], subject_id, exam_id)
            if not source_exam:
                return error_response("源考试不存在或无权访问", code=404, http_status_code=404)

            source_template = db.execute(
                select(BizExamGradingTemplate).where(
                    BizExamGradingTemplate.template_id == template_id,
                    BizExamGradingTemplate.subject_id == subject_id,
                    BizExamGradingTemplate.exam_id == exam_id,
                    BizExamGradingTemplate.creator_id == int(user["user_id"]),
                )
            ).scalar_one_or_none()
            if not source_template:
                return error_response("源阅卷模板不存在或无权访问", code=404, http_status_code=404)

            target_exam = await _get_owned_exam(db, user["user_id"], subject_id, target_exam_id)
            if not target_exam:
                return error_response("目标考试不存在或无权访问", code=404, http_status_code=404)

            # 加载目标考试下的所有标准，按 question_no 分组，每组取最新版本
            target_standards = db.execute(
                select(BizExamGradingStandard).where(
                    BizExamGradingStandard.subject_id == subject_id,
                    BizExamGradingStandard.exam_id == target_exam_id,
                    BizExamGradingStandard.creator_id == int(user["user_id"]),
                )
            ).scalars().all()
            _resolve_all_standard_items_for_versions(
                db, target_standards, subject_id, target_exam_id, user["user_id"]
            )

            latest_by_question = {}
            for std in target_standards:
                items = _standard_items_for_standard(std)
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    qno = str(item.get("question_no") or "").strip()
                    if not qno:
                        continue
                    current = latest_by_question.get(qno)
                    if current is None or int(std.version_no or 0) > int(current[0] or 0):
                        latest_by_question[qno] = (int(std.version_no or 0), std, item)

            source_items = source_template.template_items or []
            if isinstance(source_items, str):
                try:
                    source_items = json.loads(source_items)
                except Exception:
                    source_items = []

            missing = []
            new_items = []
            for item in source_items:
                if not isinstance(item, dict):
                    continue
                qno = str(item.get("question_no") or "").strip()
                if not qno:
                    continue
                mapped = latest_by_question.get(qno)
                if not mapped:
                    missing.append(qno)
                    continue
                _, std, std_item = mapped
                new_items.append({
                    "question_no": qno,
                    "standard_id": std.standard_id,
                    "standard_version_no": std.version_no,
                    "question_version_no": std_item.get("question_version_no") or 1,
                    "standard_item": std_item,
                })

            if missing:
                return error_response(
                    f"目标考试缺少题号 {', '.join(missing)} 的评分标准，请先为目标考试配置阅卷标准",
                    code=400,
                    http_status_code=400,
                )

            final_name = template_name or f"{source_template.template_name}（副本）"
            new_template = BizExamGradingTemplate(
                subject_id=subject_id,
                exam_id=target_exam_id,
                creator_id=int(user["user_id"]),
                template_name=final_name,
                description=source_template.description,
                template_items=new_items,
            )
            db.add(new_template)
            db.commit()
            db.refresh(new_template)
            result = _grading_template_to_dict(new_template)
    except SQLAlchemyError as exc:
        mcp_logger.error(f"复制阅卷模板失败: {str(exc)}")
        return error_response(f"复制阅卷模板失败: {str(exc)}", code=500, http_status_code=500)

    return success_response(result, message="阅卷模板已复制", code=0)


async def grading_tasks(request: Request):
    """读取或创建阅卷任务；评分结果仍写入既有本地阅卷记录。"""
    user, error = _current_teacher(request)
    if error:
        return error

    _ensure_local_package_schema()
    _ensure_grading_task_schema()

    if request.method == "GET":
        with get_db() as db:
            rows = db.execute(
                select(BizExamGradingTask, BizLocalExamPackage, BizExam, BizSubject, BizExamGradingTemplate)
                .join(BizLocalExamPackage, BizLocalExamPackage.local_package_id == BizExamGradingTask.local_package_id)
                .join(BizExam, BizExam.id == BizExamGradingTask.exam_id)
                .join(BizSubject, BizSubject.subject_id == BizExamGradingTask.subject_id)
                .join(BizExamGradingTemplate, BizExamGradingTemplate.template_id == BizExamGradingTask.template_id)
                .where(BizExamGradingTask.creator_id == int(user["user_id"]))
                .order_by(BizExamGradingTask.created_at.desc(), BizExamGradingTask.task_id.desc())
            ).all()
            result = []
            for task, package, exam, subject, template in rows:
                result.append({
                    "task_id": task.task_id,
                    "subject_id": task.subject_id,
                    "subject_no": subject.subject_no,
                    "subject_name": subject.subject_name,
                    "exam_id": task.exam_id,
                    "exam_name": exam.exam_name,
                    "local_package_id": task.local_package_id,
                    "package_name": package.package_name,
                    "student_count": package.student_count or 0,
                    "package_fingerprint": package.package_fingerprint,
                    "standard_id": task.standard_id,
                    "template_id": task.template_id,
                    "template_name": template.template_name,
                    "template_items": _ensure_list_payload(template.template_items),
                    "created_at": task.created_at.strftime("%Y-%m-%d %H:%M:%S") if task.created_at else None,
                })
        return success_response(result)

    if request.method != "POST":
        return error_response("请求方法不支持", code=405, http_status_code=405)

    try:
        data = await request.json()
        subject_id = int(data.get("subject_id"))
        exam_id = int(data.get("exam_id"))
        local_package_id = int(data.get("local_package_id"))
        standard_id = int(data.get("standard_id"))
        template_id = int(data.get("template_id"))
    except (TypeError, ValueError, AttributeError):
        return error_response("科目、考试、试卷、阅卷标准和阅卷模板均为必选项", code=400, http_status_code=400)

    with get_db() as db:
        exam = await _get_owned_exam(db, user["user_id"], subject_id, exam_id)
        if not exam:
            return error_response("考试不存在或无权访问", code=404, http_status_code=404)
        package = await _get_owned_local_package(db, user["user_id"], subject_id, exam_id, local_package_id)
        if not package:
            return error_response("试卷包不存在或无权访问", code=404, http_status_code=404)
        existing_task = db.execute(select(BizExamGradingTask.task_id).where(
            BizExamGradingTask.creator_id == int(user["user_id"]),
            BizExamGradingTask.local_package_id == local_package_id,
        )).scalar_one_or_none()
        if existing_task:
            return error_response("该试卷包已创建阅卷任务，请在阅卷管理列表中查看", code=409, http_status_code=409)
        standard = db.execute(select(BizExamGradingStandard).where(
            BizExamGradingStandard.standard_id == standard_id,
            BizExamGradingStandard.subject_id == subject_id,
            BizExamGradingStandard.exam_id == exam_id,
            BizExamGradingStandard.creator_id == int(user["user_id"]),
        )).scalar_one_or_none()
        if not standard:
            return error_response("阅卷标准不存在或无权访问", code=404, http_status_code=404)
        template = db.execute(select(BizExamGradingTemplate).where(
            BizExamGradingTemplate.template_id == template_id,
            BizExamGradingTemplate.subject_id == subject_id,
            BizExamGradingTemplate.exam_id == exam_id,
            BizExamGradingTemplate.creator_id == int(user["user_id"]),
        )).scalar_one_or_none()
        if not template:
            return error_response("阅卷模板不存在或无权访问", code=404, http_status_code=404)

        task = BizExamGradingTask(
            subject_id=subject_id,
            exam_id=exam_id,
            local_package_id=local_package_id,
            standard_id=standard_id,
            template_id=template_id,
            creator_id=int(user["user_id"]),
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        result = {
            "task_id": task.task_id,
            "subject_id": subject_id,
            "exam_id": exam_id,
            "local_package_id": local_package_id,
            "standard_id": standard_id,
            "template_id": template_id,
            "package_name": package.package_name,
            "template_name": template.template_name,
            "created_at": task.created_at.strftime("%Y-%m-%d %H:%M:%S") if task.created_at else None,
        }

    return success_response(result, message="阅卷任务已创建", code=0)


async def exam_grading_templates(request: Request):
    if request.method == "GET":
        return await list_grading_templates(request)
    if request.method == "POST":
        return await create_grading_template(request)

    return error_response("请求方法不支持", code=405, http_status_code=405)

async def exam_grading_standards(request: Request):
    if request.method == "GET":
        return await get_latest_grading_standard(request)
    if request.method == "POST":
        return await create_grading_standard(request)

    return error_response("请求方法不支持", code=405, http_status_code=405)


async def register_local_package(request: Request):
    """登记或匹配浏览器本地试卷包，不上传大文件。"""
    user, error = _current_teacher(request)
    if error:
        return error

    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error
    exam_id, exam_id_error = _get_exam_id(request)
    if exam_id_error:
        return exam_id_error

    try:
        data = await request.json()
    except Exception:
        return error_response("本地试卷包数据格式不正确", code=400, http_status_code=400)

    package_name = str(data.get("package_name") or "").strip()
    package_fingerprint = str(data.get("package_fingerprint") or "").strip()
    if not package_name:
        return error_response("试卷包名称不能为空")
    if not package_fingerprint:
        return error_response("试卷包指纹不能为空")

    structure_summary = data.get("structure_summary") if isinstance(data.get("structure_summary"), dict) else {}
    answer_index = data.get("answer_index") if isinstance(data.get("answer_index"), list) else []
    file_manifest = data.get("file_manifest") if isinstance(data.get("file_manifest"), list) else []

    def _pick_best_package_id(candidates):
        """从 (local_package_id, updated_at) 行中选最优记录 ID。

        匹配阶段只查主键和更新时间两个小字段，避免把 parsed_students 等
        几十 MB 的 JSON 大字段拉进 MySQL 排序缓冲区（Out of sort memory）。
        优先选历史评分记录最多的试卷包，其次按更新时间。
        """
        candidate_list = [
            (int(row[0]), row[1].timestamp() if row[1] else 0)
            for row in (candidates or [])
            if row[0] is not None
        ]
        if not candidate_list:
            return None
        record_count_map = {}
        ids = [item[0] for item in candidate_list]
        count_rows = db.execute(
            select(
                BizLocalGradingRecord.local_package_id,
                func.count(BizLocalGradingRecord.record_id),
            )
            .where(
                BizLocalGradingRecord.creator_id == int(user["user_id"]),
                BizLocalGradingRecord.local_package_id.in_(ids),
            )
            .group_by(BizLocalGradingRecord.local_package_id)
        ).all()
        record_count_map = {row[0]: int(row[1] or 0) for row in count_rows}
        return sorted(
            candidate_list,
            key=lambda item: (record_count_map.get(item[0], 0), item[1], item[0]),
            reverse=True,
        )[0][0]

    def _package_has_real_students(package) -> bool:
        # 只有解析出真实题目（非 is_blank 且 result 非空）的试卷包才可复用；
        # 历史“考生数量对、题目全空”的坏缓存不允许再被指纹匹配命中。
        students = package.parsed_students
        if not isinstance(students, list):
            return False
        return any(
            isinstance(student, dict)
            and not student.get("is_blank")
            and isinstance(student.get("result"), list)
            and student["result"]
            for student in students
        )

    def _find_matching_package(db):
        # 四级匹配：完整指纹 -> 快速指纹 -> 学号哈希 -> 文件指纹。
        # 文件指纹只依赖原始文件（路径+大小+内容哈希），可以在
        # Word 全量转换之前计算，用于“重复上传同一试卷包”秒级复用。
        for condition in (
            [BizLocalExamPackage.package_fingerprint == package_fingerprint],
            [BizLocalExamPackage.quick_fingerprint == quick_fingerprint] if quick_fingerprint else None,
            [BizLocalExamPackage.student_no_hash == student_no_hash] if student_no_hash else None,
            [BizLocalExamPackage.file_fingerprint == file_fingerprint] if file_fingerprint else None,
        ):
            if not condition:
                continue
            rows = db.execute(
                select(
                    BizLocalExamPackage.local_package_id,
                    BizLocalExamPackage.updated_at,
                ).where(
                    *base_package_filters,
                    *condition,
                )
            ).all()
            best_id = _pick_best_package_id(rows)
            if best_id is None:
                continue
            candidate = db.execute(
                select(BizLocalExamPackage).where(
                    BizLocalExamPackage.local_package_id == best_id
                )
            ).scalar_one_or_none()
            if candidate is not None and _package_has_real_students(candidate):
                return candidate
        return None

    _ensure_local_package_schema()
    with get_db() as db:
        exam = await _get_owned_exam(db, user["user_id"], subject_id, exam_id)
        if not exam:
            return error_response("考试不存在或无权访问", code=404, http_status_code=404)

        base_package_filters = [
            BizLocalExamPackage.subject_id == subject_id,
            BizLocalExamPackage.exam_id == exam_id,
            BizLocalExamPackage.creator_id == int(user["user_id"]),
            BizLocalExamPackage.status == "active",
        ]
        quick_fingerprint = str(data.get("quick_fingerprint") or "").strip()
        student_no_hash = str(data.get("student_no_hash") or "").strip()
        file_fingerprint = str(data.get("file_fingerprint") or "").strip()
        probe = bool(data.get("probe"))
        package = _find_matching_package(db)
        matched = package is not None
        if matched and probe:
            # 探测模式：只查询是否已有同内容试卷包，不改动任何数据。
            return success_response(
                {"matched": True, "package": _local_package_to_dict(package)},
                message="已匹配历史试卷包",
                code=0,
            )
        if not package:
            if probe:
                # 探测未命中时不创建记录，等真正解析完成后再登记。
                return success_response({"matched": False, "package": None}, code=0)
            package = BizLocalExamPackage(
                subject_id=subject_id,
                exam_id=exam_id,
                creator_id=int(user["user_id"]),
                package_name=package_name,
                package_fingerprint=package_fingerprint,
            )
            db.add(package)

        package.package_name = package_name
        package.quick_fingerprint = quick_fingerprint or None
        package.file_fingerprint = file_fingerprint or None
        package.folder_name = str(data.get("folder_name") or "").strip() or None
        package.json_file_name = str(data.get("json_file_name") or "").strip() or None
        package.json_file_size = _to_int_or_none(data.get("json_file_size"))
        package.student_count = _to_int_or_none(data.get("student_count")) or len(answer_index)
        package.image_count = _to_int_or_none(data.get("image_count")) or len(file_manifest)
        package.word_document_count = _to_int_or_none(data.get("word_document_count")) or 0
        package.total_file_size = _to_int_or_none(data.get("total_file_size"))
        package.total_image_size = _to_int_or_none(data.get("total_image_size"))
        package.total_word_size = _to_int_or_none(data.get("total_word_size"))
        package.student_no_hash = student_no_hash or None
        package.structure_summary = structure_summary
        package.answer_index = answer_index
        package.file_manifest = file_manifest
        if isinstance(data.get("parsed_students"), list):
            package.parsed_students = data["parsed_students"]
        if isinstance(data.get("parsed_image_manifest"), list):
            package.parsed_image_manifest = data["parsed_image_manifest"]
        source_token = str(data.get("source_token") or "").strip()
        if source_token and structure_summary.get("answer_key_name"):
            package.reference_answers = None
            package.answer_key_json = None
        if source_token and structure_summary.get("blank_paper_name"):
            package.blank_paper_json = None
        if isinstance(data.get("reference_answers"), list):
            package.reference_answers = data["reference_answers"]
        if isinstance(data.get("blank_paper_json"), dict):
            package.blank_paper_json = data["blank_paper_json"]
        if isinstance(data.get("answer_key_json"), dict):
            package.answer_key_json = data["answer_key_json"]
        package.status = "active"
        db.commit()
        db.refresh(package)

    if source_token:
        stage_dir = LOCAL_PACKAGE_STAGE_DIR / str(user["user_id"]) / source_token
        final_dir = LOCAL_PACKAGE_DATA_DIR / str(user["user_id"]) / str(package.local_package_id)
        try:
            final_dir.parent.mkdir(parents=True, exist_ok=True)
            if final_dir.exists():
                shutil.rmtree(final_dir)
            if stage_dir.exists():
                shutil.move(str(stage_dir), str(final_dir))
                with get_db() as db:
                    stored = db.execute(
                        select(BizLocalExamPackage).where(
                            BizLocalExamPackage.local_package_id == package.local_package_id
                        )
                    ).scalar_one_or_none()
                    if stored:
                        stored.parsed_storage_path = final_dir.relative_to(settings.MCP_BASE_DIR).as_posix()
                        db.commit()
                        db.refresh(stored)
                        package = stored
        except Exception as exc:
            mcp_logger.error(f"移动本地试卷包持久化文件失败: {exc}")

    return success_response({
        "matched": matched,
        "package": _local_package_to_dict(package),
    }, message="本地试卷包匹配成功" if matched else "本地试卷包登记成功", code=0)


async def process_local_package(request: Request):
    """批量转换试卷包中的学生 Word，并保存参考答案原文件。"""
    user, error = _current_teacher(request)
    if error:
        return error
    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error
    exam_id, exam_id_error = _get_exam_id(request)
    if exam_id_error:
        return exam_id_error

    # 试卷包文件夹可能包含上千个文件（考生文档 + 数据库备份等），
    # Starlette 默认限制 1000 个文件 part、1MB 字段，超限会直接返回 400，
    # 这里显式放宽，避免大试卷包上传被框架拦截。
    try:
        form = await request.form(
            max_files=20000,
            max_fields=10000,
            max_part_size=512 * 1024 * 1024,
        )
    except Exception as exc:
        mcp_logger.error(f"解析试卷包上传表单失败: {exc}")
        return error_response(
            f"上传数据解析失败：{exc}",
            code=400,
            http_status_code=400,
        )
    package_name = str(form.get("package_name") or "").strip()
    uploaded_files = form.getlist("files")
    try:
        relative_paths = json.loads(str(form.get("file_paths") or "[]"))
    except (TypeError, json.JSONDecodeError):
        relative_paths = []
    try:
        raw_folder_paths = json.loads(str(form.get("folder_paths") or "[]"))
    except (TypeError, json.JSONDecodeError):
        raw_folder_paths = []
    folder_paths = [
        str(item)
        for item in raw_folder_paths
        if isinstance(item, str) and item.strip()
    ] if isinstance(raw_folder_paths, list) else []
    if not package_name or (not uploaded_files and not folder_paths):
        return error_response("试卷包名称和试卷包文件不能为空", code=400, http_status_code=400)

    with get_db() as db:
        exam = await _get_owned_exam(db, user["user_id"], subject_id, exam_id)
        if not exam:
            return error_response("考试不存在或无权访问", code=404, http_status_code=404)

    token = uuid4().hex
    stage_dir = LOCAL_PACKAGE_STAGE_DIR / str(user["user_id"]) / token
    input_dir = stage_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    saved_inputs: list[Path] = []
    try:
        for index, upload in enumerate(uploaded_files):
            if not getattr(upload, "filename", None):
                continue
            raw_relative = str(relative_paths[index] if index < len(relative_paths) else upload.filename)
            if Path(upload.filename).suffix.lower() == ".zip":
                archive_path = Path(upload.filename).name
                try:
                    with zipfile.ZipFile(upload.file) as archive:
                        for member in archive.infolist():
                            raw_member_path = member.filename.replace("\\", "/")
                            member_path = Path(raw_member_path)
                            if member_path.is_absolute() or ".." in member_path.parts:
                                continue
                            safe_parts = [part for part in member_path.parts if part not in ("", ".")]
                            if not safe_parts:
                                continue
                            relative_member = Path(*safe_parts)
                            target = input_dir / relative_member
                            if member.is_dir():
                                target.mkdir(parents=True, exist_ok=True)
                                folder_paths.append(relative_member.as_posix())
                                continue
                            target.parent.mkdir(parents=True, exist_ok=True)
                            with archive.open(member) as source, target.open("wb") as destination:
                                _copy_stream_in_chunks(source, destination)
                            saved_inputs.append(target)
                            parent = relative_member.parent
                            while parent.parts:
                                folder_paths.append(parent.as_posix())
                                parent = parent.parent
                except zipfile.BadZipFile as exc:
                    raise ValueError(f"压缩包无法读取：{archive_path}") from exc
                continue
            relative = Path(raw_relative.replace("\\", "/"))
            if relative.is_absolute() or ".." in relative.parts:
                continue
            relative = Path(*[part for part in relative.parts if part not in ("", ".")])
            if not relative.parts:
                relative = Path(upload.filename)
            target = input_dir / relative
            # 单个文件保存失败（如超长路径触发 Windows MAX_PATH）只跳过，
            # 不让一个考生的异常提交拖垮整包上传
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                await _save_upload_file_stream(upload, target)
            except OSError as save_exc:
                mcp_logger.warning(
                    f"跳过无法保存的答案文件: relative={relative.as_posix()}, error={save_exc}"
                )
                continue
            saved_inputs.append(target)

        def save_supporting(field_name: str) -> Path | None:
            upload = form.get(field_name)
            if not upload or not getattr(upload, "filename", None):
                return None
            target = stage_dir / "supporting" / Path(upload.filename).name
            target.parent.mkdir(parents=True, exist_ok=True)
            return target

        blank_upload = form.get("blank_paper")
        answer_upload = form.get("answer_key")
        blank_path = save_supporting("blank_paper")
        answer_path = save_supporting("answer_key")
        if blank_path:
            await _save_upload_file_stream(blank_upload, blank_path)
        if answer_path:
            await _save_upload_file_stream(answer_upload, answer_path)

        inferred_answer_path = next(
            (path for path in saved_inputs if _is_answer_supporting_document(path)),
            None,
        )
        inferred_blank_path = next(
            (path for path in saved_inputs if _is_blank_supporting_document(path)),
            None,
        )
        if not answer_path and inferred_answer_path:
            answer_path = stage_dir / "supporting" / inferred_answer_path.name
            answer_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(inferred_answer_path, answer_path)
        if not blank_path and inferred_blank_path:
            blank_path = stage_dir / "supporting" / inferred_blank_path.name
            blank_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(inferred_blank_path, blank_path)

        student_documents = [
            path for path in saved_inputs
            if _is_student_answer_document(path)
            and not _is_answer_supporting_document(path)
            and not _is_blank_supporting_document(path)
            and not (blank_path and path.name == blank_path.name)
            and not (answer_path and path.name == answer_path.name)
        ]
        json_candidates = [
            path for path in saved_inputs if path.suffix.lower() == ".json"
        ]
        existing_json = json_candidates[0] if json_candidates else None
        # 空白卷解析（第四步改造）：无作答考生以其题目骨架作为各自的试卷。
        # 解析失败只降级为原行为（考生模板兜底），不阻断整包处理。
        blank_paper_document = None
        blank_paper_images: list = []
        if blank_path and blank_path.is_file():
            try:
                blank_parsed = parse_blank_paper(blank_path, stage_dir / "parsed")
                blank_paper_document = blank_parsed.get("blank_paper_json")
                # 空白卷参考图片并入包图片清单：只有进入 parsed_image_manifest，
                # 前端图片解析器才能按 media/blank_paper/... 引用显示
                blank_paper_images = blank_parsed.get("images") or []
            except Exception as blank_exc:
                mcp_logger.warning(f"空白试卷解析失败，改用考生模板兜底: {blank_exc}")
        # 试卷前缀从 input 目录推断（33A 包误用默认 32A 会导致考生 ID 带错误前缀）
        inferred_paper = infer_paper_prefix(input_dir)
        # 表单明确指定的答案卷/空白卷相对路径（显式指定优先于文件名关键词猜测）
        known_answer_key_rel = None
        known_blank_paper_rel = None
        try:
            if answer_path and answer_path.is_file():
                known_answer_key_rel = answer_path.relative_to(stage_dir).as_posix()
            if blank_path and blank_path.is_file():
                known_blank_paper_rel = blank_path.relative_to(stage_dir).as_posix()
        except ValueError:
            pass
        parsed = process_package_documents(
            student_documents=student_documents,
            existing_json=existing_json,
            input_files=saved_inputs,
            output_dir=stage_dir / "parsed",
            input_root=input_dir,
            folder_paths=folder_paths,
            blank_paper_document=blank_paper_document,
            paper=inferred_paper,
            llm_options={
                "enabled": (
                    bool(settings.REFERENCE_PARSER_LLM_ENABLED)
                    and bool(str(settings.REFERENCE_PARSER_LLM_API_KEY or "").strip())
                ),
                "provider": settings.REFERENCE_PARSER_LLM_PROVIDER,
                "model": settings.REFERENCE_PARSER_LLM_MODEL,
                "api_key": settings.REFERENCE_PARSER_LLM_API_KEY,
                "timeout": settings.REFERENCE_PARSER_LLM_TIMEOUT,
            },
        )
        image_manifest = []
        for item in [*parsed["images"], *blank_paper_images]:
            copied = dict(item)
            copied["storage_path"] = str(Path(copied["storage_path"]).relative_to(stage_dir)).replace("\\", "/")
            image_manifest.append(copied)

        parsed_students = parsed["students"]
        blank_student_count = sum(
            1
            for student in parsed_students
            if isinstance(student, dict) and student.get("is_blank")
        )
        if parsed_students and blank_student_count == len(parsed_students):
            mcp_logger.warning(
                f"试卷包未解析出任何考生答题文档，全部 {len(parsed_students)} 名考生为空白卷: "
                f"package_name={package_name}"
            )

        # 第五步改造：结构序列化双写（manifest/unified-paper/candidate-answers/
        # parse-report）。失败只记日志，不影响解析结果返回。
        serialized = serialize_package(
            stage_dir,
            paper=inferred_paper,
            known_answer_key=known_answer_key_rel,
            known_blank_paper=known_blank_paper_rel,
        )
        if serialized.get("status") != "success":
            mcp_logger.warning(f"结构序列化失败（不影响解析结果）: {serialized.get('error')}")

        return success_response({
            "source_token": token,
            "students": parsed_students,
            "blank_student_count": blank_student_count,
            "students_json": Path(parsed["students_json"]).relative_to(stage_dir).as_posix(),
            "images": image_manifest,
            "supporting_files": {
                "answer_key_name": answer_path.name if answer_path else None,
                "blank_paper_name": blank_path.name if blank_path else None,
            },
            "structure_serialized": serialized.get("status") == "success",
            "parse_report": (serialized.get("report") or {}).get("summary"),
        }, message="试卷包文档已批量转换并解析", code=0)
    except Exception as exc:
        shutil.rmtree(stage_dir, ignore_errors=True)
        mcp_logger.exception(f"批量转换试卷包文档失败: {exc}")
        if isinstance(exc, (ValueError, RuntimeError, zipfile.BadZipFile)):
            return error_response(
                f"试卷包文档转换失败: {exc}",
                code=400,
                http_status_code=400,
            )
        return error_response(f"试卷包文档转换失败: {exc}", code=500, http_status_code=500)


async def get_local_package(request: Request):
    user, error = _current_teacher(request)
    if error:
        return error

    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error
    exam_id, exam_id_error = _get_exam_id(request)
    if exam_id_error:
        return exam_id_error
    local_package_id = _normalize_local_package_id(request.path_params.get("local_package_id"))
    if not local_package_id:
        return error_response("本地试卷包ID不正确", code=400, http_status_code=400)

    _ensure_local_package_schema()
    with get_db() as db:
        package = await _get_owned_local_package(db, user["user_id"], subject_id, exam_id, local_package_id)
        if not package:
            return error_response("本地试卷包不存在或无权访问", code=404, http_status_code=404)

    return success_response(_local_package_to_dict(package))


async def import_local_package_reference_answers(request: Request):
    """按需解析本地试卷包已上传的参考答案和评分标准文件。"""
    user, error = _current_teacher(request)
    if error:
        return error
    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error
    exam_id, exam_id_error = _get_exam_id(request)
    if exam_id_error:
        return exam_id_error
    local_package_id = _normalize_local_package_id(request.path_params.get("local_package_id"))
    if not local_package_id:
        return error_response("本地试卷包ID不正确", code=400, http_status_code=400)

    _ensure_local_package_schema()
    with get_db() as db:
        package = await _get_owned_local_package(db, user["user_id"], subject_id, exam_id, local_package_id)
        if not package:
            return error_response("本地试卷包不存在或无权访问", code=404, http_status_code=404)
        if not package.parsed_storage_path:
            return error_response(
                "当前试卷包没有保存参考答案和评分标准原文件，请重新上传试卷包",
                code=400,
                http_status_code=400,
            )

        root = _resolve_local_package_storage_root(package)
        if root is None:
            return error_response("本地试卷包文件不存在", code=404, http_status_code=404)

        structure_summary = package.structure_summary or {}
        answer_path = _find_supporting_word_document(
            root,
            str(structure_summary.get("answer_key_name") or "").strip() or None,
        )
        if not answer_path:
            return error_response(
                "未找到参考答案和评分标准 Word 文件，请重新上传试卷包并选择该文件",
                code=400,
                http_status_code=400,
            )

        try:
            llm_options = {
                "enabled": (
                    bool(settings.REFERENCE_PARSER_LLM_ENABLED)
                    and bool(str(settings.REFERENCE_PARSER_LLM_API_KEY or "").strip())
                ),
                "provider": settings.REFERENCE_PARSER_LLM_PROVIDER,
                "model": settings.REFERENCE_PARSER_LLM_MODEL,
                "api_key": settings.REFERENCE_PARSER_LLM_API_KEY,
                "timeout": settings.REFERENCE_PARSER_LLM_TIMEOUT,
            }
            reference = process_reference_document(
                answer_path,
                root / "parsed",
                json_filename="answer-key.json",
                image_dir_name="answer-images",
                image_prefix="answer",
                llm_options=llm_options,
                expected_questions=_expected_reference_questions_from_package(package),
            )
        except Exception as exc:
            mcp_logger.error(f"导入参考答案和评分标准失败: {exc}")
            return error_response(
                f"导入参考答案和评分标准失败: {exc}",
                code=500,
                http_status_code=500,
            )

        # 第四步改造：从评分细则文本提取每题满分（补充 max_score 字段）；
        # 空白卷骨架（注册时随包解析产出）一并入库，供确认页/结构对齐使用。
        max_score_enriched = enrich_reference_answers_with_max_score(reference["reference_answers"])
        blank_paper_loaded = False
        blank_paper_json_path = root / "parsed" / "blank-paper.json"
        if blank_paper_json_path.is_file():
            try:
                package.blank_paper_json = json.loads(blank_paper_json_path.read_text(encoding="utf-8"))
                blank_paper_loaded = True
            except (OSError, json.JSONDecodeError) as blank_exc:
                mcp_logger.warning(f"空白试卷骨架读取失败: {blank_exc}")

        package.reference_answers = reference["reference_answers"]
        package.answer_key_json = reference["document"]
        db.commit()
        db.refresh(package)
        # 答案卷导入后刷新结构序列化（unified-paper 此时才有权威题目结构）
        import_summary = package.structure_summary or {}
        import_answer_name = str(import_summary.get("answer_key_name") or "").strip()
        import_blank_name = str(import_summary.get("blank_paper_name") or "").strip()
        serialized = serialize_package(
            root,
            paper="",
            known_answer_key=f"supporting/{import_answer_name}" if import_answer_name else None,
            known_blank_paper=f"supporting/{import_blank_name}" if import_blank_name else None,
        )
        if serialized.get("status") != "success":
            mcp_logger.warning(f"结构序列化失败（不影响答案导入结果）: {serialized.get('error')}")
        result = _local_package_to_dict(package)
        result["imported_reference_count"] = len(reference["reference_answers"])
        result["reference_parser_mode"] = reference.get("parser_mode")
        result["reference_parser_warnings"] = reference.get("warnings") or []
        result["reference_llm_error"] = reference.get("llm_error")
        result["max_score_enriched_count"] = max_score_enriched
        result["blank_paper_loaded"] = blank_paper_loaded
        result["structure_serialized"] = serialized.get("status") == "success"
        result["parse_report"] = (serialized.get("report") or {}).get("summary")

    return success_response(result, message="参考答案和评分标准已导入", code=0)


async def get_local_package_file(request: Request):
    user, error = _current_teacher(request)
    if error:
        return error
    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error
    exam_id, exam_id_error = _get_exam_id(request)
    if exam_id_error:
        return exam_id_error
    local_package_id = _normalize_local_package_id(request.path_params.get("local_package_id"))
    if not local_package_id:
        return error_response("本地试卷包ID不正确", code=400, http_status_code=400)
    relative_path = str(request.query_params.get("path") or "").replace("\\", "/").lstrip("/")
    if not relative_path or ".." in Path(relative_path).parts:
        return error_response("文件路径不正确", code=400, http_status_code=400)

    _ensure_local_package_schema()
    with get_db() as db:
        package = await _get_owned_local_package(db, user["user_id"], subject_id, exam_id, local_package_id)
        if not package or not package.parsed_storage_path:
            return error_response("本地试卷包文件不存在", code=404, http_status_code=404)
        root = _resolve_local_package_storage_root(package)
        if root is None:
            return error_response("本地试卷包文件不存在", code=404, http_status_code=404)
        target = (root / relative_path).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return error_response("文件路径不正确", code=400, http_status_code=400)
        if not target.is_file():
            return error_response("本地试卷包文件不存在", code=404, http_status_code=404)
    return FileResponse(target)



async def delete_local_package(request: Request):
    """彻底删除本地试卷包业务记录及其评分记录。"""
    user, error = _current_teacher(request)
    if error:
        return error

    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error
    exam_id, exam_id_error = _get_exam_id(request)
    if exam_id_error:
        return exam_id_error
    local_package_id = _normalize_local_package_id(request.path_params.get("local_package_id"))
    if not local_package_id:
        return error_response("本地试卷包ID不正确", code=400, http_status_code=400)

    try:
        _ensure_local_package_schema()
        with get_db() as db:
            package = await _get_owned_local_package(db, user["user_id"], subject_id, exam_id, local_package_id)
            if not package:
                return error_response("本地试卷包不存在或无权删除", code=404, http_status_code=404)

            records = db.execute(
                select(BizLocalGradingRecord).where(
                    BizLocalGradingRecord.local_package_id == package.local_package_id,
                    BizLocalGradingRecord.creator_id == int(user["user_id"]),
                )
            ).scalars().all()
            for record in records:
                db.delete(record)
            db.delete(package)
            db.commit()
    except SQLAlchemyError as exc:
        mcp_logger.error(f"彻底删除本地试卷包失败: {str(exc)}")
        return error_response(f"彻底删除本地试卷包失败: {str(exc)}", code=500, http_status_code=500)

    return success_response(None, message="本地试卷包历史记录已删除", code=0)


async def get_local_package_structure(request: Request):
    """包结构预检（只读）：返回 Manifest 形态的定位结果。

    供教师确认页与排查使用：考生清单、命中的布局模式、答案卷/素材豁免定位；
    识别失败时返回人话报错与目录树预览。不改动任何包数据。
    """
    user, error = _current_teacher(request)
    if error:
        return error

    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error
    exam_id, exam_id_error = _get_exam_id(request)
    if exam_id_error:
        return exam_id_error
    local_package_id = _normalize_local_package_id(request.path_params.get("local_package_id"))
    if not local_package_id:
        return error_response("本地试卷包ID不正确", code=400, http_status_code=400)

    _ensure_local_package_schema()
    with get_db() as db:
        package = await _get_owned_local_package(db, user["user_id"], subject_id, exam_id, local_package_id)
        if not package:
            return error_response("本地试卷包不存在或无权访问", code=404, http_status_code=404)
        if not getattr(package, "parsed_storage_path", None):
            return error_response("试卷包尚未完成解析，无结构可预检", code=400, http_status_code=400)
        root = _resolve_local_package_storage_root(package)
        if root is None:
            return error_response("试卷包尚未完成解析，无结构可预检", code=400, http_status_code=400)
        # paper 由 input 目录自动推断（如 33A），可用查询参数覆盖
        paper = (request.query_params.get("paper") or "").strip()
        # 包记录里上传时明确指定的答案卷/空白卷文件名（显式定位优先）
        structure_summary = package.structure_summary or {}
        known_answer_key = None
        known_blank_paper = None
        answer_key_name = str(structure_summary.get("answer_key_name") or "").strip()
        blank_paper_name = str(structure_summary.get("blank_paper_name") or "").strip()
        if answer_key_name:
            known_answer_key = f"supporting/{answer_key_name}"
        if blank_paper_name:
            known_blank_paper = f"supporting/{blank_paper_name}"
        result = recognize_package_structure(
            root, paper,
            known_answer_key=known_answer_key,
            known_blank_paper=known_blank_paper,
        )
        # 附带解析报告（结构序列化产物），供前端报告弹窗展示
        parse_report_path = root / "parsed" / "parse-report.json"
        if parse_report_path.is_file():
            try:
                result["parse_report"] = json.loads(parse_report_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as report_exc:
                mcp_logger.warning(f"解析报告读取失败: {report_exc}")
        return _success_response_with_data(result)


async def local_package_detail(request: Request):
    if request.method == "GET":
        return await get_local_package(request)
    if request.method == "DELETE":
        return await delete_local_package(request)
    return error_response("请求方法不支持", code=405, http_status_code=405)
async def list_local_grading_records(request: Request):
    user, error = _current_teacher(request)
    if error:
        return error

    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error
    exam_id, exam_id_error = _get_exam_id(request)
    if exam_id_error:
        return exam_id_error
    local_package_id = _normalize_local_package_id(request.path_params.get("local_package_id"))
    if not local_package_id:
        return error_response("本地试卷包ID不正确", code=400, http_status_code=400)

    _ensure_local_package_schema()
    with get_db() as db:
        package = await _get_owned_local_package(db, user["user_id"], subject_id, exam_id, local_package_id)
        if not package:
            return error_response("本地试卷包不存在或无权访问", code=404, http_status_code=404)
        query = select(BizLocalGradingRecord).where(
            BizLocalGradingRecord.local_package_id == package.local_package_id,
            BizLocalGradingRecord.creator_id == int(user["user_id"]),
        )
        student_no = (request.query_params.get("student_no") or "").strip()
        question_no = (request.query_params.get("question_no") or "").strip()
        if student_no:
            query = query.where(BizLocalGradingRecord.student_no == student_no)
        if question_no:
            query = query.where(BizLocalGradingRecord.question_no == question_no)
        # 先只按主键排序（小字段 filesort），再取整行，避免大文本记录
        # 触发 MySQL "Out of sort memory"。
        ordered_ids = db.execute(
            query.with_only_columns(BizLocalGradingRecord.record_id)
            .order_by(BizLocalGradingRecord.graded_at.desc())
        ).scalars().all()
        if ordered_ids:
            rows = db.execute(
                select(BizLocalGradingRecord).where(
                    BizLocalGradingRecord.record_id.in_(list(ordered_ids))
                )
            ).scalars().all()
            by_id = {record.record_id: record for record in rows}
            records = [by_id[record_id] for record_id in ordered_ids if record_id in by_id]
        else:
            records = []

        creator_names = {}
        creator_ids = {record.creator_id for record in records}
        if creator_ids:
            user_rows = db.execute(
                select(User.id, User.fullname, User.username).where(User.id.in_(creator_ids))
            ).all()
            creator_names = {
                row[0]: (row[1] or row[2] or f"用户{row[0]}")
                for row in user_rows
            }

    return success_response([
        _grading_record_to_dict(record, creator_names.get(record.creator_id, ""))
        for record in records
    ])


async def update_local_grading_round(request: Request):
    """更新试阅轮次的备注/星标（按记录ID批量，作用于同一轮的全部记录）。"""
    user, error = _current_teacher(request)
    if error:
        return error

    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error
    exam_id, exam_id_error = _get_exam_id(request)
    if exam_id_error:
        return exam_id_error
    local_package_id = _normalize_local_package_id(request.path_params.get("local_package_id"))
    if not local_package_id:
        return error_response("本地试卷包ID不正确", code=400, http_status_code=400)

    try:
        data = await request.json()
    except Exception:
        return error_response("请求数据格式不正确", code=400, http_status_code=400)

    record_ids = [int(item) for item in (data.get("record_ids") or []) if str(item).strip().isdigit()]
    if not record_ids:
        return error_response("缺少需要更新的记录ID", code=400, http_status_code=400)
    if "note" not in data and "starred" not in data:
        return error_response("没有需要更新的内容", code=400, http_status_code=400)

    note = data.get("note")
    if note is not None and len(str(note)) > 255:
        return error_response("备注最长 255 个字符", code=400, http_status_code=400)
    starred = data.get("starred")

    with get_db() as db:
        package = await _get_owned_local_package(db, user["user_id"], subject_id, exam_id, local_package_id)
        if not package:
            return error_response("本地试卷包不存在或无权访问", code=404, http_status_code=404)
        records = db.execute(
            select(BizLocalGradingRecord).where(
                BizLocalGradingRecord.record_id.in_(record_ids),
                BizLocalGradingRecord.local_package_id == package.local_package_id,
                BizLocalGradingRecord.creator_id == int(user["user_id"]),
            )
        ).scalars().all()
        if not records:
            return error_response("没有可更新的评分记录", code=404, http_status_code=404)
        for record in records:
            if note is not None:
                record.round_note = str(note).strip() or None
            if starred is not None:
                record.round_starred = bool(starred)
        db.commit()

    return success_response({"updated": len(records)}, message="更新成功", code=0)


async def delete_local_grading_records(request: Request):
    """删除不需要的历史试阅记录（仅允许删除试阅记录，正式评阅记录不可删除）。"""
    user, error = _current_teacher(request)
    if error:
        return error

    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error
    exam_id, exam_id_error = _get_exam_id(request)
    if exam_id_error:
        return exam_id_error
    local_package_id = _normalize_local_package_id(request.path_params.get("local_package_id"))
    if not local_package_id:
        return error_response("本地试卷包ID不正确", code=400, http_status_code=400)

    try:
        data = await request.json()
    except Exception:
        return error_response("请求数据格式不正确", code=400, http_status_code=400)

    record_ids = [int(item) for item in (data.get("record_ids") or []) if str(item).strip().isdigit()]
    if not record_ids:
        return error_response("缺少需要删除的记录ID", code=400, http_status_code=400)

    with get_db() as db:
        package = await _get_owned_local_package(db, user["user_id"], subject_id, exam_id, local_package_id)
        if not package:
            return error_response("本地试卷包不存在或无权访问", code=404, http_status_code=404)
        records = db.execute(
            select(BizLocalGradingRecord).where(
                BizLocalGradingRecord.record_id.in_(record_ids),
                BizLocalGradingRecord.local_package_id == package.local_package_id,
                BizLocalGradingRecord.creator_id == int(user["user_id"]),
                BizLocalGradingRecord.is_trial == True,  # noqa: E712
            )
        ).scalars().all()
        if not records:
            return error_response("没有可删除的试阅记录（正式评阅记录不可删除）", code=404, http_status_code=404)
        for record in records:
            db.delete(record)
        db.commit()

    return success_response({"deleted": len(records)}, message="删除成功", code=0)


async def save_local_grading_records(request: Request):
    user, error = _current_teacher(request)
    if error:
        return error

    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error
    exam_id, exam_id_error = _get_exam_id(request)
    if exam_id_error:
        return exam_id_error
    local_package_id = _normalize_local_package_id(request.path_params.get("local_package_id"))
    if not local_package_id:
        return error_response("本地试卷包ID不正确", code=400, http_status_code=400)

    try:
        data = await request.json()
    except Exception:
        return error_response("评分结果数据格式不正确", code=400, http_status_code=400)
    records_payload = data.get("records") if isinstance(data.get("records"), list) else None
    if records_payload is None:
        records_payload = [data]
    if not records_payload:
        return error_response("没有需要保存的评分结果")

    try:
        with get_db() as db:
            package = await _get_owned_local_package(db, user["user_id"], subject_id, exam_id, local_package_id)
            if not package:
                return error_response("本地试卷包不存在或无权访问", code=404, http_status_code=404)
            saved_records = []
            # 批量保存同样先攒写回，全部保存后一次性应用
            pending_updates = []
            for payload in records_payload:
                if not isinstance(payload, dict):
                    continue
                record = _save_local_grading_record(
                    db,
                    apply_student_json=False,
                    user_id=user["user_id"],
                    subject_id=subject_id,
                    exam_id=exam_id,
                    local_package=package,
                    payload=payload,
                )
                saved_records.append(record)
                if record is not None and not record.is_trial:
                    pending_updates.append({
                        "student_no": record.student_no,
                        "question_no": record.question_no,
                        "score": record.score,
                        "comment": record.grading_desc,
                    })
            for update in pending_updates:
                _set_formal_score_on_student_json(
                    package,
                    student_no=update["student_no"],
                    question_no=update["question_no"],
                    score=update["score"],
                    comment=update["comment"],
                )
            db.commit()
            result = [_grading_record_to_dict(record) for record in saved_records]
    except (SQLAlchemyError, ValueError) as exc:
        mcp_logger.error(f"保存本地评分结果失败: {str(exc)}")
        return error_response(f"保存本地评分结果失败: {str(exc)}", code=500, http_status_code=500)

    return success_response(result, message="评分结果保存成功", code=0)


async def local_grading_records(request: Request):
    if request.method == "GET":
        return await list_local_grading_records(request)
    if request.method == "POST":
        return await save_local_grading_records(request)
    return error_response("请求方法不支持", code=405, http_status_code=405)
async def list_packages(request: Request):
    """获取当前考试下的试卷包。"""
    user, error = _current_teacher(request)
    if error:
        return error

    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error
    exam_id, exam_id_error = _get_exam_id(request)
    if exam_id_error:
        return exam_id_error
    keyword = (request.query_params.get("keyword") or "").strip()

    with get_db() as db:
        exam = await _get_owned_exam(db, user["user_id"], subject_id, exam_id)
        if not exam:
            return error_response("考试不存在或无权访问", code=404, http_status_code=404)

        query = select(BizExamPackage).where(BizExamPackage.exam_id == exam_id)
        if keyword:
            query = query.where(BizExamPackage.package_name.like(f"%{keyword}%"))
        query = query.order_by(BizExamPackage.uploaded_at.desc(), BizExamPackage.package_id.desc())
        packages = db.execute(query).scalars().all()

    return success_response([_package_to_dict(package) for package in packages])


async def list_all_packages(request: Request):
    """获取当前教师名下全部考试的试卷包，供独立答卷管理页面使用。"""
    user, error = _current_teacher(request)
    if error:
        return error

    keyword = (request.query_params.get("keyword") or "").strip()
    subject_id = request.query_params.get("subject_id")
    exam_id = request.query_params.get("exam_id")
    with get_db() as db:
        query = (
            select(BizExamPackage, BizExam, BizSubject)
            .join(BizExam, BizExam.id == BizExamPackage.exam_id)
            .join(BizSubject, BizSubject.subject_id == BizExam.subject_id)
            .where(
                BizExam.creator_id == int(user["user_id"]),
                BizSubject.creator_id == int(user["user_id"]),
            )
        )
        if subject_id:
            try:
                query = query.where(BizSubject.subject_id == int(subject_id))
            except (TypeError, ValueError):
                return error_response("科目编号无效")
        if exam_id:
            try:
                query = query.where(BizExam.id == int(exam_id))
            except (TypeError, ValueError):
                return error_response("考试编号无效")
        if keyword:
            query = query.where(
                or_(
                    BizExamPackage.package_name.like(f"%{keyword}%"),
                    BizExam.exam_name.like(f"%{keyword}%"),
                )
            )
        query = query.order_by(BizExamPackage.uploaded_at.desc(), BizExamPackage.package_id.desc())
        rows = db.execute(query).all()

    packages = []
    for package, exam, subject in rows:
        item = _package_to_dict(package)
        item.update({
            "subject_id": subject.subject_id,
            "subject_no": subject.subject_no,
            "subject_name": subject.subject_name,
            "exam_name": exam.exam_name,
        })
        packages.append(item)

    # 独立答卷管理页还要展示原系统解析器创建的本地试卷包登记记录。
    with get_db() as db:
        local_query = (
            # 列表只需要轻量摘要字段。不能 select ORM 整行：该实体包含
            # parsed_students/file_manifest 等大型 JSON，MySQL 排序时会把
            # 它们一并放入临时结果，数据量较大时触发 1038 Out of sort memory。
            select(
                BizLocalExamPackage.local_package_id,
                BizLocalExamPackage.exam_id,
                BizLocalExamPackage.subject_id,
                BizLocalExamPackage.package_name,
                BizLocalExamPackage.student_count,
                BizLocalExamPackage.parsed_storage_path,
                BizLocalExamPackage.created_at,
                BizExam.exam_name,
                BizSubject.subject_no,
                BizSubject.subject_name,
            )
            .join(BizExam, BizExam.id == BizLocalExamPackage.exam_id)
            .join(BizSubject, BizSubject.subject_id == BizLocalExamPackage.subject_id)
            .where(
                BizLocalExamPackage.creator_id == int(user["user_id"]),
                BizLocalExamPackage.status == "active",
                BizExam.creator_id == int(user["user_id"]),
                BizSubject.creator_id == int(user["user_id"]),
            )
        )
        if subject_id:
            local_query = local_query.where(BizLocalExamPackage.subject_id == int(subject_id))
        if exam_id:
            local_query = local_query.where(BizLocalExamPackage.exam_id == int(exam_id))
        if keyword:
            local_query = local_query.where(
                or_(
                    BizLocalExamPackage.package_name.like(f"%{keyword}%"),
                    BizExam.exam_name.like(f"%{keyword}%"),
                )
            )
        local_rows = db.execute(local_query.order_by(
            BizLocalExamPackage.created_at.desc(),
            BizLocalExamPackage.local_package_id.desc(),
        )).all()
        for row in local_rows:
            packages.append({
                "package_id": f"local_{row.local_package_id}",
                "local_package_id": row.local_package_id,
                "isLocal": True,
                "exam_id": row.exam_id,
                "subject_id": row.subject_id,
                "subject_no": row.subject_no,
                "subject_name": row.subject_name,
                "exam_name": row.exam_name,
                "package_name": row.package_name,
                "student_count": row.student_count,
                "extract_path": row.parsed_storage_path or "parsed",
                "uploaded_at": (
                    row.created_at.strftime("%Y-%m-%d %H:%M:%S")
                    if row.created_at else None
                ),
            })
    return success_response(packages)


async def create_package(request: Request):
    """创建试卷包并解析其中的考生答卷。"""
    user, error = _current_teacher(request)
    if error:
        return error

    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error
    exam_id, exam_id_error = _get_exam_id(request)
    if exam_id_error:
        return exam_id_error

    form = await request.form()
    package_name = (form.get("package_name") or "").strip()
    package_file = form.get("package_file")
    blank_paper = form.get("blank_paper")

    if not package_name:
        return error_response("试卷包名称不能为空")
    if not package_file or not getattr(package_file, "filename", None):
        return error_response("请上传试卷包文件")

    try:
        with get_db() as db:
            exam = await _get_owned_exam(db, user["user_id"], subject_id, exam_id)
            if not exam:
                return error_response("考试不存在或无权访问", code=404, http_status_code=404)

            saved_path, relative_file_path = await _save_package_file(
                package_file,
                user["user_id"],
                subject_id,
                exam_id,
            )
            if not saved_path:
                return error_response("试卷包文件保存失败")

            if saved_path.suffix.lower() == ".zip":
                _, preview_entries, _, json_candidates, parsed_json_count = _find_answer_json_in_zip(saved_path)
                if not preview_entries:
                    if not json_candidates:
                        return error_response("压缩包中没有找到 .json 文件")
                    if parsed_json_count == 0:
                        return error_response("压缩包中存在 JSON 文件，但 JSON 内容解析失败")
                    return error_response("压缩包中存在 JSON 文件，但没有按模板规则识别出考生答题数据")
            elif saved_path.suffix.lower() == ".json":
                preview_json = json.loads(_decode_json_bytes(saved_path.read_bytes()))
                if not _extract_student_entries(preview_json):
                    return error_response("JSON 文件中未找到可解析的考生答题数据")

            blank_paper_path = await _save_exam_file(
                blank_paper,
                user["user_id"],
                subject_id,
                "blank_paper",
            )
            if blank_paper_path:
                exam.blank_paper_path = blank_paper_path

            package = BizExamPackage(
                exam_id=exam_id,
                package_name=package_name,
                file_path=relative_file_path,
                extract_path=None,
            )
            db.add(package)
            db.commit()
            db.refresh(package)
            result = _package_to_dict(package)

            parser_thread = threading.Thread(
                target=_parse_package_answers_background,
                args=(package.package_id, exam_id, str(saved_path)),
                daemon=True,
            )
            parser_thread.start()
    except (SQLAlchemyError, zipfile.BadZipFile) as e:
        mcp_logger.error(f"创建试卷包错误: {str(e)}")
        return error_response(
            f"创建试卷包错误: {str(e)}",
            code=500,
            http_status_code=500,
        )

    return success_response(result, "试卷包创建成功，系统正在后台解析考生答卷", code=0)


async def exam_packages(request: Request):
    if request.method == "GET":
        return await list_packages(request)
    if request.method == "POST":
        return await create_package(request)

    return error_response("请求方法不支持", code=405, http_status_code=405)


async def get_package(request: Request):
    """获取当前教师创建的单个试卷包。"""
    user, error = _current_teacher(request)
    if error:
        return error

    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error
    exam_id, exam_id_error = _get_exam_id(request)
    if exam_id_error:
        return exam_id_error
    package_id, package_id_error = _get_package_id(request)
    if package_id_error:
        return package_id_error

    with get_db() as db:
        package = await _get_owned_package(db, user["user_id"], subject_id, exam_id, package_id)

    if not package:
        return error_response("试卷包不存在或无权访问", code=404, http_status_code=404)

    return success_response(_package_to_dict(package))


async def delete_package(request: Request):
    """删除当前教师创建的试卷包及其答卷记录。"""
    user, error = _current_teacher(request)
    if error:
        return error

    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error
    exam_id, exam_id_error = _get_exam_id(request)
    if exam_id_error:
        return exam_id_error
    package_id, package_id_error = _get_package_id(request)
    if package_id_error:
        return package_id_error

    try:
        with get_db() as db:
            package = await _get_owned_package(db, user["user_id"], subject_id, exam_id, package_id)
            if not package:
                return error_response("试卷包不存在或无权删除", code=404, http_status_code=404)

            answers = db.execute(
                select(BizAnswerSheet).where(
                    BizAnswerSheet.exam_id == exam_id,
                    BizAnswerSheet.package_id == package_id,
                )
            ).scalars().all()
            for answer in answers:
                files = db.execute(
                    select(BizAnswerFile).where(BizAnswerFile.answer_id == answer.answer_id)
                ).scalars().all()
                for file in files:
                    db.delete(file)
                db.delete(answer)

            db.delete(package)
            db.commit()
    except SQLAlchemyError as e:
        mcp_logger.error(f"删除试卷包数据库错误: {str(e)}")
        return error_response(
            f"删除试卷包数据库错误: {str(e)}",
            code=500,
            http_status_code=500,
        )

    return success_response(None, "试卷包删除成功", code=0)


async def list_answer_sheets(request: Request):
    """获取试卷包内解析出的考生答卷列表。"""
    user, error = _current_teacher(request)
    if error:
        return error

    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error
    exam_id, exam_id_error = _get_exam_id(request)
    if exam_id_error:
        return exam_id_error
    package_id, package_id_error = _get_package_id(request)
    if package_id_error:
        return package_id_error

    with get_db() as db:
        package = await _get_owned_package(db, user["user_id"], subject_id, exam_id, package_id)
        if not package:
            return error_response("试卷包不存在或无权访问", code=404, http_status_code=404)

        answers = db.execute(
            select(BizAnswerSheet)
            .where(
                BizAnswerSheet.exam_id == exam_id,
                BizAnswerSheet.package_id == package_id,
            )
            .order_by(BizAnswerSheet.uploaded_at.desc(), BizAnswerSheet.answer_id.desc())
        ).scalars().all()

    return success_response([_answer_sheet_to_dict(answer) for answer in answers])


async def list_answer_files(request: Request):
    """获取某个考生答卷的文件明细。"""
    user, error = _current_teacher(request)
    if error:
        return error

    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error
    exam_id, exam_id_error = _get_exam_id(request)
    if exam_id_error:
        return exam_id_error
    package_id, package_id_error = _get_package_id(request)
    if package_id_error:
        return package_id_error
    answer_id, answer_id_error = _get_answer_id(request)
    if answer_id_error:
        return answer_id_error

    with get_db() as db:
        package = await _get_owned_package(db, user["user_id"], subject_id, exam_id, package_id)
        if not package:
            return error_response("试卷包不存在或无权访问", code=404, http_status_code=404)

        answer = db.execute(
            select(BizAnswerSheet).where(
                BizAnswerSheet.answer_id == answer_id,
                BizAnswerSheet.exam_id == exam_id,
                BizAnswerSheet.package_id == package_id,
            )
        ).scalar_one_or_none()
        if not answer:
            return error_response("考生答卷不存在或无权访问", code=404, http_status_code=404)

        files = db.execute(
            select(BizAnswerFile)
            .where(BizAnswerFile.answer_id == answer_id)
            .order_by(BizAnswerFile.relative_path.asc(), BizAnswerFile.id.asc())
        ).scalars().all()

    return success_response({
        "answer": _answer_sheet_to_dict(answer),
        "files": [_answer_file_to_dict(file) for file in files],
    })


async def get_answer_image(request: Request):
    """获取考生答卷中的图片资源。"""
    user, error = _current_teacher(request)
    if error:
        return error

    subject_id, subject_id_error = _get_subject_id(request)
    if subject_id_error:
        return subject_id_error
    exam_id, exam_id_error = _get_exam_id(request)
    if exam_id_error:
        return exam_id_error
    package_id, package_id_error = _get_package_id(request)
    if package_id_error:
        return package_id_error
    answer_id, answer_id_error = _get_answer_id(request)
    if answer_id_error:
        return answer_id_error

    try:
        file_id = int(request.path_params["file_id"])
    except (KeyError, TypeError, ValueError):
        return error_response("文件ID不正确", code=400, http_status_code=400)

    with get_db() as db:
        package = await _get_owned_package(db, user["user_id"], subject_id, exam_id, package_id)
        if not package:
            return error_response("试卷包不存在或无权访问", code=404, http_status_code=404)

        answer = db.execute(
            select(BizAnswerSheet).where(
                BizAnswerSheet.answer_id == answer_id,
                BizAnswerSheet.exam_id == exam_id,
                BizAnswerSheet.package_id == package_id,
            )
        ).scalar_one_or_none()
        if not answer:
            return error_response("考生答卷不存在或无权访问", code=404, http_status_code=404)

        answer_file = db.execute(
            select(BizAnswerFile).where(
                BizAnswerFile.id == file_id,
                BizAnswerFile.answer_id == answer_id,
                BizAnswerFile.exam_id == exam_id,
                BizAnswerFile.package_id == package_id,
            )
        ).scalar_one_or_none()
        if not answer_file:
            return error_response("答卷图片不存在或无权访问", code=404, http_status_code=404)

    image_path = Path(settings.MCP_BASE_DIR) / (answer_file.file_path or "")
    if not image_path.exists() or not image_path.is_file():
        return error_response("答卷图片文件不存在", code=404, http_status_code=404)

    return FileResponse(str(image_path))


def _get_form_text(form, key, default=""):
    value = form.get(key)
    return (value or default).strip() if isinstance(value, str) else default


def _build_grading_payload_response(
    *,
    key,
    question_label,
    student_no,
    service_uuid,
    image_name,
    question_text,
    answer_text,
    max_score,
    result,
    status="success",
    error=None,
):
    payload = {
        "key": key,
        "question_label": question_label,
        "status": status,
        "student_no": student_no,
        "service_uuid": str(service_uuid),
        "image_name": image_name,
        "question_text": question_text,
        "answer_text": answer_text,
        "max_score": max_score,
        "result": result,
    }
    if error:
        payload["error"] = error
    return payload


async def grade_with_mcp(request: Request):
    """可选上传当前题目图片，并调用所选MCP的 evaluate_paper 完成评分。"""
    user, error = _current_teacher(request)
    if error:
        return error

    form = await request.form(max_fields=1000, max_part_size=64 * 1024 * 1024)
    service_uuid = form.get("service_uuid")
    student_no = (form.get("student_no") or "").strip()
    question_text = (form.get("question_text") or "").strip()
    answer_text = (form.get("answer_text") or "").strip()
    max_score = _normalize_max_score(form.get("max_score"))
    image = form.get("image")
    subject_id_value = _to_int_or_none(form.get("subject_id"))
    exam_id_value = _to_int_or_none(form.get("exam_id"))
    local_package_id = _normalize_local_package_id(form.get("local_package_id"))
    package_fingerprint = (form.get("package_fingerprint") or "").strip()
    quick_fingerprint = (form.get("quick_fingerprint") or "").strip()
    student_no_hash = (form.get("student_no_hash") or "").strip()
    question_no = (form.get("question_no") or "").strip()
    question_label = (form.get("question_label") or question_no or "").strip()
    grading_mode = (form.get("grading_mode") or "").strip()
    is_trial = str(form.get("is_trial") or "").lower() in {"1", "true", "yes"}
    standard_id = _to_int_or_none(form.get("standard_id"))
    standard_version_no = _to_int_or_none(form.get("standard_version_no"))
    question_version_no = _to_int_or_none(form.get("question_version_no"))
    template_id = _to_int_or_none(form.get("template_id"))
    template_name = (form.get("template_name") or "").strip()
    standard_label = (form.get("standard_label") or "").strip()

    if not service_uuid:
        return error_response("请选择MCP服务", code=400, http_status_code=400)
    if not student_no:
        return error_response("缺少学生ID", code=400, http_status_code=400)
    if not question_text:
        return error_response("缺少题目内容", code=400, http_status_code=400)
    safe_student_no = _safe_upload_name(student_no)
    relative_base_folder = (
        Path("media")
        / "teacher_scoring"
        / "temp_grading"
        / str(user["user_id"])
    )
    image_name = ""
    saved_image_path = ""
    if image and getattr(image, "filename", None):
        suffix = Path(image.filename).suffix.lower()
        if suffix not in IMAGE_SUFFIXES:
            suffix = ".png"
        image_name = f"{uuid4().hex}{suffix}"
        student_dir = Path(settings.MCP_BASE_DIR) / relative_base_folder / f"{safe_student_no}_KS"
        student_dir.mkdir(parents=True, exist_ok=True)
        image_path = student_dir / image_name
        image_path.write_bytes(await image.read())
        saved_image_path = str(image_path)

    with get_db() as db:
        service, module = _resolve_grading_service(db, service_uuid)
        if not module:
            return error_response("所选MCP不存在", code=404, http_status_code=400)
        # 大题级评分模块（如第四大题项目评分）需要遍历考生提交的文件目录
        grading_package = None
        if subject_id_value and exam_id_value and (local_package_id or package_fingerprint or quick_fingerprint or student_no_hash):
            grading_package, _ = await _resolve_owned_local_package_for_grading(
                db,
                user["user_id"],
                subject_id_value,
                exam_id_value,
                local_package_id,
                package_fingerprint=package_fingerprint,
                quick_fingerprint=quick_fingerprint,
                student_no_hash=student_no_hash,
            )
        student_answer_folder = _resolve_student_answer_folder(grading_package, student_no)
        # SQL 评分需要沙盒建表语句：优先取评分标准项 mcp_params 配置
        item_mcp_params = _load_standard_item_mcp_params(db, standard_id, question_no, question_version_no)

    try:
        result = _execute_evaluate_paper(
            module=module,
            service=service,
            student_no=safe_student_no,
            media_path=saved_image_path or image_name,
            base_media_folder=_select_grading_base_folder(
                Path(settings.MCP_BASE_DIR) / relative_base_folder,
                student_answer_folder, safe_student_no, image_name
            ),
            question_text=question_text,
            answer_text=answer_text,
            max_score=max_score,
            multi_service=str(form.get("multi_service") or "").lower() in {"1", "true", "yes"},
            answer_folder=student_answer_folder,
            setup_sql=_extract_setup_sql(question_text, item_mcp_params),
            adaptation_rules=item_mcp_params.get("adaptation_rules") if isinstance(item_mcp_params, dict) else None,
        )
        # 大题级评分服务返回整题结果：单题评阅只取本小问的得分点
        result = _extract_score_point_for_question(result, question_no, max_score)
    except Exception as exc:
        mcp_logger.error(f"MCP评分失败: {str(exc)}")
        return error_response(
            f"MCP评分失败: {str(exc)}",
            code=500,
            http_status_code=500
        )

    response_payload = {
        "status": "success",
        "student_no": student_no,
        "service_uuid": str(service_uuid),
        "image_name": image_name,
        "question_text": question_text,
        "answer_text": answer_text,
        "max_score": max_score,
        "question_no": question_no,
        "question_label": question_label,
        "grading_mode": grading_mode,
        "standard_id": standard_id,
        "standard_version_no": standard_version_no,
        "question_version_no": question_version_no,
        "template_id": template_id,
        "template_name": template_name,
        "standard_label": standard_label,
        "result": result,
    }

    if subject_id_value and exam_id_value and (local_package_id or package_fingerprint or quick_fingerprint or student_no_hash):
        try:
            with get_db() as db:
                local_package, save_lookup_message = await _resolve_owned_local_package_for_grading(
                    db,
                    user["user_id"],
                    subject_id_value,
                    exam_id_value,
                    local_package_id,
                    package_fingerprint=package_fingerprint,
                    quick_fingerprint=quick_fingerprint,
                    student_no_hash=student_no_hash,
                )
                if local_package:
                    record = _save_local_grading_record(
                        db,
                        user_id=user["user_id"],
                        subject_id=subject_id_value,
                        exam_id=exam_id_value,
                        local_package=local_package,
                        payload={
                            **response_payload,
                            "mcp_service_id": str(service_uuid),
                            "is_trial": is_trial,
                        },
                    )
                    db.commit()
                    response_payload["grading_record"] = _grading_record_to_dict(record)
                elif save_lookup_message:
                    response_payload["save_error"] = save_lookup_message
        except Exception as exc:
            mcp_logger.error(f"保存本地评分结果失败: {str(exc)}")
            response_payload["save_error"] = str(exc)

    return success_response(response_payload)

async def grade_batch_with_mcp(request: Request):
    """Batch grade multiple selected questions with per-question MCP config."""
    user, error = _current_teacher(request)
    if error:
        return error

    # 整卷批量评阅时 targets JSON 可达数十 MB、图片附件也可能超过 1MB，
    # 放宽 Starlette multipart 默认限制（单字段 1MB）
    form = await request.form(max_fields=50000, max_part_size=256 * 1024 * 1024)
    mode = _get_form_text(form, "mode", "batch-grade")
    subject_id_value = _to_int_or_none(form.get("subject_id"))
    exam_id_value = _to_int_or_none(form.get("exam_id"))
    local_package_id = _normalize_local_package_id(form.get("local_package_id"))
    package_fingerprint = (form.get("package_fingerprint") or "").strip()
    quick_fingerprint = (form.get("quick_fingerprint") or "").strip()
    student_no_hash = (form.get("student_no_hash") or "").strip()
    is_trial = str(form.get("is_trial") or "").lower() in {"1", "true", "yes"}
    standard_id = _to_int_or_none(form.get("standard_id"))
    standard_version_no = _to_int_or_none(form.get("standard_version_no"))
    question_version_no = _to_int_or_none(form.get("question_version_no"))
    template_id = _to_int_or_none(form.get("template_id"))
    template_name = (form.get("template_name") or "").strip()
    standard_label = (form.get("standard_label") or "").strip()
    targets_text = _get_form_text(form, "targets")
    if not targets_text:
        return error_response("缺少批量评阅题目", code=400, http_status_code=400)

    try:
        targets = json.loads(targets_text)
    except json.JSONDecodeError:
        return error_response("批量评阅题目格式不正确", code=400, http_status_code=400)

    if not isinstance(targets, list) or not targets:
        return error_response("请先选择需要批量评阅的题目", code=400, http_status_code=400)

    results = []
    service_cache = {}
    # 正式评阅的分数写回先攒起来，整批评完后一次性应用
    pending_student_json_updates: list[dict] = []
    local_package = None

    def _save_batch_record(**kwargs):
        record = _save_local_grading_record(db, apply_student_json=False, **kwargs)
        if record is not None and not record.is_trial:
            pending_student_json_updates.append({
                "student_no": record.student_no,
                "question_no": record.question_no,
                "score": record.score,
                "comment": record.grading_desc,
            })
        return record
    relative_base_folder = (
        Path("media")
        / "teacher_scoring"
        / "temp_grading"
        / str(user["user_id"])
    )
    absolute_base_folder = Path(settings.MCP_BASE_DIR) / relative_base_folder

    with get_db() as db:
        for index, target in enumerate(targets):
            if not isinstance(target, dict):
                results.append(_build_grading_payload_response(
                    key=f"target-{index + 1}",
                    question_label="未知题目",
                    student_no="",
                    service_uuid="",
                    image_name="",
                    question_text="",
                    answer_text="",
                    max_score=None,
                    result=None,
                    status="failed",
                    error="评阅题目无效",
                ))
                continue

            key = str(target.get("key") or f"target-{index + 1}")
            question_label = str(target.get("question_label") or key)
            question_no = str(target.get("question_no") or _question_no_from_label(question_label)).strip()
            service_uuid = target.get("service_uuid")
            student_no = str(target.get("student_no") or "").strip()
            question_text = str(target.get("question_text") or "").strip()
            answer_text = str(target.get("answer_text") or "").strip()
            max_score = _normalize_max_score(target.get("max_score"))
            sub_questions = target.get("sub_questions")
            if not isinstance(sub_questions, list):
                sub_questions = []
            image_field = str(target.get("image_field") or "")
            image = form.get(image_field) if image_field else None
            standard_id = _to_int_or_none(target.get("standard_id"))
            standard_version_no = _to_int_or_none(target.get("standard_version_no"))
            question_version_no = _to_int_or_none(target.get("question_version_no"))
            template_id = _to_int_or_none(target.get("template_id"))
            template_name = str(target.get("template_name") or "").strip()
            standard_label = str(target.get("standard_label") or "").strip()

            if not service_uuid or not student_no or not question_text:
                results.append(_build_grading_payload_response(
                    key=key,
                    question_label=question_label,
                    student_no=student_no,
                    service_uuid=service_uuid or "",
                    image_name="",
                    question_text=question_text,
                    answer_text=answer_text,
                    max_score=max_score,
                    result=None,
                    status="failed",
                    error="缺少 MCP 服务、学生编号或题目内容",
                ))
                continue
            try:
                cache_key = str(service_uuid)
                if cache_key not in service_cache:
                    service, module = _resolve_grading_service(db, service_uuid)
                    if not module:
                        raise ValueError("所选 MCP 服务不存在")
                    service_cache[cache_key] = (service, module)
                service, module = service_cache[cache_key]

                safe_student_no = _safe_upload_name(student_no)
                image_name = ""
                saved_image_path = ""
                if image and getattr(image, "filename", None):
                    suffix = Path(image.filename).suffix.lower()
                    if suffix not in IMAGE_SUFFIXES:
                        suffix = ".png"
                    image_name = f"{uuid4().hex}{suffix}"
                    student_dir = absolute_base_folder / f"{safe_student_no}_KS"
                    student_dir.mkdir(parents=True, exist_ok=True)
                    image_path = student_dir / image_name
                    image_path.write_bytes(await image.read())
                    saved_image_path = str(image_path)

                local_package = None
                if subject_id_value and exam_id_value and (local_package_id or package_fingerprint or quick_fingerprint or student_no_hash):
                    local_package, save_lookup_message = await _resolve_owned_local_package_for_grading(
                        db,
                        user["user_id"],
                        subject_id_value,
                        exam_id_value,
                        local_package_id,
                        package_fingerprint=package_fingerprint,
                        quick_fingerprint=quick_fingerprint,
                        student_no_hash=student_no_hash,
                    )
                else:
                    save_lookup_message = "缺少 subject_id、exam_id、本地试卷包ID或指纹，无法保存本地评分记录"

                # 大题级评分模块（如第四大题项目评分）需要遍历考生提交的文件目录
                student_answer_folder = _resolve_student_answer_folder(local_package, student_no)

                # 大题各小题的作答截图（如第二大题的关系图/表设计截图）
                topic_index = None
                try:
                    topic_index = int(str(question_no).split(".", 1)[0]) - 1
                except (TypeError, ValueError):
                    topic_index = None
                student_answer_images = _resolve_student_answer_images(
                    local_package, student_no, topic_index
                )

                # 未作答兜底：小题级目标既无文本答案、又无上传图片、也无作答截图、
                # 且考生答案目录为空时，直接记 0 分，不再调用评分服务（避免大模型凭参考答案给空卷送分）。
                # 大题级目标（题号不带小数点，如第四大题项目评分）依赖答案目录扫描，不做此拦截。
                # 文件作答类小题（.sesx/.fla/.max/Unity 工程等）答案在考生目录中，目录有内容则放行，
                # 由评分服务自行定位文件并按文件类型判分（找不到对应文件时服务自身会记 0 并说明）。
                normalized_target_question_no = _normalize_result_question_no(question_no)
                has_uploaded_image = image is not None and getattr(image, "filename", None)
                sub_answer_images = (student_answer_images or {}).get(normalized_target_question_no) or []
                has_answer_folder_files = False
                if student_answer_folder:
                    try:
                        with os.scandir(student_answer_folder) as entries:
                            has_answer_folder_files = any(True for _ in entries)
                    except OSError:
                        has_answer_folder_files = False
                if "." in normalized_target_question_no and not answer_text.strip() and not has_uploaded_image and not sub_answer_images and not has_answer_folder_files:
                    result = {
                        "total_score": 0,
                        "max_score": max_score,
                        "grading_desc": "未作答：未提交答案文本或截图，记0分",
                        "comment": "未作答：未提交答案文本或截图，记0分",
                    }
                else:
                    effective_question_text = _append_major_question_return_rule(question_text, sub_questions)
                    # SQL 评分需要沙盒建表语句：优先取评分标准项 mcp_params 配置
                    item_mcp_params = _load_standard_item_mcp_params(
                        db, standard_id, question_no, question_version_no
                    )
                    setup_sql = _extract_setup_sql(effective_question_text, item_mcp_params)
                    result = _execute_evaluate_paper(
                        module=module,
                        service=service,
                        student_no=safe_student_no,
                        media_path=image_name,
                        base_media_folder=_select_grading_base_folder(
                            absolute_base_folder, student_answer_folder, safe_student_no, image_name
                        ),
                        question_text=effective_question_text,
                        answer_text=answer_text,
                        max_score=max_score,
                        multi_service=str(target.get("multi_service") or "").lower() in {"1", "true", "yes", True},
                        answer_folder=student_answer_folder,
                        answer_images=student_answer_images,
                        setup_sql=setup_sql,
                        adaptation_rules=item_mcp_params.get("adaptation_rules") if isinstance(item_mcp_params, dict) else None,
                    )

                if sub_questions:
                    extracted_subs = []
                    for sub_index, sub_question in enumerate(sub_questions):
                        if not isinstance(sub_question, dict):
                            continue
                        sub_question_no = str(sub_question.get("question_no") or "").strip()
                        sub_result = _extract_sub_question_result(
                            result,
                            sub_question_no,
                            index=sub_index,
                            total_count=len(sub_questions),
                        )
                        extracted_subs.append((sub_question, sub_result))

                    def _has_sub_breakdown(sub_result):
                        return not (isinstance(sub_result, dict) and sub_result.get("extract_error"))

                    # 大题级评分服务（如创意设计海报评分）对整道大题返回一个
                    # 总分和得分点明细，没有按小题的细分。此时不得把大题总分
                    # 复制到每个小题（会导致小题分数相加、总分翻倍），
                    # 只在大题层级保存一条记录，前端按大题分数展示。
                    if extracted_subs and not any(_has_sub_breakdown(item[1]) for item in extracted_subs):
                        response_item = _build_grading_payload_response(
                            key=key,
                            question_label=question_label,
                            student_no=student_no,
                            service_uuid=service_uuid,
                            image_name=image_name,
                            question_text=question_text,
                            answer_text=answer_text,
                            max_score=max_score,
                            result=result,
                        )
                        response_item["question_no"] = question_no
                        response_item["standard_id"] = standard_id
                        response_item["standard_version_no"] = standard_version_no
                        response_item["question_version_no"] = question_version_no
                        response_item["template_id"] = template_id
                        response_item["template_name"] = template_name
                        response_item["standard_label"] = standard_label
                        response_item["whole_question_result"] = True
                        if local_package:
                            try:
                                record = _save_batch_record(
                                    user_id=user["user_id"],
                                    subject_id=subject_id_value,
                                    exam_id=exam_id_value,
                                    local_package=local_package,
                                    payload={
                                        **response_item,
                                        "mcp_service_id": str(service_uuid),
                                        "grading_mode": mode,
                                        "is_trial": is_trial,
                                    },
                                )
                                response_item["grading_record"] = _grading_record_to_dict(record)
                            except Exception as save_exc:
                                mcp_logger.error(f"保存试阅评分记录失败: key={key}, error={str(save_exc)}")
                                response_item["save_error"] = f"保存评分记录失败: {str(save_exc)}"
                        elif save_lookup_message:
                            response_item["save_error"] = save_lookup_message
                        results.append(response_item)
                    else:
                        for sub_question, sub_result in extracted_subs:
                            sub_question_no = str(sub_question.get("question_no") or "").strip()
                            sub_question_label = str(sub_question.get("question_label") or sub_question_no or question_label).strip()
                            sub_max_score = _normalize_max_score(sub_question.get("max_score"))
                            # 小题分数不得超过该小题满分：评分服务内部评分标准
                            # 与前端评分标准不一致时会返回超分，此处按小题满分截断
                            sub_result = _clamp_score_result(
                                sub_result,
                                sub_max_score if sub_max_score is not None else max_score,
                            )
                            response_item = _build_grading_payload_response(
                                key=str(sub_question.get("key") or f"{key}__sub{sub_question.get('question_no') or ''}"),
                                question_label=sub_question_label,
                                student_no=student_no,
                                service_uuid=service_uuid,
                                image_name=image_name,
                                question_text=str(sub_question.get("question_text") or "").strip() or question_text,
                                answer_text=str(sub_question.get("answer_text") or "").strip(),
                                max_score=sub_max_score,
                                result=sub_result,
                            )
                            response_item["question_no"] = sub_question_no
                            response_item["standard_id"] = _to_int_or_none(sub_question.get("standard_id")) or standard_id
                            response_item["standard_version_no"] = _to_int_or_none(sub_question.get("standard_version_no")) or standard_version_no
                            response_item["question_version_no"] = _to_int_or_none(sub_question.get("question_version_no")) or question_version_no
                            response_item["template_id"] = _to_int_or_none(sub_question.get("template_id")) or template_id
                            response_item["template_name"] = str(sub_question.get("template_name") or template_name or "").strip()
                            response_item["standard_label"] = str(sub_question.get("standard_label") or standard_label or "").strip()
                            response_item["major_question_no"] = question_no
                            response_item["major_result"] = result
                            if local_package:
                                try:
                                    record = _save_batch_record(
                                        user_id=user["user_id"],
                                        subject_id=subject_id_value,
                                        exam_id=exam_id_value,
                                        local_package=local_package,
                                        payload={
                                            **response_item,
                                            "mcp_service_id": str(service_uuid),
                                            "grading_mode": mode,
                                            "is_trial": is_trial,
                                        },
                                    )
                                    response_item["grading_record"] = _grading_record_to_dict(record)
                                except Exception as save_exc:
                                    mcp_logger.error(f"保存试阅评分记录失败: key={response_item.get('key')}, error={str(save_exc)}")
                                    response_item["save_error"] = f"保存评分记录失败: {str(save_exc)}"
                            elif save_lookup_message:
                                response_item["save_error"] = save_lookup_message
                            results.append(response_item)
                else:
                    response_item = _build_grading_payload_response(
                        key=key,
                        question_label=question_label,
                        student_no=student_no,
                        service_uuid=service_uuid,
                        image_name=image_name,
                        question_text=question_text,
                        answer_text=answer_text,
                        max_score=max_score,
                        # 大题级评分服务返回整题结果：单目标评阅只取本小问的得分点
                        result=_extract_score_point_for_question(result, question_no, max_score),
                    )
                    response_item["question_no"] = question_no
                    response_item["standard_id"] = standard_id
                    response_item["standard_version_no"] = standard_version_no
                    response_item["question_version_no"] = question_version_no
                    response_item["template_id"] = template_id
                    response_item["template_name"] = template_name
                    response_item["standard_label"] = standard_label
                    if local_package:
                        try:
                            record = _save_batch_record(
                                user_id=user["user_id"],
                                subject_id=subject_id_value,
                                exam_id=exam_id_value,
                                local_package=local_package,
                                payload={
                                    **response_item,
                                    "mcp_service_id": str(service_uuid),
                                    "grading_mode": mode,
                                    "is_trial": is_trial,
                                },
                            )
                            response_item["grading_record"] = _grading_record_to_dict(record)
                        except Exception as save_exc:
                            mcp_logger.error(f"保存本地评分记录失败: key={key}, error={str(save_exc)}")
                            response_item["save_error"] = f"保存评分记录失败: {str(save_exc)}"
                    elif save_lookup_message:
                        response_item["save_error"] = save_lookup_message
                    results.append(response_item)
            except Exception as exc:
                mcp_logger.error(f"批量 MCP 评分失败: key={key}, error={str(exc)}")
                results.append(_build_grading_payload_response(
                    key=key,
                    question_label=question_label,
                    student_no=student_no,
                    service_uuid=service_uuid,
                    image_name="",
                    question_text=question_text,
                    answer_text=answer_text,
                    max_score=max_score,
                    result=None,
                    status="failed",
                    error=f"MCP 评分失败: {str(exc)}",
                ))

        try:
            if local_package_id and subject_id_value and exam_id_value:
                # 正式评阅的分数写回：整批结束后统一应用到学生 JSON，
                # 避免每条记录整体重写 parsed_students（3MB+）导致 GB 级写放大
                if pending_student_json_updates and local_package is not None:
                    for update in pending_student_json_updates:
                        try:
                            _set_formal_score_on_student_json(
                                local_package,
                                student_no=update["student_no"],
                                question_no=update["question_no"],
                                score=update["score"],
                                comment=update["comment"],
                            )
                        except Exception as update_exc:
                            mcp_logger.error(
                                f"写回考生 JSON 分数失败: student_no={update.get('student_no')}, "
                                f"question_no={update.get('question_no')}, error={str(update_exc)}"
                            )
                db.commit()
        except Exception as exc:
            mcp_logger.error(f"保存批量本地评分结果失败: {str(exc)}")
            db.rollback()
    success_count = len([item for item in results if item.get("status") != "failed"])
    failed_count = len(results) - success_count
    return success_response({
        "status": "success" if failed_count == 0 else "partial_success",
        "mode": mode,
        "total": len(results),
        "success_count": success_count,
        "failed_count": failed_count,
        "results": results,
    })


async def package_detail(request: Request):
    if request.method == "GET":
        return await get_package(request)
    if request.method == "DELETE":
        return await delete_package(request)

    return error_response("请求方法不支持", code=405, http_status_code=405)


async def subject_exams(request: Request):
    if request.method == "GET":
        return await list_exams(request)
    if request.method == "POST":
        return await create_exam(request)

    return error_response("请求方法不支持", code=405, http_status_code=405)


def get_router():
    return [
        Route("/subjects", list_subjects, methods=["GET"]),
        Route("/subjects", create_subject, methods=["POST"]),
        Route("/exams", list_all_exams, methods=["GET"]),
        Route("/grade-with-mcp", grade_with_mcp, methods=["POST"]),
        Route("/grade-batch-with-mcp", grade_batch_with_mcp, methods=["POST"]),
        Route("/packages", list_all_packages, methods=["GET"]),
        Route("/grading-tasks", grading_tasks, methods=["GET", "POST"]),
        Route("/subjects/{subject_id}/exams/{exam_id}/local-packages/process", process_local_package, methods=["POST"]),
        Route("/subjects/{subject_id}/exams/{exam_id}/local-packages", register_local_package, methods=["POST"]),
        Route("/subjects/{subject_id}/exams/{exam_id}/local-packages/{local_package_id}/reference-answers/import", import_local_package_reference_answers, methods=["POST"]),
        Route("/subjects/{subject_id}/exams/{exam_id}/local-packages/{local_package_id}", local_package_detail, methods=["GET", "DELETE"]),
        Route("/subjects/{subject_id}/exams/{exam_id}/local-packages/{local_package_id}/structure", get_local_package_structure, methods=["GET"]),
        Route("/subjects/{subject_id}/exams/{exam_id}/local-packages/{local_package_id}/files", get_local_package_file, methods=["GET"]),
        Route("/subjects/{subject_id}/exams/{exam_id}/local-packages/{local_package_id}/grading-records", local_grading_records, methods=["GET", "POST"]),
        Route("/subjects/{subject_id}/exams/{exam_id}/local-packages/{local_package_id}/grading-records/round-update", update_local_grading_round, methods=["POST"]),
        Route("/subjects/{subject_id}/exams/{exam_id}/local-packages/{local_package_id}/grading-records/batch-delete", delete_local_grading_records, methods=["POST"]),
        Route("/subjects/{subject_id}/exams/{exam_id}/packages/{package_id}/answers/{answer_id}/files/{file_id}", get_answer_image, methods=["GET"]),
        Route("/subjects/{subject_id}/exams/{exam_id}/packages/{package_id}/answers/{answer_id}/files", list_answer_files, methods=["GET"]),
        Route("/subjects/{subject_id}/exams/{exam_id}/packages/{package_id}/answers", list_answer_sheets, methods=["GET"]),
        Route("/subjects/{subject_id}/exams/{exam_id}/packages/{package_id}", package_detail, methods=["GET", "DELETE"]),
        Route("/subjects/{subject_id}/exams/{exam_id}/packages", exam_packages, methods=["GET", "POST"]),
        Route("/subjects/{subject_id}/exams/{exam_id}/grading-standards/versions", list_grading_standard_versions, methods=["GET"]),
        Route("/subjects/{subject_id}/exams/{exam_id}/grading-standards/{standard_id}", update_grading_standard, methods=["PUT"]),
        Route("/subjects/{subject_id}/exams/{exam_id}/grading-standards/{standard_id}", delete_grading_standard, methods=["DELETE"]),
        Route("/subjects/{subject_id}/exams/{exam_id}/grading-templates/{template_id}", update_grading_template, methods=["PUT"]),
        Route("/subjects/{subject_id}/exams/{exam_id}/grading-templates/{template_id}", delete_grading_template, methods=["DELETE"]),
        Route("/subjects/{subject_id}/exams/{exam_id}/grading-templates/{template_id}/copy", copy_grading_template, methods=["POST"]),
        Route("/subjects/{subject_id}/exams/{exam_id}/grading-templates", exam_grading_templates, methods=["GET", "POST"]),
        Route("/subjects/{subject_id}/exams/{exam_id}/grading-standards", exam_grading_standards, methods=["GET", "POST"]),
        Route("/subjects/{subject_id}/exams/{exam_id}", exam_detail, methods=["GET", "PUT", "DELETE"]),
        Route("/subjects/{subject_id}/exams", subject_exams, methods=["GET", "POST"]),
        Route("/subjects/{subject_id}", subject_detail, methods=["GET", "PUT", "DELETE"]),
    ]
