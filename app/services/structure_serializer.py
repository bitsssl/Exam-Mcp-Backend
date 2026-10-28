"""结构序列化服务（解析通用化 · 第五步交付物：输出标准化 + 解析报告）。

职责：包解析完成后，把散落的解析产物（students.json / answer-key.json /
blank-paper.json / 包目录结构）序列化为第一步 Schema 定义的标准结构：

  manifest.json          包结构清单（文件角色 + 考生定位，来自包结构识别服务）
  unified-paper.json     统一试卷结构（题目 + 满分 + 参考答案）
  candidate-answers.json 考生作答结构（全部考生，按题号对齐）
  parse-report.json      解析报告（教师可读：数量、文件去向、异常清单）

双写原则：旧结构原样保留（评分段继续消费，零改动）；新结构是增量产物，
序列化失败只记日志降级，绝不阻断解析主流程。
已解析的历史包不回写（保持零触碰），需要时用本服务只读生成。

与 docs/parsing_schemas/convert_legacy.py 的关系：convert_legacy 是冻结的
对拍基准（独立实现），本模块是生产实现，由 verify_structure_serializer.py
对两者产出做交叉一致性校验。
"""
import json
import re
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.services.package_structure_recognition import recognize_package_structure
from app.services.paper_structure_builder import enrich_reference_answers_with_max_score

QUESTION_ID_RE = re.compile(r"^([0-9]+(\.[0-9]+)*|unassigned)$")


# ==================== 噪声清洗 ====================

def _clean_image_refs(refs) -> Tuple[str, ...]:
    """过滤 images 里的空串与 '-' 占位符（旧数据噪声）。"""
    return tuple(
        r for r in (str(item) for item in refs or [])
        if r.strip() and r.strip() != "-"
    )


# ==================== UnifiedPaper ====================

def _questions_from_answer_key(answer_key: Dict[str, Any]) -> Tuple[List[dict], List[str]]:
    """从答案卷规则解析产物构建 sections（题干权威来源=答案卷）。"""
    sections: List[dict] = []
    warnings: List[str] = []
    for major_index, major in enumerate(answer_key.get("大题") or [], 1):
        questions = []
        for question_index, question in enumerate(major.get("小题") or [], 1):
            if not isinstance(question, dict):
                continue
            question_id = f"{major_index}.{question_index}"
            answer = str(question.get("参考答案") or "").strip()
            criteria = str(question.get("评分情况") or "").strip()
            images = [str(i) for i in question.get("参考答案图片") or [] if str(i).strip()]
            for sub in question.get("子小题") or []:
                if not isinstance(sub, dict):
                    continue
                sub_answer = str(sub.get("参考答案") or "").strip()
                sub_criteria = str(sub.get("评分情况") or "").strip()
                label = str(sub.get("编号") or "").strip()
                if sub_answer:
                    piece = f"{label}\n{sub_answer}" if label else sub_answer
                    answer = "\n".join(x for x in (answer, piece) if x)
                if sub_criteria:
                    piece = f"{label}\n{sub_criteria}" if label else sub_criteria
                    criteria = "\n".join(x for x in (criteria, piece) if x)
            if not answer and images:
                answer = f"（参考答案为图片：{'、'.join(images)}）"
            stem = str(question.get("题干") or "").strip()
            if not stem:
                warnings.append(f"题 {question_id} 题干为空")
            if not answer and not criteria:
                warnings.append(f"题 {question_id} 无判分依据，默认转人工")
            questions.append({
                "questionId": question_id,
                "number": str(question.get("编号") or "") or None,
                "stem": stem,
                "maxScore": enrichable_score(stem, answer, criteria),
                "questionType": None,
                "answer": {
                    "referenceAnswer": answer or None,
                    "gradingCriteria": criteria or None,
                    "referenceImages": images,
                    "confidence": None,
                    "parserMode": "rule",
                },
                "options": [],
                "warnings": [],
            })
        sections.append({
            "sectionId": str(major_index),
            "title": str(major.get("标题") or "").strip(),
            "description": str(major.get("说明") or "").strip(),
            "questions": questions,
        })
    return sections, warnings


def _questions_from_blank_paper(blank_doc: Dict[str, Any]) -> Tuple[List[dict], List[str]]:
    """从空白卷骨架构建 sections（无答案卷时的题目结构来源）。

    与考生模板（blank_paper_to_student_template）保持一致：子小题操作步骤
    并入题干、参考图片汇总到 referenceImages（media/blank_paper/...）。
    """
    sections: List[dict] = []
    for major_index, major in enumerate(blank_doc.get("大题") or [], 1):
        questions = []
        for question_index, question in enumerate(major.get("小题") or [], 1):
            if not isinstance(question, dict):
                continue
            stem = str(question.get("题干") or "").strip()
            images = [
                f"media/blank_paper/{str(name).strip()}"
                for name in (question.get("参考答案图片") or [])
                if str(name).strip()
            ]
            steps = []
            for sub in (question.get("子小题") or []):
                if not isinstance(sub, dict):
                    continue
                sub_stem = str(sub.get("题干") or "").strip()
                if sub_stem:
                    steps.append(sub_stem)
                for name in (sub.get("参考答案图片") or []):
                    ref = f"media/blank_paper/{str(name).strip()}"
                    if str(name).strip() and ref not in images:
                        images.append(ref)
            if steps:
                stem = "\n".join([stem, *steps])
            questions.append({
                "questionId": f"{major_index}.{question_index}",
                "number": str(question.get("编号") or "") or None,
                "stem": stem,
                "maxScore": None,
                "questionType": None,
                "answer": {
                    "referenceAnswer": None,
                    "gradingCriteria": None,
                    "referenceImages": images,
                    "confidence": None,
                    "parserMode": "rule",
                },
                "options": [],
                "warnings": ["题目结构来自空白卷，待答案卷导入后补充参考答案与满分"],
            })
        sections.append({
            "sectionId": str(major_index),
            "title": str(major.get("标题") or "").strip(),
            "description": str(major.get("说明") or "").strip(),
            "questions": questions,
        })
    return sections, []


def _questions_from_students(students: List[dict]) -> Tuple[List[dict], List[str]]:
    """从考生解析结果反推 sections（最后兜底，仅题干）。"""
    template = next(
        (s for s in students if isinstance(s.get("result"), list) and s["result"]),
        None,
    )
    if not template:
        return [], ["全部考生均无作答文档，无法反推题目结构"]
    sections = []
    for major_index, major in enumerate(template["result"], 1):
        questions = []
        for question_index, sub in enumerate(major.get("sub_question") or [], 1):
            questions.append({
                "questionId": f"{major_index}.{question_index}",
                "number": None,
                "stem": str(sub.get("question") or "").strip(),
                "maxScore": None,
                "questionType": None,
                "answer": {
                    "referenceAnswer": None,
                    "gradingCriteria": None,
                    "referenceImages": [],
                    "confidence": None,
                    "parserMode": None,
                },
                "options": [],
                "warnings": ["题目结构由考生文档反推（仅题干），请以答案卷为准"],
            })
        sections.append({
            "sectionId": str(major_index),
            "title": str(major.get("question") or "").split("\n", 1)[0],
            "description": str(major.get("question") or "").split("\n", 1)[1] if "\n" in str(major.get("question") or "") else "",
            "questions": questions,
        })
    return sections, []


def enrichable_score(stem: str, answer: str, criteria: str) -> Optional[float]:
    """题级满分提取（复用第四步规则，直接内联调用避免循环导入开销）。"""
    from app.services.paper_structure_builder import extract_max_score
    return extract_max_score(stem, answer, criteria)


def build_unified_paper(parsed_dir: Path, paper_id: str) -> Tuple[Optional[dict], List[str]]:
    """按来源优先级构建 UnifiedPaper：答案卷 > 空白卷 > 考生文档反推。"""
    warnings: List[str] = []
    answer_key_path = parsed_dir / "answer-key.json"
    blank_path = parsed_dir / "blank-paper.json"
    students_path = parsed_dir / "students.json"

    structure_source = None
    sections: List[dict] = []
    title = None
    if answer_key_path.is_file():
        doc = json.loads(answer_key_path.read_text(encoding="utf-8"))
        sections, warnings = _questions_from_answer_key(doc)
        structure_source = "answer_key"
        title = str(doc.get("文档") or "") or None
    if not sections and blank_path.is_file():
        doc = json.loads(blank_path.read_text(encoding="utf-8"))
        sections, warnings = _questions_from_blank_paper(doc)
        structure_source = "blank_paper"
        title = str(doc.get("文档") or "") or None
    if not sections and students_path.is_file():
        students = json.loads(students_path.read_text(encoding="utf-8"))
        sections, warnings = _questions_from_students(students)
        structure_source = "student_documents"

    if not sections:
        return None, warnings + ["无法构建题目结构（无答案卷/空白卷/考生文档）"]

    paper = {
        "schemaVersion": "1.0",
        "paperId": paper_id,
        "title": title,
        "subject": None,
        "totalScore": None,
        "structureSource": structure_source,
        "sections": sections,
    }
    return paper, warnings


# ==================== CandidateAnswer ====================

def build_candidate_answers(students: List[dict], paper_id: str,
                            known_question_ids: Optional[set] = None) -> List[dict]:
    """考生作答结构（与 convert_legacy 同构，生产实现）。"""
    candidates = []
    for student in students:
        answers = []
        candidate_warnings = []
        for major_index, major in enumerate(student.get("result") or [], 1):
            for question_index, sub in enumerate(major.get("sub_question") or [], 1):
                answer_files = []
                for group_key in ("excel_answers", "database_answers"):
                    for item in (sub.get("answer_files") or {}).get(group_key) or []:
                        answer_files.append({
                            "relativePath": str(item.get("relative_path") or ""),
                            "fileName": str(item.get("file_name") or ""),
                            "category": group_key,
                            "storagePath": item.get("storage_path"),
                        })
                question_id = f"{major_index}.{question_index}"
                if known_question_ids is not None and question_id not in known_question_ids:
                    candidate_warnings.append(f"题号 {question_id} 未在试卷结构中找到，转人工复核")
                answers.append({
                    "questionId": question_id,
                    "answerText": str(sub.get("answer") or ""),
                    "answerImages": list(_clean_image_refs(sub.get("images"))),
                    "answerFiles": answer_files,
                    "source": None,
                    "score": sub.get("score", 0),
                    "comment": sub.get("comment"),
                })
        unassigned = []
        for group_key in ("excel_answers", "database_answers"):
            for item in (student.get("answer_files") or {}).get(group_key) or []:
                unassigned.append(str(item.get("relative_path") or ""))
        candidates.append({
            "schemaVersion": "1.0",
            "paperId": paper_id,
            "candidateId": str(student.get("id") or ""),
            "candidateName": None,
            "isBlank": bool(student.get("is_blank")),
            "blankReason": student.get("blank_reason"),
            "mediaFolder": f"media/{student.get('id')}_KS",
            "totalScore": student.get("total_score", 0),
            "answers": answers,
            "unassignedFiles": sorted(set(unassigned)),
            "warnings": sorted(set(candidate_warnings)),
        })
    return candidates


# ==================== 解析报告 ====================

def build_parse_report(manifest_result: dict, paper: Optional[dict],
                       candidates: List[dict], paper_warnings: List[str]) -> dict:
    """教师可读的解析报告（对应边界清单第五节，供前端展示）。"""
    diagnostics = (manifest_result or {}).get("diagnostics") or {}
    manifest = (manifest_result or {}).get("manifest") or {}
    files = manifest.get("files") or {}
    question_count = sum(len(s.get("questions") or []) for s in (paper or {}).get("sections") or [])
    missing_max_score = []
    if paper:
        for section in paper["sections"]:
            for question in section["questions"]:
                if question.get("maxScore") is None:
                    missing_max_score.append(question["questionId"])

    # 题号未对齐明细：考生 x 题号 逐条列出（转人工复核的定位依据）
    known_ids = set()
    if paper:
        known_ids = {q["questionId"] for s in paper["sections"] for q in s["questions"]}
    misaligned_items: List[dict] = []
    if known_ids:
        for candidate in candidates:
            for answer in candidate.get("answers") or []:
                if answer.get("questionId") not in known_ids:
                    misaligned_items.append({
                        "candidateId": candidate.get("candidateId"),
                        "questionNo": answer.get("questionId"),
                    })
    misaligned_count = len(misaligned_items) if known_ids else sum(
        1 for c in candidates for w in c.get("warnings") or [] if "未在试卷结构中找到" in w
    )

    # 空文件夹考生明细：前端目录清单登记、但服务器上没有文件目录的考生
    manifest_candidate_ids = set()
    resolved = ((manifest.get("candidates") or {}).get("resolved") or [])
    manifest_candidate_ids = {str(item.get("candidateId") or "") for item in resolved}
    empty_folder_candidates: List[str] = []
    if manifest_candidate_ids:
        empty_folder_candidates = [
            c.get("candidateId") for c in candidates
            if c.get("candidateId") and c.get("candidateId") not in manifest_candidate_ids
        ]

    return {
        "schemaVersion": "1.0",
        "summary": {
            "candidateCount": len(candidates),
            "blankCandidateCount": sum(1 for c in candidates if c.get("isBlank")),
            "questionCount": question_count,
            "structureSource": (paper or {}).get("structureSource"),
            "matchedLayout": diagnostics.get("matchedLayout"),
        },
        "files": {
            "answerKey": files.get("answerKey"),
            "blankPaper": files.get("blankPaper"),
            "ignoredAttachmentCount": len(files.get("ignored") or []),
            "ignoredAttachments": (files.get("ignored") or [])[:20],
        },
        "issues": {
            "missingMaxScoreQuestions": missing_max_score,
            "missingMaxScoreCount": len(missing_max_score),
            "misalignedAnswerCount": misaligned_count,
            "misalignedAnswers": misaligned_items[:200],
            "candidatesWithUnassignedFiles": sum(1 for c in candidates if c.get("unassignedFiles")),
            "emptyFolderCandidateCount": len(empty_folder_candidates),
            "emptyFolderCandidates": empty_folder_candidates[:200],
            "structureWarnings": paper_warnings[:20],
            "recognitionError": (manifest_result or {}).get("error") if (manifest_result or {}).get("status") == "error" else None,
        },
    }


# ==================== 序列化入口 ====================

def serialize_package(package_root, paper: str = "", output_dir=None,
                      known_answer_key: str = None, known_blank_paper: str = None) -> dict:
    """对一个包目录执行结构序列化，产出四份 JSON（双写，不改动旧产物）。

    Args:
        package_root: 包数据目录（含 input/ 与 parsed/）。
        paper: 试卷前缀（学号补齐用），传空则自动推断（33A 包必须传对，
            否则考生 ID 带错误前缀）。
        output_dir: 输出目录，默认 package_root/parsed/。
        known_answer_key / known_blank_paper: 上传表单明确指定的答案卷/
            空白卷相对路径，显式指定优先于文件名关键词猜测。

    Returns:
        {"status", "written": [...], "report": 解析报告, "error": 失败原因}
    """
    package_root = Path(package_root)
    source_parsed_dir = package_root / "parsed"
    parsed_dir = Path(output_dir) if output_dir else source_parsed_dir
    paper_id = package_root.name
    try:
        manifest_result = recognize_package_structure(
            package_root, paper,
            known_answer_key=known_answer_key,
            known_blank_paper=known_blank_paper,
        )
        students_path = source_parsed_dir / "students.json"
        students = []
        if students_path.is_file():
            students = json.loads(students_path.read_text(encoding="utf-8"))

        paper_struct, paper_warnings = build_unified_paper(source_parsed_dir, paper_id)
        known_ids = None
        if paper_struct:
            known_ids = {q["questionId"] for s in paper_struct["sections"] for q in s["questions"]}
        candidates = build_candidate_answers(students, paper_id, known_question_ids=known_ids)
        report = build_parse_report(manifest_result, paper_struct, candidates, paper_warnings)

        parsed_dir.mkdir(parents=True, exist_ok=True)
        written = []
        if (manifest_result or {}).get("status") == "success":
            (parsed_dir / "manifest.json").write_text(
                json.dumps(manifest_result["manifest"], ensure_ascii=False, indent=2), encoding="utf-8")
            written.append("manifest.json")
        if paper_struct:
            (parsed_dir / "unified-paper.json").write_text(
                json.dumps(paper_struct, ensure_ascii=False, indent=2), encoding="utf-8")
            written.append("unified-paper.json")
        (parsed_dir / "candidate-answers.json").write_text(
            json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append("candidate-answers.json")
        (parsed_dir / "parse-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append("parse-report.json")
        return {"status": "success", "written": written, "report": report}
    except Exception as exc:  # 序列化失败降级：记日志返回，不阻断主流程
        return {"status": "error", "written": [], "error": f"{exc}\n{traceback.format_exc()}"}
