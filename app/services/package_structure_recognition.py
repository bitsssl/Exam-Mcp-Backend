"""包结构识别服务（解析通用化 · 第三步交付物）。

职责：回答"包里的东西在哪"——输入一个试卷包数据目录（含 input/、
supporting/），输出第一步定义的 PackageManifest 形态的定位结果：
考试级文件（答案卷/空白卷/素材豁免）+ 考生清单（每人作答目录与文件）。

定位逻辑原先散落在：
  - teacher_scoring.py 上传暂存流程（student_documents 收集、supporting 落位）
  - teacher_scoring.py _resolve_student_answer_folder（input/<试卷>/<学号>/KS）
  - parser _student_id_from_folder_path（文件夹名提学号）
收敛为本服务的"布局模式逐个尝试"策略，已知布局模式：
  1. per_candidate_dir：input/<试卷>/<学号>[/KS] 或 input/<学号>[/KS]
     （现有真实包的标准形态）
  2. flat_files：input/<试卷>/<学号>.docx 考生文件平铺（真实包 3/4 形态）
  3. ks_at_root：KS 目录直接挂在 input 根或包根（单考生/测试包形态）
新增布局模式时在 _LAYOUT_MATCHERS 注册即可，其余环节不动。

所有模式都不命中时输出人话报错 + 目录树预览，不静默返回空。
本服务为纯识别（只读不写），不影响现有上传/解析/评分流程；
由 docs/parsing_schemas/verify_package_structure.py 用真实包对拍保证
识别结果与现有 students.json 一致。
"""
import re
from pathlib import Path
from typing import List, Optional

from app.services.file_role_recognition import (
    STUDENT_ID_PATTERN,
    is_answer_supporting_document,
    is_blank_supporting_document,
    is_ignored_attachment,
)

MAX_TREE_PREVIEW_ENTRIES = 40
# 试卷前缀形态：32A / 33A / 12B 等（目录名"32A-new"同样取前缀"32A"）
_PAPER_PREFIX_RE = re.compile(r"^(\d{2}[A-Za-z])")
DEFAULT_PAPER = "32A"


def infer_paper_prefix(input_root) -> str:
    """从 input 目录结构推断试卷前缀（如 32A / 33A）。

    取第一层目录中第一个"非考生目录、非 KS/素材"且以 32A 形态开头的名字，
    截取前三位前缀。推断不出时回退历史默认值 32A。
    学号前缀补齐（_student_id / extract_student_id）依赖此值，
    33A 考试包如果误用 32A 会导致考生 ID 全部带上错误前缀。
    """
    input_root = Path(input_root)
    if not input_root.is_dir():
        return DEFAULT_PAPER
    try:
        names = [child.name for child in input_root.iterdir()]
    except OSError:
        return DEFAULT_PAPER
    for name in sorted(names):
        if STUDENT_ID_PATTERN.search(name):
            continue  # 考生目录（学号命名）
        if name.upper() == "KS" or any(k in name for k in ("素材", "样张", "帮助文件")):
            continue
        match = _PAPER_PREFIX_RE.match(name)
        if match:
            return match.group(1).upper()
    return DEFAULT_PAPER


# ==================== 学号提取（自 parser _student_id_from_folder_path 原样搬迁） ====================

def extract_student_id(folder_path: str, paper: str = "") -> Optional[str]:
    """从文件夹路径的任一层提取学号；学号不含试卷前缀时补上（如 32A）。"""
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
        if not paper or student_id.upper().startswith(paper.upper()):
            return student_id
        return paper + student_id
    return None


def _candidate_folder_id(folder_name: str, paper: str = "") -> Optional[str]:
    """目录名本身即学号目录（如 32A31001125A3230505）。"""
    return extract_student_id(folder_name, paper)


def _dir_tree_preview(root: Path, max_entries: int = MAX_TREE_PREVIEW_ENTRIES) -> List[str]:
    """目录树预览（供报错时人工判断），最多 max_entries 行。"""
    lines: List[str] = []
    try:
        entries = sorted(root.rglob("*"), key=lambda p: str(p).lower())
        for entry in entries:
            if len(lines) >= max_entries:
                lines.append(f"...（其余 {len(entries) - max_entries} 项已省略）")
                break
            relative = entry.relative_to(root).as_posix()
            lines.append(f"{relative}/" if entry.is_dir() else relative)
    except OSError as exc:
        lines.append(f"（目录读取失败: {exc}）")
    return lines


def _collect_files(folder: Path) -> List[dict]:
    files = []
    for file_path in sorted(folder.rglob("*"), key=lambda p: str(p).lower()):
        if not file_path.is_file():
            continue
        files.append({
            "relativePath": file_path.relative_to(folder).as_posix(),
            "fileName": file_path.name,
        })
    return files


# ==================== 布局模式匹配器 ====================

def _match_per_candidate_dir(input_root: Path, paper: str) -> Optional[dict]:
    """模式1：input/<试卷>/<学号>[/KS] 或 input/<学号>[/KS]（现有标准形态）。

    与 _resolve_student_answer_folder 的定位规则一致：
    学号目录下有 KS 子目录则取 KS，否则取学号目录本身。
    """
    student_dirs: dict[str, Path] = {}
    if not input_root.is_dir():
        return None
    # input 根第一、二层目录中学号命名的即考生目录
    for depth in (1, 2):
        for folder in input_root.glob("*/" * depth):
            if not folder.is_dir() or folder.name.upper() == "KS":
                continue
            student_id = _candidate_folder_id(folder.name, paper)
            if not student_id or student_id in student_dirs:
                continue
            ks_folder = folder / "KS"
            student_dirs[student_id] = ks_folder if ks_folder.is_dir() else folder
    if not student_dirs:
        return None
    resolved = []
    for student_id, answer_root in sorted(student_dirs.items()):
        files = _collect_files(answer_root)
        resolved.append({
            "candidateId": student_id,
            "answerRoot": answer_root.as_posix(),
            "isBlank": not files,
            "blankReason": None if files else "考生目录为空，未发现作答文件",
            "answerFiles": [
                {"relativePath": f"{answer_root.as_posix()}/{item['relativePath']}",
                 "fileName": item["fileName"]}
                for item in files
            ],
        })
    return {
        "mode": "per_candidate_dir",
        "root": "input",
        "resolved": resolved,
    }


def _match_ks_at_root(package_root: Path, input_root: Path) -> Optional[dict]:
    """模式2：KS 目录直接挂在 input 根或包根（单考生/测试包形态）。"""
    for base, prefix in ((input_root, "input"), (package_root, "")):
        ks_folder = base / "KS"
        if not ks_folder.is_dir():
            continue
        files = _collect_files(ks_folder)
        root_text = f"{prefix}/KS" if prefix else "KS"
        return {
            "mode": "ks_at_root",
            "root": root_text,
            "resolved": [{
                "candidateId": "",
                "answerRoot": root_text,
                "isBlank": not files,
                "blankReason": None if files else "KS 目录为空",
                "answerFiles": [
                    {"relativePath": f"{root_text}/{item['relativePath']}",
                     "fileName": item["fileName"]}
                    for item in files
                ],
            }],
        }
    return None


def _match_flat_files(input_root: Path, paper: str) -> Optional[dict]:
    """模式3：input/<试卷>/<学号>.docx 考生文件平铺（真实包 3/4 形态）。

    考生靠文件名学号区分（对应 Manifest candidates.mode=flat_files），
    每人一份作答文档，无独立文件夹。
    """
    if not input_root.is_dir():
        return None
    resolved = []
    seen: set = set()
    # input 根一、二层的文件（input/<学号>.xxx 或 input/<试卷>/<学号>.xxx）
    for depth in (1, 2):
        for file_path in input_root.glob("*/" * (depth - 1) + "*.*"):
            if not file_path.is_file():
                continue
            student_id = extract_student_id(file_path.stem, paper)
            if not student_id or student_id in seen:
                continue
            seen.add(student_id)
            relative = file_path.relative_to(input_root).as_posix()
            resolved.append({
                "candidateId": student_id,
                "answerRoot": file_path.parent.relative_to(input_root).as_posix() or ".",
                "isBlank": False,
                "blankReason": None,
                "answerFiles": [{"relativePath": relative, "fileName": file_path.name}],
            })
    if not resolved:
        return None
    return {
        "mode": "flat_files",
        "root": "input",
        "resolved": sorted(resolved, key=lambda item: item["candidateId"]),
    }


_LAYOUT_MATCHERS = (
    ("per_candidate_dir", lambda root, input_root, paper: _match_per_candidate_dir(input_root, paper)),
    ("flat_files", lambda root, input_root, paper: _match_flat_files(input_root, paper)),
    ("ks_at_root", lambda root, input_root, paper: _match_ks_at_root(root, input_root)),
)


# ==================== 统一入口 ====================

def recognize_package_structure(package_root, paper: str = "",
                                known_answer_key: str = None,
                                known_blank_paper: str = None) -> dict:
    """识别包结构，输出 PackageManifest 形态的定位结果（只读）。

    Args:
        package_root: 包数据目录（含 input/，可含 supporting/），
            对应现有包记录的 parsed_storage_path 指向的目录。
        paper: 试卷名（如 32A/33A），用于学号前缀补齐；传空则从 input
            目录自动推断（infer_paper_prefix），推断不出回退 32A。
        known_answer_key / known_blank_paper: 上传表单明确指定的答案卷/
            空白卷相对路径（如 supporting/33A.docx）。显式指定优先于
            文件名关键词猜测——文件名不含"答案/空白"关键词时，
            关键词识别必然落空，而表单信息是确定的。

    Returns:
        status=success: {"status", "manifest": {files, candidates, ...}, "diagnostics"}
        status=error:   {"status", "error": 人话报错, "expected": 期望结构说明,
                          "tree_preview": [...]}
    """
    package_root = Path(package_root)
    if not paper:
        paper = infer_paper_prefix(package_root / "input")
    if not package_root.is_dir():
        return {
            "status": "error",
            "error": f"包目录不存在：{package_root}",
            "expected": "请确认试卷包数据目录（应包含 input/ 子目录）",
            "tree_preview": [],
        }
    input_root = package_root / "input"
    if not input_root.is_dir():
        # input 缺失时仍允许"包根直接挂 KS"的单考生形态；
        # 两者都不存在才报错
        ks_root_result = _match_ks_at_root(package_root, input_root)
        if ks_root_result:
            return {
                "status": "success",
                "manifest": {
                    "schemaVersion": "1.0",
                    "roleSource": "auto_detected",
                    "files": {"answerKey": None, "blankPaper": None, "ignored": []},
                    "candidates": {
                        "mode": ks_root_result["mode"],
                        "root": ks_root_result["root"],
                        "candidateIdRegex": STUDENT_ID_PATTERN.pattern,
                        "candidateIdSource": "dir_name",
                        "resolved": ks_root_result["resolved"],
                    },
                },
                "diagnostics": {
                    "matchedLayout": "ks_at_root",
                    "candidateCount": len(ks_root_result["resolved"]),
                    "blankCount": sum(1 for item in ks_root_result["resolved"] if item["isBlank"]),
                    "answerKeyFound": False,
                    "ignoredCount": 0,
                },
            }
        return {
            "status": "error",
            "error": f"未找到 input 目录：{package_root} 下没有 input/，也无法在包根定位 KS 目录",
            "expected": "期望结构：input/<试卷>/<学号>/KS/作答文件（或 input/<学号>/KS）",
            "tree_preview": _dir_tree_preview(package_root),
        }

    matched_layout = None
    matched_name = None
    for name, matcher in _LAYOUT_MATCHERS:
        matched_layout = matcher(package_root, input_root, paper)
        if matched_layout:
            matched_name = name
            break

    # ---------- 考试级文件定位（答案卷/空白卷/素材豁免） ----------
    # 显式指定（上传表单/包记录）优先；未指定时按文件名关键词猜测
    answer_key = None
    blank_paper = None
    for known, label in ((known_answer_key, "答案卷"), (known_blank_paper, "空白试卷")):
        if not known:
            continue
        known_path = package_root / str(known).lstrip("/")
        if known_path.is_file():
            relative = known_path.relative_to(package_root).as_posix()
            if label == "答案卷":
                answer_key = answer_key or relative
            else:
                blank_paper = blank_paper or relative
    ignored_paths: List[str] = []
    for base, prefix in ((package_root / "supporting", "supporting"), (input_root, "input")):
        if not base.is_dir():
            continue
        for file_path in sorted(base.rglob("*"), key=lambda p: str(p).lower()):
            if not file_path.is_file():
                continue
            relative = f"{prefix}/{file_path.relative_to(base).as_posix()}"
            if relative == answer_key or relative == blank_paper:
                continue  # 已显式定位，不重复扫描
            if is_answer_supporting_document(file_path):
                answer_key = answer_key or relative
            elif is_blank_supporting_document(file_path):
                blank_paper = blank_paper or relative
            elif is_ignored_attachment(file_path):
                ignored_paths.append(relative)

    if matched_layout is None:
        non_empty = any(input_root.rglob("*"))
        detail = (
            "input 目录为空，未发现任何考生作答文件"
            if not non_empty
            else "input 目录下未发现学号命名的考生目录，也不存在挂载在根部的 KS 目录"
        )
        return {
            "status": "error",
            "error": f"无法定位考生作答：{detail}。",
            "expected": (
                "期望结构（任一即可）："
                "input/<试卷>/<学号>/KS/作答文件、input/<学号>/KS/作答文件，"
                "或 KS 目录直接挂在 input 根下。"
                "若包结构不同，请在确认页手动指定考生作答目录规则。"
            ),
            "tree_preview": _dir_tree_preview(input_root),
            "manifest": {
                "schemaVersion": "1.0",
                "files": {
                    "answerKey": answer_key,
                    "blankPaper": blank_paper,
                    "ignored": ignored_paths,
                },
                "candidates": None,
            },
        }

    resolved = matched_layout["resolved"]
    # 学号去重合并（不同层级发现同一考生时保留文件更多的目录）
    return {
        "status": "success",
        "manifest": {
            "schemaVersion": "1.0",
            "roleSource": "auto_detected",
            "files": {
                "answerKey": answer_key,
                "blankPaper": blank_paper,
                "ignored": ignored_paths,
            },
            "candidates": {
                "mode": matched_layout["mode"],
                "root": matched_layout["root"],
                "candidateIdRegex": STUDENT_ID_PATTERN.pattern,
                "candidateIdSource": "dir_name",
                "resolved": resolved,
            },
        },
        "diagnostics": {
            "matchedLayout": matched_name,
            "paper": paper,
            "candidateCount": len(resolved),
            "blankCount": sum(1 for item in resolved if item["isBlank"]),
            "answerKeyFound": bool(answer_key),
            "ignoredCount": len(ignored_paths),
        },
    }
