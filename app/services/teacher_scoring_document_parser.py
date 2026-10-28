"""教师评分试卷文档转换器。

这里保留了试卷包转换所需的核心逻辑，输入可以是 doc/docx、已有 JSON 和图片。
转换结果是普通 JSON 数据，调用方负责把结果和生成的图片保存到试卷包目录。
"""

from __future__ import annotations

from copy import deepcopy
from collections import Counter
import hashlib
import json
import logging
import re
import time
import zipfile
import xml.etree.ElementTree as ET
from http import HTTPStatus
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Iterable

from app.services.file_role_recognition import (
    DATABASE_ANSWER_KEYWORDS,
    DATABASE_ANSWER_SUFFIXES,
    EXCEL_ANSWER_SUFFIXES,
    IMAGE_SUFFIXES,
    STUDENT_ID_PATTERN,
    WORD_SUFFIXES,
    answer_file_category as _answer_file_category,
    is_database_answer_file as _database_answer_file,
    is_ignored_attachment,
    is_temporary_word_file,
)

logger = logging.getLogger("mcp")


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

# 后缀/关键词/学号正则与作答文件分类已收敛至 file_role_recognition（第二步改造），
# 此处经顶部 import 继续以原名称暴露（teacher_scoring.py 等调用方不受影响）。
EXCEL_QUESTION_KEYWORDS = ("excel", "xlsx", "xls", "工作簿", "电子表格", "表格")
DATABASE_QUESTION_KEYWORDS = (
    "数据库",
    "备份",
    "还原",
    "恢复",
    "sql",
    "database",
    "db",
    "mdf",
    "ldf",
    "bak",
    "dmp",
    "dump",
)
RE_PART = re.compile(r"^（[一二三四五六七八九十]+）")
RE_QUESTION = re.compile(r"^(\d+)\s*[.、．]")
RE_SUBQ = re.compile(r"^（(\d+)）")
RE_GROUP = re.compile(r"^【.+】")
RE_FOOTER = re.compile(r"第\s*\d+\s*页")
RE_SKIP_NOISE = re.compile(r"^SHAPE\s*\*|^MERGEFORMAT")
ANSWER_MARKER = re.compile(r"参考答案|答案\s*[:：]|^答案\s*\d|^答案\b")
SCORE_MARKER = re.compile(
    r"^(分值|采分点|本题|第[一二三四五六七八九十\d]+小题|注意|评阅|涉及题目|数据库管理系统)\b|"
    r"每(空|小题|题|类|错|少)|适当扣分|起扣|默认给分|（\d+(?:\.\d+)?分）|共\s*\d+\s*分"
)
LLM_INPUT_MAX_CHARS = 60000


def _validate_docx_file(path: Path) -> None:
    """校验 .docx 是否真的是包含主文档的 OOXML 压缩包。"""
    try:
        with zipfile.ZipFile(path) as archive:
            if "word/document.xml" not in archive.namelist():
                raise ValueError(
                    f"文档“{path.name}”缺少 word/document.xml，不是有效的 DOCX 文件"
                )
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise ValueError(
            f"学生文档“{path.name}”不是有效的 DOCX 文件，请重新导出或上传 .doc 文件"
        ) from exc


def _paragraph_text(paragraph, parents: dict | None = None) -> str:
    """拼接段落内全部 w:t 文本。

    parents 提供（元素->父级）映射时，跳过 mc:Fallback 内的文本：
    .doc 转换出的 .docx 中，文本框内容在 AlternateContent 的 Choice 和
    Fallback 里各存一份，全量 iter 会把同一段作答提取两遍。
    """
    if parents is None:
        return "".join(node.text or "" for node in paragraph.iter(W + "t"))
    pieces = []
    for node in paragraph.iter(W + "t"):
        element = node
        duplicated = False
        while element is not None:
            if element is not paragraph and element.tag == MC + "Fallback":
                duplicated = True
                break
            element = parents.get(element)
        if not duplicated:
            pieces.append(node.text or "")
    return "".join(pieces)


class _DocxReader:
    def __init__(self, path: Path):
        self.archive = zipfile.ZipFile(path)
        self.document = ET.fromstring(self.archive.read("word/document.xml"))
        self.parents = {child: parent for parent in self.document.iter() for child in parent}
        rel_ns = "{http://schemas.openxmlformats.org/package/2006/relationships}"
        try:
            rel_xml = ET.fromstring(self.archive.read("word/_rels/document.xml.rels"))
        except KeyError:
            rel_xml = None
        self.relations = {
            item.get("Id"): item.get("Target")
            for item in (rel_xml.findall(rel_ns + "Relationship") if rel_xml is not None else [])
        }

    def close(self) -> None:
        self.archive.close()

    def in_fallback(self, element) -> bool:
        while element in self.parents:
            element = self.parents[element]
            if element.tag == MC + "Fallback":
                return True
        return False

    def blocks(self):
        body = self.document.find(W + "body")
        if body is None:
            return
        for element in body:
            tag = element.tag.split("}")[-1]
            if tag in {"p", "tbl"}:
                yield tag, element

    def paragraph_images(self, paragraph) -> list[str]:
        result = []
        for blip in paragraph.iter(A + "blip"):
            if self.in_fallback(blip):
                continue
            relation_id = blip.get(R + "embed")
            if relation_id:
                result.append(relation_id)
        return result

    def paragraph_boxes(self, paragraph) -> list[list[str]]:
        result = []
        for box in paragraph.iter(W + "txbxContent"):
            if self.in_fallback(box):
                continue
            result.append([_paragraph_text(item, self.parents) for item in box.iter(W + "p")])
        return result

    def image_data(self, relation_id: str) -> tuple[bytes | None, str]:
        target = self.relations.get(relation_id, "")
        archive_name = target if target.startswith("word/") else "word/" + target.lstrip("/")
        try:
            data = self.archive.read(archive_name)
        except KeyError:
            return None, ".png"
        return data, Path(target).suffix or ".png"

    def table_text(self, table) -> str:
        rows = []
        for row in table.iter(W + "tr"):
            cells = []
            for cell in row.iter(W + "tc"):
                text = "\n".join(
                    _paragraph_text(paragraph, self.parents).strip()
                    for paragraph in cell.iter(W + "p")
                    if _paragraph_text(paragraph, self.parents).strip()
                )
                if text:
                    cells.append(text)
            if cells:
                rows.append(" | ".join(cells))
        return "\n".join(rows)


def _convert_docs_to_docx(paths: Iterable[Path]) -> dict[Path, Path]:
    doc_paths = [
        path
        for path in paths
        if path.suffix.lower() == ".doc" and not is_temporary_word_file(path)
    ]
    if not doc_paths:
        return {}
    try:
        import win32com.client  # type: ignore
    except ImportError as exc:
        raise RuntimeError("解析 .doc 文件需要 Windows Microsoft Word 或 pywin32") from exc

    converted: dict[Path, Path] = {}
    word = win32com.client.Dispatch("Word.Application")
    word.Visible = False
    word.DisplayAlerts = 0
    try:
        for path in doc_paths:
            with NamedTemporaryFile(suffix=".docx", delete=False) as temporary_file:
                temporary = Path(temporary_file.name)
            document = None
            try:
                document = word.Documents.Open(str(path.resolve()), ReadOnly=True)
                document.SaveAs2(str(temporary), FileFormat=16)
                document.Close(False)
                document = None
                _validate_docx_file(temporary)
                converted[path] = temporary
            except Exception as exc:
                # 个别 .doc 损坏或被占用时跳过该考生，避免中止整批。
                logger.warning(f"跳过转换失败的 .doc 学生文档“{path}”：{exc}")
            finally:
                if document is not None:
                    document.Close(False)
                if path not in converted and temporary.exists():
                    temporary.unlink()
    except Exception:
        for temporary in converted.values():
            if temporary.exists():
                temporary.unlink()
        raise
    finally:
        word.Quit()
    return converted


def _convert_doc_to_docx(path: Path) -> tuple[Path, Path | None]:
    if path.suffix.lower() == ".docx":
        _validate_docx_file(path)
        return path, None
    try:
        import win32com.client  # type: ignore
    except ImportError as exc:
        raise RuntimeError("解析 .doc 文件需要 Windows Microsoft Word 或 pywin32") from exc

    with NamedTemporaryFile(suffix=".docx", delete=False) as temporary_file:
        temp_file = Path(temporary_file.name)
    word = win32com.client.Dispatch("Word.Application")
    word.Visible = False
    word.DisplayAlerts = 0
    document = None
    try:
        document = word.Documents.Open(str(path.resolve()), ReadOnly=True)
        document.SaveAs2(str(temp_file), FileFormat=16)
        document.Close(False)
        document = None
        _validate_docx_file(temp_file)
    except Exception:
        if temp_file.exists():
            temp_file.unlink()
        raise
    finally:
        if document is not None:
            document.Close(False)
        word.Quit()
    return temp_file, temp_file


def _split_answer_box(lines: Iterable[str]) -> tuple[str, str]:
    answers: list[str] = []
    criteria: list[str] = []
    bucket = criteria
    for line in lines:
        text = str(line or "").strip()
        if not text or re.match(r"^(答案|采分点)\d*$", text):
            continue
        if ANSWER_MARKER.search(text) and not re.search(r"采分点|分值", text):
            bucket = answers
        elif SCORE_MARKER.search(text) or re.match(r"^[\d.\s]+$", text):
            bucket = criteria
        bucket.append(text)
    return "\n".join(answers), "\n".join(criteria)


def _parse_reference_document(
    path: Path,
    image_dir: Path,
    image_prefix: str = "answer",
    source_lines: list[str] | None = None,
) -> dict[str, Any]:
    docx_path, temporary = _convert_doc_to_docx(path)
    reader = _DocxReader(docx_path)
    root: dict[str, Any] = {"文档": path.stem, "大题": []}
    current_part = None
    current_question = None
    current_subquestion = None
    group = None
    image_index = 0

    def current_target():
        return current_subquestion or current_question or current_part

    def save_image(relation_id: str) -> str | None:
        nonlocal image_index
        data, suffix = reader.image_data(relation_id)
        if data is None:
            return None
        image_index += 1
        image_dir.mkdir(parents=True, exist_ok=True)
        file_name = f"{image_prefix}_{image_index:03d}{suffix}"
        target = image_dir / file_name
        target.write_bytes(data)
        return file_name

    try:
        for tag, element in reader.blocks():
            if tag == "tbl":
                text = reader.table_text(element).strip()
                if source_lines is not None and text:
                    source_lines.append(text)
                if text and current_question is not None:
                    (current_subquestion or current_question)["题干"] += "\n" + text
                continue

            text = _paragraph_text(element, reader.parents).strip()
            boxes = reader.paragraph_boxes(element)
            relation_ids = reader.paragraph_images(element)
            if source_lines is not None:
                if text:
                    source_lines.append(text)
                for box_lines in boxes:
                    source_lines.extend(
                        str(line).strip()
                        for line in box_lines
                        if str(line).strip()
                    )
                if relation_ids:
                    source_lines.append(
                        f"[文档图片: {', '.join(relation_ids)}]"
                    )
            target = current_target()

            for relation_id in relation_ids:
                file_name = save_image(relation_id)
                if file_name and target is not None:
                    target.setdefault("参考答案图片", []).append(file_name)

            if boxes:
                box_target = current_question
                for lines in boxes:
                    first_line = (lines[0] if lines else "").strip()
                    match = RE_QUESTION.match(first_line)
                    if not match or current_question is None or match.group(1) != str(current_question["编号"]):
                        box_target = current_target()
                    if box_target is None:
                        continue
                    answer, criteria = _split_answer_box(lines)
                    if answer:
                        box_target["参考答案"] = (
                            box_target.get("参考答案", "") + "\n" + answer
                        ).strip()
                    if criteria:
                        box_target["评分情况"] = (
                            box_target.get("评分情况", "") + "\n" + criteria
                        ).strip()
                continue

            if not text or RE_SKIP_NOISE.match(text) or RE_FOOTER.search(text):
                continue
            if RE_PART.match(text):
                current_part = {"标题": text, "小题": []}
                root["大题"].append(current_part)
                current_question = None
                current_subquestion = None
                group = None
                continue
            if current_part is None:
                continue
            if RE_GROUP.match(text):
                # 分组保持归属当前大题（与考生卷解析一致），组标题记入分组，
                # 小题连续编号与评分标准对齐。
                group = text
                current_question = None
                current_subquestion = None
                continue
            match = RE_QUESTION.match(text)
            if match:
                current_question = {"编号": match.group(1), "题干": text}
                if group:
                    current_question["分组"] = group
                current_part["小题"].append(current_question)
                current_subquestion = None
                continue
            match = RE_SUBQ.match(text)
            if match and current_question is not None:
                current_subquestion = {"编号": f"（{match.group(1)}）", "题干": text}
                current_question.setdefault("子小题", []).append(current_subquestion)
                continue
            if current_question is not None:
                (current_subquestion or current_question)["题干"] += "\n" + text
            else:
                current_part["说明"] = (
                    current_part.get("说明", "") + "\n" + text
                ).strip()
    finally:
        reader.close()
        if temporary and temporary.exists():
            temporary.unlink()
    return root


def _student_id(path: Path, paper: str) -> str:
    stem = path.stem
    match = re.search(r"(\d{6,}[A-Za-z]\d+)", stem)
    if not match:
        parent = path.parent
        while parent != parent.parent:
            match = re.search(r"(\d{6,}[A-Za-z]\d+)", parent.name)
            if match:
                stem = match.group(1)
                break
            parent = parent.parent
    if stem.upper().startswith(paper.upper()):
        return stem
    return paper + stem


def _convert_image_to_png(source: Path) -> bool:
    """用 Word 把 WMF/EMF 矢量图转成浏览器可显示的 PNG。

    考生从 Access 等程序直接粘贴的关系图截图常以 WMF 内嵌在 Word 里，
    浏览器无法渲染该格式，评分页会显示为空白。
    刚结束 .doc 批量转换时 Word 进程可能尚未完全退出，这里带重试。
    """
    try:
        import win32com.client  # type: ignore
    except ImportError:
        return False
    target = source.with_suffix(".png")
    for attempt in range(3):
        word = None
        document = None
        try:
            word = win32com.client.Dispatch("Word.Application")
            word.Visible = False
            word.DisplayAlerts = 0
            document = word.Documents.Open(str(source.resolve()), ReadOnly=True)
            document.SaveAs2(str(target.resolve()), FileFormat=18)
            return target.is_file() and target.stat().st_size > 0
        except Exception as exc:
            if attempt == 2:
                logger.warning(f"WMF 图片转换 PNG 失败“{source.name}”：{exc}")
            time.sleep(2)
        finally:
            for closer in (lambda: document.Close(False), lambda: word.Quit()):
                try:
                    closer()
                except Exception:
                    pass
    return False


def _parse_student_document(
    path: Path,
    student_id: str,
    media_root: Path,
    docx_path: Path | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    resolved_docx, temporary = (
        (docx_path, None) if docx_path is not None else _convert_doc_to_docx(path)
    )
    reader = _DocxReader(resolved_docx)
    student_dir = media_root / f"{student_id}_KS"
    student_dir.mkdir(parents=True, exist_ok=True)
    images: list[dict[str, Any]] = []
    image_count = 0
    parts: list[dict[str, Any]] = []
    current_part = None
    current_question = None
    prefixes: list[str] = []
    fallback_pieces: list[str] = []
    fallback_images: list[str] = []
    seen_image_digests: set[str] = set()
    started = False

    def ensure_implicit_part() -> None:
        nonlocal current_part, started
        if current_part is None:
            current_part = {
                "_q": ["考生作答"],
                "question": "",
                "answer": "",
                "images": [],
                "score": 0,
                "comment": "",
                "sub_question": [],
            }
            parts.append(current_part)
        started = True

    def image_ref(relation_id: str, dedup_duplicate: bool = False) -> str | None:
        nonlocal image_count
        data, suffix = reader.image_data(relation_id)
        if data is None:
            return None
        # 考生文档里可能出现"游离的重复图"：同一张截图贴了两遍，第二份孤零零
        # 落在最后一个小题题干之后、新大题标题之前，按文档顺序会被误挂到别的小题
        # （如 1.3 的图重复出现在 2.5 下）。仅在这种"紧邻新大题标题"的位置，
        # 与前文字节级重复的图片才被丢弃；其它位置的重复图可能是有意贴给
        # 多个小题的得分依据，保持原样。
        digest = hashlib.md5(data).hexdigest()
        if dedup_duplicate and digest in seen_image_digests:
            return None
        seen_image_digests.add(digest)
        image_count += 1
        suffix = suffix.lower()
        if suffix in {".wmf", ".emf"}:
            # 先落盘原格式再转 PNG；转换失败时保留原文件（至少可下载查看）。
            temporary_image = student_dir / f"image{image_count}{suffix}"
            temporary_image.write_bytes(data)
            if _convert_image_to_png(temporary_image):
                temporary_image.unlink(missing_ok=True)
                data = None
                suffix = ".png"
        name = f"image{image_count}{suffix}"
        target = student_dir / name
        if data is not None:
            target.write_bytes(data)
        relative = f"media/{student_id}_KS/{name}"
        images.append(
            {
                "name": name,
                "relative_path": relative,
                "storage_path": str(target),
                "file_type": suffix.lstrip(".").lower(),
                "file_size": target.stat().st_size,
            }
        )
        return relative

    try:
        all_blocks = list(reader.blocks())

        def _block_text(tag: str, element) -> str:
            if tag == "p":
                return _paragraph_text(element, reader.parents).strip()
            return reader.table_text(element).strip()

        def _trailing_before_part(index: int) -> bool:
            """当前块之后（跳过空文本块）紧邻新大题标题时返回 True。"""
            for j in range(index + 1, min(index + 4, len(all_blocks))):
                text = _block_text(*all_blocks[j])
                if not text:
                    continue
                return bool(RE_PART.match(text))
            return False

        for index, (tag, element) in enumerate(all_blocks):
            if tag == "p":
                refs = [
                    image_ref(item, dedup_duplicate=_trailing_before_part(index))
                    for item in reader.paragraph_images(element)
                ]
                refs = [item for item in refs if item]
                text = _paragraph_text(element, reader.parents).strip()
            else:
                refs = []
                text = reader.table_text(element).strip()
            if text and (RE_SKIP_NOISE.match(text) or RE_FOOTER.search(text)):
                text = ""
            placeholders = [f"----{item}----" for item in refs]

            if not started:
                if RE_PART.match(text):
                    started = True
                elif RE_QUESTION.match(text):
                    ensure_implicit_part()
                else:
                    for piece in ([text] if text else []) + placeholders:
                        fallback_pieces.append(piece)
                        if piece.startswith("----"):
                            fallback_images.append(piece.strip("-"))
                    continue
            if RE_PART.match(text):
                current_part = {
                    "_q": [text],
                    "question": "",
                    "answer": "",
                    "images": [],
                    "score": 0,
                    "comment": "",
                    "sub_question": [],
                }
                parts.append(current_part)
                current_part.setdefault("_blocks", []).append({"text": text, "images": refs})
                current_question = None
                prefixes = []
                for item in placeholders:
                    current_part["_q"].append(item)
                    current_part["images"].append(item.strip("-"))
                continue
            if current_part is None:
                ensure_implicit_part()
            # 记录原始文本块，供大模型修复“题干/作答错位”时使用。
            current_part.setdefault("_blocks", []).append({"text": text, "images": refs})
            if RE_GROUP.match(text):
                if current_part["sub_question"]:
                    # 【…】分组（如“KES Plus 等低代码工具”备选组）仍归属当前大题，
                    # 组标题并入下一小题题干，保证小题连续编号（4.1-4.9）与评分标准对齐。
                    prefixes = [text]
                    current_question = None
                else:
                    current_part["_q"].append(text)
                continue
            match = RE_QUESTION.match(text)
            if match:
                current_question = {
                    "question": "\n\n".join(prefixes + [text]),
                    "answer": "",
                    "images": [],
                    "score": 0,
                    "comment": "",
                    "sub_question": [],
                }
                current_part["sub_question"].append(current_question)
                prefixes = []
                for item in placeholders:
                    current_question["answer"] += (
                        "\n\n" if current_question["answer"] else ""
                    ) + item
                    current_question["images"].append(item.strip("-"))
                continue
            pieces = ([text] if text else []) + placeholders
            if current_question is not None:
                for piece in pieces:
                    current_question["answer"] += (
                        "\n\n" if current_question["answer"] else ""
                    ) + piece
                    if piece.startswith("----"):
                        current_question["images"].append(piece.strip("-"))
            else:
                current_part["_q"].extend(pieces)
                current_part["images"].extend(
                    piece.strip("-") for piece in placeholders
                )
    finally:
        reader.close()
        if temporary and temporary.exists():
            temporary.unlink()

    for part in parts:
        part["question"] = "\n\n".join(item for item in part.pop("_q") if item)
        part["answer"] = ""
        for question in part["sub_question"]:
            question["sub_question"] = []
            question["answer"] = question["answer"].strip()

    if not parts and fallback_pieces:
        title = path.stem or "考生作答"
        parts.append(
            {
                "question": title,
                "answer": "",
                "images": [],
                "score": 0,
                "comment": "",
                "sub_question": [
                    {
                        "question": title,
                        "answer": "\n\n".join(fallback_pieces).strip(),
                        "images": fallback_images,
                        "score": 0,
                        "comment": "",
                        "sub_question": [],
                    }
                ],
            }
        )

    return {"id": student_id, "total_score": 0, "result": parts}, images


def _extract_student_entries(value: Any) -> list[dict[str, Any]]:
    def is_student(item: Any) -> bool:
        return isinstance(item, dict) and (
            item.get("id")
            or item.get("studentId")
            or isinstance(item.get("result"), list)
        )

    if isinstance(value, list):
        return [item for item in value if is_student(item)]
    if is_student(value):
        return [value]
    if isinstance(value, dict):
        return [item for item in value.values() if is_student(item)]
    return []


def _strip_answer_label(text: str) -> str:
    """去掉源文档自带的“参考答案：/评分情况：”等前缀标签。"""
    cleaned = text.strip()
    for label in ("参考答案", "评分情况", "答案", "评分标准"):
        if cleaned.startswith(label):
            cleaned = cleaned[len(label):].lstrip("：: \n")
    return cleaned.strip()


def _flatten_reference_answers(value: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for major_index, major in enumerate(value.get("大题", []) or [], 1):
        questions = major.get("小题", []) if isinstance(major, dict) else []
        if not questions:
            questions = [major]
        for question_index, question in enumerate(questions, 1):
            if not isinstance(question, dict):
                continue
            question_no = f"{major_index}.{question_index}"
            answer = _strip_answer_label(str(question.get("参考答案") or ""))
            criteria = _strip_answer_label(str(question.get("评分情况") or ""))

            # 答案写在子小题层级时（如“（1）（2）”分问作答），
            # 子小题的参考答案并入参考答案、评分情况并入评分标准，两栏分开。
            sub_questions = question.get("子小题")
            if isinstance(sub_questions, list) and sub_questions:
                sub_answer_pieces = []
                sub_criteria_pieces = []
                for sub in sub_questions:
                    if not isinstance(sub, dict):
                        continue
                    sub_answer = _strip_answer_label(str(sub.get("参考答案") or ""))
                    sub_criteria = _strip_answer_label(str(sub.get("评分情况") or ""))
                    if not sub_answer and not sub_criteria:
                        continue
                    label = str(sub.get("编号") or sub.get("题干") or "").strip()
                    if sub_answer:
                        sub_answer_pieces.append(f"{label}\n{sub_answer}" if label else sub_answer)
                    if sub_criteria:
                        sub_criteria_pieces.append(f"{label}\n{sub_criteria}" if label else sub_criteria)
                if sub_answer_pieces:
                    answer = "\n".join([piece for piece in (answer, *sub_answer_pieces) if piece])
                if sub_criteria_pieces:
                    criteria = "\n".join([piece for piece in (criteria, *sub_criteria_pieces) if piece])

            # 参考答案本身是图片时，在文本里标注图片文件名，
            # 提示阅卷人查看参考答案图片。
            reference_images = [
                str(item) for item in (question.get("参考答案图片") or []) if str(item).strip()
            ]
            if not answer and reference_images:
                answer = f"（参考答案为图片：{('、'.join(reference_images))}）"
            elif reference_images:
                answer += f"\n（另有参考答案图片：{('、'.join(reference_images))}）"

            result.append(
                {
                    "question_no": question_no,
                    "question_text": str(question.get("题干") or "").strip(),
                    "reference_answer": answer,
                    "grading_criteria": criteria,
                    "reference_images": reference_images,
                }
            )
    return result


def _clip_text(value: str, limit: int = LLM_INPUT_MAX_CHARS) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return text[:head] + "\n\n……中间内容已截断……\n\n" + text[-tail:]


def _reference_document_to_text(value: dict[str, Any]) -> str:
    lines: list[str] = []
    document_name = str(value.get("文档") or "").strip()
    if document_name:
        lines.append(f"文档：{document_name}")
    for major_index, major in enumerate(value.get("大题", []) or [], 1):
        if not isinstance(major, dict):
            continue
        title = str(major.get("标题") or f"第{major_index}大题").strip()
        lines.append(f"\n【大题 {major_index}】{title}")
        if str(major.get("说明") or "").strip():
            lines.append(f"说明：{major.get('说明')}")
        questions = major.get("小题", []) or [major]
        for question_index, question in enumerate(questions, 1):
            if not isinstance(question, dict):
                continue
            question_no = f"{major_index}.{question_index}"
            lines.append(f"\n题号：{question_no}")
            if str(question.get("分组") or "").strip():
                lines.append(f"分组：{question.get('分组')}")
            if str(question.get("题干") or "").strip():
                lines.append(f"题干：{question.get('题干')}")
            if str(question.get("参考答案") or "").strip():
                lines.append(f"参考答案：{question.get('参考答案')}")
            if str(question.get("评分情况") or "").strip():
                lines.append(f"评分标准：{question.get('评分情况')}")
            images = question.get("参考答案图片") or []
            if images:
                lines.append(f"参考答案图片：{', '.join(str(item) for item in images)}")
            sub_questions = question.get("子小题") or []
            for sub in sub_questions:
                if not isinstance(sub, dict):
                    continue
                label = str(sub.get("编号") or "").strip()
                if str(sub.get("题干") or "").strip():
                    lines.append(f"{label} 题干：{sub.get('题干')}")
                if str(sub.get("参考答案") or "").strip():
                    lines.append(f"{label} 参考答案：{sub.get('参考答案')}")
                if str(sub.get("评分情况") or "").strip():
                    lines.append(f"{label} 评分标准：{sub.get('评分情况')}")
    return _clip_text("\n".join(lines).strip())


def _strip_json_fence(text: str) -> str:
    value = str(text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
        value = re.sub(r"\s*```$", "", value)
    start = value.find("{")
    end = value.rfind("}")
    if start >= 0 and end > start:
        return value[start:end + 1]
    return value


def _normalize_question_no(value: Any) -> str:
    text = str(value or "").strip()
    text = text.replace("，", ".").replace("、", ".").replace("．", ".")
    text = re.sub(r"^第\s*", "", text)
    text = re.sub(r"\s*(题|小题)$", "", text)
    text = re.sub(r"\s+", "", text)
    match = re.match(r"^(\d+)[^\d]+(\d+)$", text)
    if match:
        return f"{int(match.group(1))}.{int(match.group(2))}"
    match = re.match(r"^(\d+)\.(\d+)$", text)
    if match:
        return f"{int(match.group(1))}.{int(match.group(2))}"
    return text


def _normalize_reference_answer_items(
    items: Any,
    expected_questions: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    expected_set = {
        _normalize_question_no(item.get("question_no"))
        for item in (expected_questions or [])
        if isinstance(item, dict) and str(item.get("question_no") or "").strip()
    }
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        question_no = _normalize_question_no(item.get("question_no"))
        if not question_no or question_no in seen:
            continue
        if expected_set and question_no not in expected_set:
            continue
        reference_images = item.get("reference_images") or []
        if not isinstance(reference_images, list):
            reference_images = []
        confidence = item.get("confidence")
        try:
            confidence = float(confidence) if confidence not in (None, "") else None
        except (TypeError, ValueError):
            confidence = None
        normalized = {
            "question_no": question_no,
            "question_text": str(item.get("question_text") or "").strip(),
            "reference_answer": _strip_answer_label(str(item.get("reference_answer") or "")),
            "grading_criteria": _strip_answer_label(str(item.get("grading_criteria") or "")),
            "reference_images": [str(image).strip() for image in reference_images if str(image).strip()],
        }
        if item.get("max_score") not in (None, ""):
            normalized["max_score"] = item.get("max_score")
        if confidence is not None:
            normalized["confidence"] = max(0.0, min(1.0, confidence))
        if isinstance(item.get("warnings"), list):
            normalized["warnings"] = [
                str(warning).strip() for warning in item["warnings"] if str(warning).strip()
            ]
        result.append(normalized)
        seen.add(question_no)
    return result


def _merge_reference_answers(
    rule_items: list[dict[str, Any]],
    llm_items: list[dict[str, Any]],
    expected_questions: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    rule_by_no = {
        _normalize_question_no(item.get("question_no")): dict(item)
        for item in rule_items
        if isinstance(item, dict) and str(item.get("question_no") or "").strip()
    }
    llm_by_no = {
        _normalize_question_no(item.get("question_no")): dict(item)
        for item in llm_items
        if isinstance(item, dict) and str(item.get("question_no") or "").strip()
    }
    if expected_questions:
        order = [
            _normalize_question_no(item.get("question_no"))
            for item in expected_questions
            if isinstance(item, dict) and str(item.get("question_no") or "").strip()
        ]
    else:
        order = list(rule_by_no.keys()) + [
            question_no for question_no in llm_by_no if question_no not in rule_by_no
        ]

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for question_no in order:
        if not question_no or question_no in seen:
            continue
        rule_item = rule_by_no.get(question_no, {})
        llm_item = llm_by_no.get(question_no, {})
        source = llm_item if (
            str(llm_item.get("reference_answer") or "").strip()
            or str(llm_item.get("grading_criteria") or "").strip()
        ) else rule_item
        if not source:
            continue
        merged = {
            "question_no": question_no,
            "question_text": (
                str(source.get("question_text") or "").strip()
                or str(rule_item.get("question_text") or "").strip()
            ),
            "reference_answer": str(source.get("reference_answer") or "").strip(),
            "grading_criteria": str(source.get("grading_criteria") or "").strip(),
            "reference_images": (
                rule_item.get("reference_images")
                or source.get("reference_images")
                or []
            ),
        }
        for key in ("max_score", "confidence", "warnings"):
            if key in source:
                merged[key] = source[key]
        result.append(merged)
        seen.add(question_no)
    return result


def _build_reference_llm_prompt(
    parsed: dict[str, Any],
    rule_items: list[dict[str, Any]],
    expected_questions: list[dict[str, Any]] | None = None,
    source_text: str = "",
) -> list[dict[str, str]]:
    expected_text = "无"
    if expected_questions:
        expected_text = json.dumps(expected_questions, ensure_ascii=False, indent=2)
    parsed_text = _reference_document_to_text(parsed)
    rule_text = json.dumps(rule_items, ensure_ascii=False, indent=2)
    source_text = _clip_text(source_text)
    user_content = f"""
请从下面的参考答案和评分标准文档中，识别每道题的参考答案、评分标准，并按题号输出。

当前试卷包已解析出的题号结构如下，输出的 question_no 必须优先匹配这些题号：
{expected_text}

规则解析初稿如下，可用于辅助判断，但不要盲目照抄错误字段：
{rule_text}

文档正文如下：
{parsed_text}

文档原始顺序文本（当上面的规则结构不完整时，以此为补充）：
{source_text or "无"}

输出要求：
1. 只返回 JSON，不要 Markdown，不要解释。
2. JSON 顶层格式必须是 {{"reference_answers": [...]}}。
3. 每个 reference_answers 元素包含 question_no、question_text、reference_answer、grading_criteria、max_score、confidence、warnings。
4. 如果某题没有明确参考答案或评分标准，对应字段留空字符串，不要编造。
5. 若存在 expected question_no，只输出这些题号范围内的内容。
"""
    return [
        {
            "role": "system",
            "content": "你是严谨的考试参考答案与评分标准结构化识别助手，必须返回可被 JSON.parse 解析的 JSON。",
        },
        {"role": "user", "content": _clip_text(user_content)},
    ]


def _call_dashscope_reference_llm(
    messages: list[dict[str, str]],
    *,
    api_key: str,
    model: str,
    timeout: int,
) -> dict[str, Any]:
    import dashscope  # type: ignore

    dashscope.api_key = api_key
    response = dashscope.Generation.call(
        model=model,
        messages=messages,
        result_format="message",
        timeout=timeout,
    )
    status_code = getattr(response, "status_code", None)
    if status_code != HTTPStatus.OK:
        code = getattr(response, "code", "") or ""
        message = getattr(response, "message", "") or response
        raise RuntimeError(f"DashScope 调用失败: {status_code} {code} {message}")

    content = ""
    try:
        content = response.output.choices[0].message.content
    except Exception:
        if isinstance(response, dict):
            content = (
                response.get("output", {})
                .get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
    if not content:
        raise RuntimeError("DashScope 未返回可解析内容")
    return json.loads(_strip_json_fence(content))


def _parse_reference_answers_with_llm(
    parsed: dict[str, Any],
    rule_items: list[dict[str, Any]],
    *,
    llm_options: dict[str, Any],
    expected_questions: list[dict[str, Any]] | None = None,
    source_text: str = "",
) -> list[dict[str, Any]]:
    provider = str(llm_options.get("provider") or "dashscope").lower()
    api_key = str(llm_options.get("api_key") or "").strip()
    model = str(llm_options.get("model") or "qwen-plus").strip()
    timeout = int(llm_options.get("timeout") or 120)
    if not api_key:
        raise RuntimeError("未配置参考答案解析模型 API Key")
    if provider not in {"dashscope", "aliyun", "bailian"}:
        raise RuntimeError(f"暂不支持的参考答案解析模型提供方: {provider}")

    messages = _build_reference_llm_prompt(
        parsed,
        rule_items,
        expected_questions,
        source_text,
    )
    data = _call_dashscope_reference_llm(
        messages,
        api_key=api_key,
        model=model,
        timeout=timeout,
    )
    return _normalize_reference_answer_items(
        data.get("reference_answers") if isinstance(data, dict) else data,
        expected_questions,
    )


def load_json_file(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8-sig")
    return _extract_student_entries(json.loads(text))


def _normalize_image_ref(value: Any) -> str:
    return str(value or "").replace("\\", "/").strip().split("?")[0].split("#")[0].lstrip("./").lower()


def _collect_student_image_refs(value: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(value, dict):
        for item in value.values():
            refs.extend(_collect_student_image_refs(item))
    elif isinstance(value, list):
        for item in value:
            refs.extend(_collect_student_image_refs(item))
    elif isinstance(value, str):
        for match in re.finditer(r"----\s*([^\r\n]+?\.(?:png|jpg|jpeg|bmp|gif|webp|svg))\s*----", value, re.I):
            refs.append(match.group(1).strip())
        normalized = value.replace("\\", "/").strip()
        if Path(normalized.split("?")[0].split("#")[0]).suffix.lower() in IMAGE_SUFFIXES:
            refs.append(normalized)
    return refs


def _image_path_matches_refs(relative_path: Path, image_refs: list[str]) -> bool:
    if not image_refs:
        return False
    normalized_path = _normalize_image_ref(relative_path.as_posix())
    file_name = normalized_path.split("/")[-1]
    for image_ref in image_refs:
        normalized_ref = _normalize_image_ref(image_ref)
        ref_name = normalized_ref.split("/")[-1]
        if normalized_path == normalized_ref:
            return True
        if normalized_path.endswith("/" + normalized_ref) or normalized_ref.endswith("/" + normalized_path):
            return True
        if ref_name and ref_name == file_name:
            return True
    return False


def _image_path_belongs_to_students(relative_path: Path, student_ids: list[str]) -> bool:
    normalized_path = relative_path.as_posix().lower()
    return any(str(student_id or "").lower() in normalized_path for student_id in student_ids)


def _safe_input_relative_path(file_path: Path, input_root: Path | None) -> Path:
    try:
        relative_input_path = (
            file_path.relative_to(input_root)
            if input_root is not None
            else Path(file_path.name)
        )
    except ValueError:
        relative_input_path = Path(file_path.name)
    return Path(*(
        part for part in relative_input_path.parts
        if part not in ("", ".", "..")
    ))


def _student_match_aliases(student_id: str, paper: str) -> set[str]:
    normalized = re.sub(r"[^0-9a-z]+", "", str(student_id or "").lower())
    aliases = {normalized} if normalized else set()
    paper_prefix = re.sub(r"[^0-9a-z]+", "", paper.lower())
    if paper_prefix and normalized.startswith(paper_prefix):
        aliases.add(normalized[len(paper_prefix):])
    return {alias for alias in aliases if alias}


def _path_matches_student_id(relative_path: Path, student_id: str, paper: str) -> bool:
    normalized_path = re.sub(r"[^0-9a-z]+", "", relative_path.as_posix().lower())
    return any(alias in normalized_path for alias in _student_match_aliases(student_id, paper))


def _student_id_from_folder_path(folder_path: str, paper: str) -> str | None:
    parts = [
        part.strip()
        for part in str(folder_path or "").replace("\\", "/").split("/")
        if part.strip() not in {"", ".", ".."}
    ]
    for part in parts:
        match = STUDENT_ID_PATTERN.search(part)
        if not match:
            continue
        student_id = match.group(0)
        return student_id if student_id.upper().startswith(paper.upper()) else paper + student_id
    return None


def _blank_question_node(node: dict[str, Any], keep_images: bool = False) -> dict[str, Any]:
    copied = deepcopy(node)
    copied.pop("answer_files", None)
    if "answer" in copied:
        copied["answer"] = ""
    # 模板来自另一考生的作答时清空图片（那是人家的答案图）；
    # 模板来自空白卷骨架时保留图片（那是题目自带的参考图）
    if "images" in copied and not keep_images:
        copied["images"] = []
    if "score" in copied:
        copied["score"] = 0
    if "comment" in copied:
        copied["comment"] = ""
    sub_questions = copied.get("sub_question")
    if isinstance(sub_questions, list):
        copied["sub_question"] = [
            _blank_question_node(item, keep_images)
            for item in sub_questions
            if isinstance(item, dict)
        ]
    return copied


def _blank_student_from_template(student_id: str, template: dict[str, Any] | None,
                                 keep_images: bool = False) -> dict[str, Any]:
    result = []
    if isinstance(template, dict) and isinstance(template.get("result"), list):
        result = [
            _blank_question_node(item, keep_images)
            for item in template["result"]
            if isinstance(item, dict)
        ]
    return {
        "id": student_id,
        "total_score": 0,
        "result": result,
        "is_blank": True,
        "answer_status": "blank",
        "blank_reason": "考生 KS 文件夹为空，未发现答题文件",
    }


def _normalized_folder_paths(folder_paths: list[str] | None) -> list[str]:
    normalized = []
    for folder_path in folder_paths or []:
        path = Path(str(folder_path).replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts:
            continue
        clean = "/".join(part for part in path.parts if part not in {"", "."})
        if clean:
            normalized.append(clean)
    return sorted(set(normalized), key=lambda item: (item.count("/"), item.lower()))


def _answer_file_item(file_path: Path, relative_input_path: Path, category: str) -> dict[str, Any]:
    file_size = file_path.stat().st_size if file_path.exists() else 0
    relative_text = relative_input_path.as_posix()
    return {
        "file_name": file_path.name,
        "file_type": "Excel表格" if category == "excel_answers" else "数据库备份/数据文件",
        "file_size": file_size,
        "size_kb": round(file_size / 1024, 2),
        "relative_path": relative_text,
        "storage_path": f"input/{relative_text}",
    }


def _merge_answer_file_group(target: dict[str, Any], source: dict[str, list[dict[str, Any]]]) -> None:
    if not source.get("excel_answers") and not source.get("database_answers"):
        return
    existing = target.get("answer_files")
    if not isinstance(existing, dict):
        existing = {}
    for key in ("excel_answers", "database_answers"):
        if source.get(key):
            existing[key] = source[key]
    target["answer_files"] = existing


def _normalized_match_text(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").lower())


def _question_text_for_file_match(question: dict[str, Any], parent_text: str = "") -> str:
    return "\n".join(
        str(item or "")
        for item in (
            parent_text,
            question.get("question"),
            question.get("题干"),
            question.get("answer"),
        )
        if item
    )


def _file_name_match_score(file_item: dict[str, Any], question_text: str) -> int:
    normalized_question = _normalized_match_text(question_text)
    file_name = str(file_item.get("file_name") or "")
    path_name = Path(file_name)
    candidates = {
        _normalized_match_text(file_name),
        _normalized_match_text(path_name.stem),
    }
    return 8 if any(candidate and candidate in normalized_question for candidate in candidates) else 0


def _category_match_score(category: str, question_text: str) -> int:
    normalized_question = _normalized_match_text(question_text)
    keywords = (
        EXCEL_QUESTION_KEYWORDS
        if category == "excel_answers"
        else DATABASE_QUESTION_KEYWORDS
    )
    matched = sum(
        1
        for keyword in keywords
        if _normalized_match_text(keyword) in normalized_question
    )
    return min(matched, 2) * 4


def _iter_question_targets(
    student: dict[str, Any],
) -> list[tuple[str, dict[str, Any], str]]:
    result = student.get("result")
    if not isinstance(result, list):
        return []

    targets: list[tuple[str, dict[str, Any], str]] = []
    for major_index, major in enumerate(result, 1):
        if not isinstance(major, dict):
            continue
        major_text = str(major.get("question") or major.get("题干") or "")
        sub_questions = major.get("sub_question")
        if isinstance(sub_questions, list) and sub_questions:
            for sub_index, sub_question in enumerate(sub_questions, 1):
                if isinstance(sub_question, dict):
                    targets.append((f"{major_index}.{sub_index}", sub_question, major_text))
        else:
            targets.append((str(major_index), major, ""))
    return targets


def _attach_answer_file_group_to_matching_questions(
    student: dict[str, Any],
    answer_files: dict[str, list[dict[str, Any]]],
) -> None:
    targets = _iter_question_targets(student)
    if not targets:
        return

    for category in ("excel_answers", "database_answers"):
        for file_item in answer_files.get(category) or []:
            scored_targets: list[tuple[int, str, dict[str, Any]]] = []
            for question_no, question, parent_text in targets:
                question_text = _question_text_for_file_match(question, parent_text)
                score = (
                    _file_name_match_score(file_item, question_text)
                    + _category_match_score(category, question_text)
                )
                if score > 0:
                    scored_targets.append((score, question_no, question))
            if not scored_targets:
                continue

            scored_targets.sort(key=lambda item: (-item[0], item[1]))
            best_score = scored_targets[0][0]
            best_questions = [
                question
                for score, _, question in scored_targets
                if score == best_score
            ]
            if len(best_questions) != 1:
                # 题干无法唯一定位时只保留考生级附件，避免错挂到某一道题。
                continue
            _merge_answer_file_group(
                best_questions[0],
                {
                    "excel_answers": [file_item] if category == "excel_answers" else [],
                    "database_answers": [file_item] if category == "database_answers" else [],
                },
            )


def _attach_answer_files_to_students(
    students: list[dict[str, Any]],
    input_files: list[Path],
    input_root: Path | None,
    student_source_dirs: dict[str, Path],
    paper: str,
) -> None:
    if not students:
        return

    grouped = {
        str(student.get("id") or "").strip(): {
            "excel_answers": [],
            "database_answers": [],
        }
        for student in students
        if isinstance(student, dict) and str(student.get("id") or "").strip()
    }
    if not grouped:
        return

    for file_path in input_files:
        if not file_path.is_file():
            continue
        # 素材/样张/帮助文件不参与题目解析（评分需要时经服务配置或
        # 题目级资源绑定按路径引用，见 PackageManifest.files.ignored）
        if is_ignored_attachment(file_path):
            continue
        category = _answer_file_category(file_path)
        if not category:
            continue
        relative_input_path = _safe_input_relative_path(file_path, input_root)
        for student_id in grouped:
            source_dir = student_source_dirs.get(student_id)
            same_student_dir = source_dir is not None and file_path.parent == source_dir
            id_in_path = _path_matches_student_id(relative_input_path, student_id, paper)
            if not same_student_dir and not id_in_path:
                continue
            item = _answer_file_item(file_path, relative_input_path, category)
            if all(existing.get("storage_path") != item["storage_path"] for existing in grouped[student_id][category]):
                grouped[student_id][category].append(item)

    for student in students:
        student_id = str(student.get("id") or "").strip()
        answer_files = grouped.get(student_id)
        if not answer_files:
            continue
        _merge_answer_file_group(student, answer_files)
        _attach_answer_file_group_to_matching_questions(student, answer_files)


def _question_key(text: Any, limit: int = 24) -> str:
    """题干首行的归一化键：去掉空白和标点，只保留前若干字符。"""
    return re.sub(r"[\W_]+", "", str(text or ""))[:limit]


def _align_students_to_common_structure(students: list[dict[str, Any]]) -> dict[int, list[str]]:
    """用多数票把所有考生对齐到权威的“大题 -> 小题”结构。

    真实试卷题目在所有考生卷面上是同一段文字，而考生常把小题内的
    作答写成“1. … 2. …”编号行，逐份解析时会被误认成新小题（例如
    大题一凭空多出几道“小题”）。这里取每个大题位置出现最多的题目
    序列作为权威结构，考生卷面上不在权威序列中的“伪小题”把题干和
    作答并回上一道真实小题。

    返回 {大题下标: [权威小题题干, ...]}，供后续大模型修复环节使用。
    """
    if len(students) < 3:
        return {}

    sequences_by_part: dict[int, Counter] = {}
    part_totals: dict[int, int] = {}
    for student in students:
        for index, part in enumerate(student.get("result") or []):
            subs = [sub for sub in (part.get("sub_question") or []) if isinstance(sub, dict)]
            sequence = tuple(_question_key(sub.get("question")) for sub in subs)
            sequences_by_part.setdefault(index, Counter())[sequence] += 1
            part_totals[index] = part_totals.get(index, 0) + 1

    canonical_by_part: dict[int, tuple[str, ...]] = {}
    for index, counter in sequences_by_part.items():
        sequence, count = counter.most_common(1)[0]
        # 权威序列必须由足够多的考生共同支撑，否则该大题保持原样。
        if count >= 3 and count >= 0.25 * part_totals[index]:
            canonical_by_part[index] = sequence

    if not canonical_by_part:
        return {}

    # 从多数派考生卷面上取一份原始题干作为权威小题题干文本。
    canonical_stems_by_part: dict[int, list[str]] = {}
    for student in students:
        for index, part in enumerate(student.get("result") or []):
            if index in canonical_stems_by_part:
                continue
            canonical = canonical_by_part.get(index)
            if not canonical:
                continue
            subs = [sub for sub in (part.get("sub_question") or []) if isinstance(sub, dict)]
            sequence = tuple(_question_key(sub.get("question")) for sub in subs)
            if sequence == canonical:
                canonical_stems_by_part[index] = [
                    str(sub.get("question") or "").strip() for sub in subs
                ]

    def matches(candidate: str, expected: str) -> bool:
        if not candidate or not expected:
            return False
        if candidate == expected:
            return True
        shorter, longer = sorted((candidate, expected), key=len)
        return len(shorter) >= 8 and shorter in longer

    for student in students:
        for index, part in enumerate(student.get("result") or []):
            canonical = canonical_by_part.get(index)
            if not canonical:
                continue
            subs = [sub for sub in (part.get("sub_question") or []) if isinstance(sub, dict)]
            aligned: list[dict[str, Any]] = []
            cursor = 0
            for sub in subs:
                key = _question_key(sub.get("question"))
                matched_at = next(
                    (
                        position
                        for position in range(cursor, len(canonical))
                        if matches(key, canonical[position])
                    ),
                    None,
                )
                if matched_at is not None:
                    aligned.append(sub)
                    cursor = matched_at + 1
                elif aligned:
                    # 伪小题：题干与作答并入上一道真实小题。
                    previous = aligned[-1]
                    extra = "\n\n".join(
                        piece for piece in (sub.get("question"), sub.get("answer")) if piece
                    )
                    previous["answer"] = (
                        previous["answer"] + "\n\n" + extra
                        if previous.get("answer") else extra
                    )
                    previous["images"] = (previous.get("images") or []) + (sub.get("images") or [])
                    merged_files = sub.get("answer_files")
                    if isinstance(merged_files, dict):
                        target_files = previous.setdefault("answer_files", {})
                        for category, files in merged_files.items():
                            target_files.setdefault(category, []).extend(files or [])
                else:
                    # 前面没有真实小题可并入，保留原样。
                    aligned.append(sub)
            part["sub_question"] = aligned

    return canonical_stems_by_part


_IMAGE_PLACEHOLDER = re.compile(r"----(.+?)----")


def _normalized_stem(text: Any) -> str:
    """题干全文归一化：去空白，用于判断考生卷面题干是否与权威题干一致。"""
    return re.sub(r"\s+", "", str(text or ""))


# 权威题干末尾常见的分值后缀，如“（5分）”，考生卷面可能缺省
RE_STEM_SCORE_SUFFIX = re.compile(r"^[（(]\d+(\.\d+)?分[)）]$")


def _stem_match_in_block(block_text: str, stem: str, start: int = 0) -> tuple[int, int] | None:
    """在文本块的任意位置匹配权威题干（忽略空白差异，忽略题号）。

    考生卷面常见“上一题作答与下一题题干同段”，题干不一定在块首，
    因此从 start 起逐位置尝试。匹配成功返回 (题干起始下标, 结束下标)，
    其后即为混入同一文本块的作答；失败返回 None。
    """
    stem_norm = re.sub(r"^\d+\s*[.、．]", "", _normalized_stem(stem))
    if not stem_norm:
        return None
    length = len(block_text)
    if start > length:
        return None
    for position in range(start, length + 1):
        index = position
        stem_index = 0
        while index < length and stem_index < len(stem_norm):
            char = block_text[index]
            if char.isspace():
                index += 1
                continue
            if char == stem_norm[stem_index]:
                index += 1
                stem_index += 1
            else:
                break
        if stem_index < len(stem_norm):
            remainder = stem_norm[stem_index:]
            if not RE_STEM_SCORE_SUFFIX.match(remainder):
                continue
        # 题干首字符不能落在块中间的空白上造成空匹配
        first_stem_char = next((ch for ch in stem_norm if not ch.isspace()), "")
        if first_stem_char and position < length and block_text[position] != first_stem_char:
            continue
        return position, index
    return None


def _has_verbatim_answer_duplication(answer: str) -> bool:
    """检测作答中“同一句 SQL/文字逐字出现两遍”的大模型修复事故。"""
    normalized = _normalized_stem(answer)
    if len(normalized) > 40:
        half = len(normalized) // 2
        if normalized[:half] == normalized[half:half * 2]:
            return True
    parts = [_normalized_stem(piece) for piece in str(answer).split(";") if _normalized_stem(piece)]
    if len(parts) >= 2 and len(parts[0]) > 15 and parts[0] == parts[1]:
        return True
    return False


# 题干匹配从题干首字开始，题号（如“2.”）会残留在上一题作答末尾，这里清掉
RE_TRAILING_QUESTION_NUMBER = re.compile(r"\s*\d+\s*[.、．]\s*$")
RE_LEADING_QUESTION_NUMBER = re.compile(r"^\s*\d+\s*[.、．]\s*")


def _rebuild_part_deterministic(
    blocks: list[dict[str, Any]],
    canonical_stems: list[str],
) -> list[dict[str, Any]] | None:
    """按权威题干对文本块做确定性重划分，失败返回 None（交给大模型兜底）。

    题干可在文本块任意位置出现（考生常把下一题题干粘进上一题作答同段），
    按题序在块序列中递增搜索；题干之后、下一题题干之前的所有内容归属本题作答。
    """
    block_texts = [str(block.get("text") or "").strip() for block in blocks]
    matched: list[tuple[int, int, int]] = []  # (块下标, 题干起始, 题干结束)
    search_block = 0
    search_position = 0
    for stem in canonical_stems:
        hit = None
        for block_index in range(search_block, len(blocks)):
            text = block_texts[block_index]
            if not text:
                continue
            start = search_position if block_index == search_block else 0
            result = _stem_match_in_block(text, stem, start=start)
            if result is not None:
                hit = (block_index, result[0], result[1])
                break
        if hit is None:
            return None
        matched.append(hit)
        # 同一块里题干之后可能紧跟下一题题干，搜索从当前题干结束处继续
        search_block = hit[0]
        search_position = hit[2]
    if len(matched) != len(canonical_stems):
        return None

    rebuilt: list[dict[str, Any]] = []
    for question_index, stem in enumerate(canonical_stems):
        block_index, _, stem_end = matched[question_index]
        text = block_texts[block_index]
        answer_parts: list[str] = []
        # 本题题干之后的尾部（若同块还有下一题题干则只取到其起始处）
        tail_end = len(text)
        if question_index + 1 < len(matched) and matched[question_index + 1][0] == block_index:
            tail_end = matched[question_index + 1][1]
        tail = text[stem_end:tail_end].strip()
        tail = RE_TRAILING_QUESTION_NUMBER.sub("", tail).strip()
        if tail:
            answer_parts.append(tail)
        answer_parts.extend(f"----{ref}----" for ref in blocks[block_index].get("images") or [])
        # 后续块直到下一题题干块，均归属本题作答
        if question_index + 1 < len(matched):
            next_block, next_start, _ = matched[question_index + 1]
            for scan_index in range(block_index + 1, next_block):
                candidate_text = block_texts[scan_index]
                placeholders = [f"----{ref}----" for ref in blocks[scan_index].get("images") or []]
                pieces = ([candidate_text] if candidate_text else []) + placeholders
                if pieces:
                    answer_parts.append("\n\n".join(pieces))
            # 下一题题干块中、题干起始之前的内容（上一题作答尾巴）也归本题
            if next_start > 0:
                head = block_texts[next_block][:next_start].strip()
                head = RE_TRAILING_QUESTION_NUMBER.sub("", head).strip()
                if head:
                    answer_parts.append(head)
        else:
            for scan_index in range(block_index + 1, len(blocks)):
                candidate_text = block_texts[scan_index]
                placeholders = [f"----{ref}----" for ref in blocks[scan_index].get("images") or []]
                pieces = ([candidate_text] if candidate_text else []) + placeholders
                if pieces:
                    answer_parts.append("\n\n".join(pieces))
        answer = "\n\n".join(piece for piece in answer_parts if piece).strip()
        rebuilt.append(
            {
                "question": stem,
                "answer": answer,
                "images": _IMAGE_PLACEHOLDER.findall(stem) + _IMAGE_PLACEHOLDER.findall(answer),
                "score": 0,
                "comment": "",
                "sub_question": [],
            }
        )
    return rebuilt


def _repair_student_structures_deterministic(
    students: list[dict[str, Any]],
    canonical_stems_by_part: dict[int, list[str]],
) -> dict[str, int]:
    """优先用权威题干做确定性结构修复，避免大模型修复时复制错漏。"""
    stats = {"repaired_parts": 0}
    if not canonical_stems_by_part:
        return stats
    for student in students:
        for index, part in enumerate(student.get("result") or []):
            canonical = canonical_stems_by_part.get(index)
            blocks = part.get("_blocks")
            if not canonical or not isinstance(blocks, list) or not blocks:
                continue
            subs = [sub for sub in part.get("sub_question") or [] if isinstance(sub, dict)]
            if len(subs) == len(canonical) and all(
                _normalized_stem(sub.get("question")) == _normalized_stem(stem)
                for sub, stem in zip(subs, canonical)
            ):
                continue
            rebuilt = _rebuild_part_deterministic(blocks, canonical)
            if rebuilt is not None:
                part["sub_question"] = rebuilt
                stats["repaired_parts"] += 1
    return stats


def _build_structure_repair_prompt(
    part_blocks: list[dict[str, Any]],
    canonical_stems: list[str],
) -> list[dict[str, str]]:
    blocks_lines = []
    for index, block in enumerate(part_blocks):
        text = str(block.get("text") or "").strip()
        placeholders = "".join(f" ----{ref}----" for ref in block.get("images") or [])
        blocks_lines.append(f"{index}: {text}{placeholders}")
    blocks_text = "\n".join(blocks_lines)
    stems_text = "\n".join(f"{i + 1}. {stem}" for i, stem in enumerate(canonical_stems))
    user_content = f"""
下面是某位考生答卷中一个大题的原始文本块（按文档顺序，下标从 0 开始，"----xxx----" 是图片占位符）：

{blocks_text}

该大题的权威小题题干（所有考生卷面上相同的题目原文）共 {len(canonical_stems)} 道：

{stems_text}

考生常把作答写进题干同一段落，或把下一题题干粘进上一题作答，导致规则解析错位。
请把上面的原始文本块重新划分到 {len(canonical_stems)} 道小题，输出 JSON：
{{"questions": [
    {{"question": "该题题干原文（逐字复制权威题干；若考生卷面该段落混入了作答，只保留题干部分）",
      "answer_head": "题干所在文本块中、题干之后剩余的作答文字（逐字复制，没有则为空字符串）",
      "answer_blocks": [该题作答所属文本块的下标列表，按文档顺序]}}
]}}

要求：
1. 只返回 JSON，不要 Markdown 代码块，不要解释。
2. questions 数组长度必须等于 {len(canonical_stems)}，顺序与权威题干一致。
3. 所有文字必须逐字复制原文，禁止改写、翻译、缩写或省略。
4. 每个文本块最多归属一道小题（题干所在块除外，其尾部作答放入 answer_head）。
5. 大题标题、分组说明（如“任选一种…”）不属于任何小题，不要分配。
6. 若某小题考生未作答，answer_head 为空字符串、answer_blocks 为空数组。
"""
    return [
        {
            "role": "system",
            "content": "你是严谨的考试答卷结构化助手，必须返回可被 JSON.parse 解析的 JSON。",
        },
        {"role": "user", "content": _clip_text(user_content)},
    ]


def _rebuild_part_from_llm(
    part: dict[str, Any],
    data: Any,
    canonical_stems: list[str],
) -> None:
    questions = data.get("questions") if isinstance(data, dict) else None
    if not isinstance(questions, list) or len(questions) != len(canonical_stems):
        raise ValueError("大模型返回的小题数量与权威结构不一致")

    blocks = part.get("_blocks") or []
    rebuilt: list[dict[str, Any]] = []
    for index, item in enumerate(questions):
        if not isinstance(item, dict):
            raise ValueError("大模型返回的小题格式不正确")
        stem = str(item.get("question") or "").strip() or canonical_stems[index]
        answer_parts: list[str] = []
        head = str(item.get("answer_head") or "").strip()
        if head:
            answer_parts.append(head)
        answer_blocks = item.get("answer_blocks") or []
        if not isinstance(answer_blocks, list):
            answer_blocks = []
        for block_index in answer_blocks:
            try:
                block = blocks[int(block_index)]
            except (TypeError, ValueError, IndexError):
                continue
            text = str(block.get("text") or "").strip()
            placeholders = [f"----{ref}----" for ref in block.get("images") or []]
            pieces = ([text] if text else []) + placeholders
            if pieces:
                answer_parts.append("\n\n".join(pieces))
        answer = "\n\n".join(piece for piece in answer_parts if piece).strip()
        if _has_verbatim_answer_duplication(answer):
            # 大模型修复常见事故：把同一句作答拼接两遍。
            # 发现逐字重复直接放弃本次大模型结果，保留规则解析原样。
            raise ValueError(f"大模型修复后的作答出现逐字重复: {answer[:60]}")
        rebuilt.append(
            {
                "question": stem,
                "answer": answer,
                "images": _IMAGE_PLACEHOLDER.findall(stem) + _IMAGE_PLACEHOLDER.findall(answer),
                "score": 0,
                "comment": "",
                "sub_question": [],
            }
        )
    part["sub_question"] = rebuilt


def _repair_student_structures_with_llm(
    students: list[dict[str, Any]],
    canonical_stems_by_part: dict[int, list[str]],
    llm_options: dict[str, Any] | None,
) -> dict[str, int]:
    """对题干/作答错位的考生卷面，用大模型按权威结构重新划分文本块。

    只处理与权威小题结构不一致（数量不同或题干文本不匹配）的大题，
    其余考生不受影响；大模型调用失败时保留规则解析结果。
    """
    stats = {"repaired_parts": 0, "failed_parts": 0}
    if not canonical_stems_by_part or not llm_options or not llm_options.get("enabled"):
        return stats
    api_key = str(llm_options.get("api_key") or "").strip()
    if not api_key:
        return stats
    model = str(llm_options.get("model") or "qwen-plus").strip()
    timeout = int(llm_options.get("timeout") or 120)

    for student in students:
        for index, part in enumerate(student.get("result") or []):
            canonical = canonical_stems_by_part.get(index)
            blocks = part.get("_blocks")
            if not canonical or not isinstance(blocks, list) or not blocks:
                continue
            subs = [sub for sub in (part.get("sub_question") or []) if isinstance(sub, dict)]
            if len(subs) == len(canonical) and all(
                _normalized_stem(sub.get("question")) == _normalized_stem(stem)
                for sub, stem in zip(subs, canonical)
            ):
                continue
            try:
                data = _call_dashscope_reference_llm(
                    _build_structure_repair_prompt(blocks, canonical),
                    api_key=api_key,
                    model=model,
                    timeout=timeout,
                )
                _rebuild_part_from_llm(part, data, canonical)
                stats["repaired_parts"] += 1
            except Exception as exc:
                stats["failed_parts"] += 1
                logger.warning(
                    f"大模型修复考生“{student.get('id')}”大题{index + 1}结构失败: {exc}"
                )
    if stats["repaired_parts"] or stats["failed_parts"]:
        logger.info(
            f"大模型结构修复完成: 成功 {stats['repaired_parts']} 处，失败 {stats['failed_parts']} 处"
        )
    return stats


def process_package_documents(
    student_documents: list[Path],
    existing_json: Path | None,
    input_files: list[Path],
    output_dir: Path,
    input_root: Path | None = None,
    paper: str = "32A",
    folder_paths: list[str] | None = None,
    llm_options: dict[str, Any] | None = None,
    blank_paper_document: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """批量转换学生文档，并整理已有 JSON/图片为统一结果。

    blank_paper_document 为空白卷骨架（paper_structure_builder.parse_blank_paper
    的产出）：无作答考生的题目结构优先取空白卷（比复制另一考生的解析结果
    更权威）；未提供时维持原行为（考生模板兜底）。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    media_root = output_dir / "media"
    students: list[dict[str, Any]] = []
    generated_images: list[dict[str, Any]] = []
    student_source_dirs: dict[str, Path] = {}
    student_documents = [
        path for path in student_documents if not is_temporary_word_file(path)
    ]
    # 单个损坏的文档（如 0 字节的 .docx）只跳过并告警，不中止整批转换。
    valid_documents: list[Path] = []
    for path in student_documents:
        if path.suffix.lower() == ".docx":
            try:
                _validate_docx_file(path)
                valid_documents.append(path)
            except ValueError as exc:
                logger.warning(f"跳过无效的学生文档: {exc}")
        else:
            valid_documents.append(path)
    student_documents = valid_documents

    converted_docs = _convert_docs_to_docx(student_documents)
    # .doc 转换失败的考生直接跳过，避免逐个重试再次拉起 Word。
    if any(path.suffix.lower() == ".doc" for path in student_documents):
        student_documents = [
            path for path in student_documents
            if path.suffix.lower() != ".doc" or path in converted_docs
        ]
    try:
        if student_documents:
            for document in sorted(student_documents, key=lambda item: item.name.lower()):
                student_id = _student_id(document, paper)
                try:
                    student, images = _parse_student_document(
                        document,
                        student_id,
                        media_root,
                        converted_docs.get(document),
                    )
                except Exception as exc:
                    logger.warning(
                        f"跳过解析失败的学生文档“{document}”：{exc}"
                    )
                    continue
                students.append(student)
                student_source_dirs[student_id] = document.parent
                generated_images.extend(images)
        elif existing_json is not None:
            students = load_json_file(existing_json)
    finally:
        for temporary in converted_docs.values():
            if temporary.exists():
                temporary.unlink()

    # 同一考生可能提交多份文档（如 Answer.doc + 代码.docx），
    # 每份文档各解析出一条记录会导致考生数虚增，这里按学号合并。
    # 其中标题重复的大题（两份文档都带完整试卷结构）按标题去重、
    # 作答取更完整的一份，避免出现"大题5~8 = 大题1~4 复制品"。
    def _part_title_key(part: dict[str, Any]) -> str:
        title = str(part.get("question") or "").strip().split("\n", 1)[0]
        return re.sub(r"[\s（）()、，,.。：:；;-]+", "", title).lower()

    def _merge_duplicate_part(target: dict[str, Any], source: dict[str, Any]) -> None:
        target_subs = target.get("sub_question") or []
        source_subs = source.get("sub_question") or []
        if len(target_subs) == len(source_subs):
            for target_sub, source_sub in zip(target_subs, source_subs):
                if not str(target_sub.get("answer") or "").strip() and str(source_sub.get("answer") or "").strip():
                    target_sub["answer"] = source_sub.get("answer")
                for image in source_sub.get("images") or []:
                    if image not in (target_sub.get("images") or []):
                        target_sub.setdefault("images", []).append(image)
        else:
            for source_sub in source_subs:
                if str(source_sub.get("answer") or "").strip():
                    target_subs.append(source_sub)

    merged_students: list[dict[str, Any]] = []
    students_by_id: dict[str, dict[str, Any]] = {}
    for student in students:
        student_id = str(student.get("id") or "").strip()
        existing = students_by_id.get(student_id) if student_id else None
        if existing is None:
            if student_id:
                students_by_id[student_id] = student
            merged_students.append(student)
        else:
            for part in student.get("result") or []:
                if not isinstance(part, dict):
                    continue
                title_key = _part_title_key(part)
                matched = next(
                    (
                        existing_part
                        for existing_part in existing["result"]
                        if isinstance(existing_part, dict) and title_key and _part_title_key(existing_part) == title_key
                    ),
                    None,
                )
                if matched is not None:
                    _merge_duplicate_part(matched, part)
                else:
                    existing["result"].append(part)
    students = merged_students
    # 多数票大题数之外的多余部分裁掉：代码文档/重复文档被误切成
    # "大题customers"等，会产生试卷结构之外的 5.x/8.x 题号
    if students:
        major_count_votes: dict[int, int] = {}
        for student in students:
            count = len(student.get("result") or [])
            major_count_votes[count] = major_count_votes.get(count, 0) + 1
        majority_majors = max(major_count_votes.items(), key=lambda kv: (kv[1], kv[0]))[0]
        for student in students:
            result = student.get("result") or []
            if len(result) > majority_majors:
                student["result"] = result[:majority_majors]
    # 全体考生多数票对齐“大题 -> 小题”结构，纠正考生作答里的编号行
    # 被误认成小题的问题（需在合并重复考生之后、生成空白卷之前）。
    canonical_stems_by_part = _align_students_to_common_structure(students)
    # 先按权威题干做确定性重划分（题干前缀匹配），解决“题干与作答同段”等错位；
    # 确定性方法无法对齐的大题再交给大模型修复。
    deterministic_repair_stats = _repair_student_structures_deterministic(
        students,
        canonical_stems_by_part,
    )
    if deterministic_repair_stats.get("repaired_parts"):
        logger.info(
            f"权威题干确定性结构修复完成: {deterministic_repair_stats['repaired_parts']} 处"
        )
    # 多数票无法处理“题干与作答在同一段落”等错位，交给大模型按权威结构修复。
    llm_repair_stats = _repair_student_structures_with_llm(
        students,
        canonical_stems_by_part,
        llm_options,
    )
    for student in students:
        for part in student.get("result") or []:
            if isinstance(part, dict):
                part.pop("_blocks", None)

    # 有空白卷时：作答文档解析不出题号结构（题数远少于空白卷骨架）的考生，
    # 统一用空白卷骨架作为其试卷结构——工程类考试考生常只提交无题号的
    # 说明文档/作品文件，fallback 单题会导致"每考生只有一道题"。
    # 原文档内容不并入题目结构（保持与空白卷一致的题量），文件仍保留在
    # 考生 KS 文件夹中，评分服务与作答文件查看均可访问。
    if isinstance(blank_paper_document, dict) and blank_paper_document:
        from app.services.paper_structure_builder import blank_paper_to_student_template
        skeleton = blank_paper_to_student_template(blank_paper_document)
        if skeleton:
            skeleton_question_count = sum(
                len(part.get("sub_question") or []) for part in skeleton
            )
            for student in students:
                if student.get("is_blank"):
                    continue  # 空白考生由下方模板逻辑统一处理
                result = student.get("result") or []
                question_count = sum(
                    len(part.get("sub_question") or [])
                    for part in result
                    if isinstance(part, dict)
                )
                # 大题数与骨架不一致（如历史版本追加的"考生提交文档"大题）、
                # 或题数不足骨架一半（无题号的 fallback 单题），都用骨架替换
                if (
                    len(result) == len(skeleton)
                    and skeleton_question_count
                    and question_count * 2 >= skeleton_question_count
                ):
                    continue  # 自身结构与空白卷一致且基本完整，保持原样
                student["result"] = deepcopy(skeleton)

    known_student_ids = set(students_by_id)
    # 无作答考生的题目骨架：优先空白卷（每个考生自己的"试卷"），
    # 无空白卷时回退到首个有作答考生的解析结果（原行为）
    template_student = None
    template_from_blank_paper = False
    if isinstance(blank_paper_document, dict) and blank_paper_document:
        # 延迟导入避免与 paper_structure_builder 的循环依赖
        from app.services.paper_structure_builder import blank_paper_to_student_template
        blank_template = blank_paper_to_student_template(blank_paper_document)
        if blank_template:
            template_student = {"id": "", "result": blank_template}
            # 空白卷骨架里的图片是题目参考图，空白考生生成时保留（不清空）
            template_from_blank_paper = True
    if template_student is None:
        template_student = next(
            (
                student for student in students
                if isinstance(student, dict) and isinstance(student.get("result"), list) and student.get("result")
            ),
            None,
        )
    for folder_path in _normalized_folder_paths(folder_paths):
        student_id = _student_id_from_folder_path(folder_path, paper)
        if not student_id or student_id in known_student_ids:
            continue
        students.append(_blank_student_from_template(student_id, template_student, keep_images=template_from_blank_paper))
        known_student_ids.add(student_id)

    student_ids = [str(student.get("id") or "").strip() for student in students if student.get("id")]
    student_image_refs = _collect_student_image_refs(students)
    for file_path in input_files:
        if file_path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        relative_input_path = _safe_input_relative_path(file_path, input_root)
        if student_image_refs:
            if not _image_path_matches_refs(relative_input_path, student_image_refs):
                continue
        elif student_ids and not _image_path_belongs_to_students(relative_input_path, student_ids):
            continue
        target = output_dir / "input-media" / relative_input_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(file_path.read_bytes())
        relative_output_path = target.relative_to(output_dir).as_posix()
        generated_images.append(
            {
                "name": target.name,
                "relative_path": relative_output_path,
                "storage_path": str(target),
                "file_type": target.suffix.lstrip(".").lower(),
                "file_size": target.stat().st_size,
            }
        )

    _attach_answer_files_to_students(
        students,
        input_files,
        input_root,
        student_source_dirs,
        paper,
    )

    # 考生清单按学号排序：解析顺序原为"文档文件名排序+空白卷追加"，
    # 与文件夹顺序（学号序）不一致；统一按学号排，展示从第一个文件夹开始
    students.sort(key=lambda item: str(item.get("id") or ""))

    students_json = output_dir / "students.json"
    students_json.write_text(
        json.dumps(students, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "students": students,
        "students_json": str(students_json),
        "images": generated_images,
        "llm_repair_stats": llm_repair_stats,
    }


def process_reference_document(
    path: Path,
    output_dir: Path,
    *,
    json_filename: str = "answer-key.json",
    image_dir_name: str = "answer-images",
    image_prefix: str = "answer",
    llm_options: dict[str, Any] | None = None,
    expected_questions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    source_lines: list[str] = []
    parsed = _parse_reference_document(
        path,
        output_dir / image_dir_name,
        image_prefix=image_prefix,
        source_lines=source_lines,
    )
    json_path = output_dir / json_filename
    json_path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
    rule_items = _flatten_reference_answers(parsed)
    reference_answers = rule_items
    parser_mode = "rule"
    warnings: list[str] = []
    llm_error = None

    if llm_options and llm_options.get("enabled"):
        try:
            llm_items = _parse_reference_answers_with_llm(
                parsed,
                rule_items,
                llm_options=llm_options,
                expected_questions=expected_questions,
                source_text="\n".join(source_lines),
            )
            if llm_items:
                reference_answers = _merge_reference_answers(
                    rule_items,
                    llm_items,
                    expected_questions,
                )
                parser_mode = "llm"
            else:
                parser_mode = "rule_fallback"
                warnings.append("模型未识别出可用题目，已使用规则解析结果")
        except Exception as exc:
            llm_error = str(exc)
            parser_mode = "rule_fallback"
            warnings.append("模型识别失败，已使用规则解析结果")
            logger.warning(f"参考答案模型识别失败，使用规则解析结果: {exc}")

    return {
        "document": parsed,
        "reference_answers": reference_answers,
        "json_path": str(json_path),
        "parser_mode": parser_mode,
        "warnings": warnings,
        "llm_error": llm_error,
    }
