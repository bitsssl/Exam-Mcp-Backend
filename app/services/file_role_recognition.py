"""统一文件角色识别模块（解析通用化 · 第二步交付物）。

职责：回答"包里的每个文件是什么角色"。原先散落在两处的判断规则收敛于此：
  - app/api/teacher_scoring.py：答案卷/空白卷/考生作答文档识别、上传类型
  - app/services/teacher_scoring_document_parser.py：数据库/Excel 作答文件分类

收敛原则：规则原样搬迁（行为与旧逻辑完全一致），由
docs/parsing_schemas/verify_file_roles.py 的对拍脚本保证等价。

新增能力（不影响现有流程）：
  - IGNORED_ATTACHMENT_DIR_KEYWORDS：素材/样张/帮助文件等附加内容豁免清单，
    对应 PackageManifest.files.ignored；"忽略"仅指不参与题目解析，
    文件保留在包内、路径记入 Manifest，评分时仍可按需引用；
  - recognize_file_role()：统一角色入口，输出 role + confidence + evidence，
    对应 PackageManifest 的 fileRef.role / roleConfidence。
"""
import re
from pathlib import Path

# ==================== 后缀与关键词（唯一权威定义） ====================

WORD_SUFFIXES = {".doc", ".docx"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".svg"}
EXCEL_ANSWER_SUFFIXES = {".xlsx", ".xls"}
DATABASE_ANSWER_SUFFIXES = {".mdf", ".ldf", ".bak", ".dmp", ".sql", ".db", ".dump"}
DATABASE_ANSWER_KEYWORDS = ("BAK", "BACKUP", "DATABASE", "DB", "SQL", "MDF", "LDF")

# 学号形态：6位以上数字 + 字母 + 数字（如 32A31001125A3230505 中的学号段）
STUDENT_ID_PATTERN = re.compile(r"\d{6,}[A-Za-z]\d+")

ANSWER_SUPPORTING_KEYWORDS = (
    "answer",
    "reference",
    "rubric",
    "答案",
    "参考答案",
    "标准答案",
    "评分标准",
)
BLANK_SUPPORTING_KEYWORDS = (
    "blank",
    "empty",
    "空白",
    "空卷",
    "空试卷",
)

# 素材/样张/帮助文件等附加内容：不参与题目解析，但保留文件与路径台账，
# 评分需要时经服务配置参数或题目级资源绑定引用。
IGNORED_ATTACHMENT_DIR_KEYWORDS = ("素材", "样张", "帮助文件", "说明文件")


# ==================== 基础判断（自 teacher_scoring.py / parser 原样搬迁） ====================

def is_temporary_word_file(path: Path) -> bool:
    """忽略 Office 生成的临时锁定文件。"""
    name = path.name.lower()
    return name.startswith("~$") or name.startswith(".~lock")


def detect_upload_type(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    if suffix in {"xlsx", "xls"}:
        return "Excel"
    if suffix in {"doc", "docx"}:
        return "Word"
    if suffix in {"jpg", "jpeg", "png", "bmp", "gif", "webp"}:
        return "Image"
    if suffix == "pdf":
        return "PDF"
    if suffix == "zip":
        return "Zip"
    return suffix.upper() if suffix else "File"


def _normalized_name_for_match(path: Path) -> str:
    parts = path.parts[-2:] if len(path.parts) >= 2 else path.parts
    return "/".join(parts).replace(" ", "").lower()


def name_matches_keywords(path: Path, keywords) -> bool:
    text = _normalized_name_for_match(path)
    return any(keyword.lower() in text for keyword in keywords)


def path_contains_student_id(path: Path) -> bool:
    return any(STUDENT_ID_PATTERN.search(part) for part in path.parts)


def is_student_answer_document(path: Path) -> bool:
    return (
        path.suffix.lower() in WORD_SUFFIXES
        and not is_temporary_word_file(path)
        and path_contains_student_id(path)
        and not name_matches_keywords(path, BLANK_SUPPORTING_KEYWORDS)
    )


def is_answer_supporting_document(path: Path) -> bool:
    if path.suffix.lower() not in WORD_SUFFIXES:
        return False
    if path_contains_student_id(path):
        return False
    return name_matches_keywords(path, ANSWER_SUPPORTING_KEYWORDS)


def is_blank_supporting_document(path: Path) -> bool:
    if path.suffix.lower() not in WORD_SUFFIXES:
        return False
    if path_contains_student_id(path):
        return False
    return name_matches_keywords(path, BLANK_SUPPORTING_KEYWORDS)


def is_database_answer_file(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix in DATABASE_ANSWER_SUFFIXES:
        return True
    upper_name = path.name.upper()
    return any(keyword in upper_name for keyword in DATABASE_ANSWER_KEYWORDS)


def answer_file_category(path: Path):
    """作答文件分类：excel_answers / database_answers / None（非作答文件）。"""
    if path.suffix.lower() in EXCEL_ANSWER_SUFFIXES:
        return "excel_answers"
    if (
        path.suffix.lower() not in WORD_SUFFIXES
        and path.suffix.lower() not in IMAGE_SUFFIXES
        and path.suffix.lower() != ".json"
        and is_database_answer_file(path)
    ):
        return "database_answers"
    return None


# ==================== 新增：素材豁免与统一角色入口 ====================

def is_ignored_attachment(path: Path) -> bool:
    """素材/样张/帮助文件等附加内容判定。

    匹配路径任一层目录名或文件名（不含扩展名）。这类文件不参与题目解析，
    但保留在包内并记入 PackageManifest.files.ignored，评分时可按路径引用。
    """
    for part in path.parts[:-1]:
        if any(keyword in part for keyword in IGNORED_ATTACHMENT_DIR_KEYWORDS):
            return True
    stem = path.stem
    return any(keyword in stem for keyword in IGNORED_ATTACHMENT_DIR_KEYWORDS)


def recognize_file_role(path: Path) -> dict:
    """统一文件角色入口（对应 PackageManifest 的 fileRef.role / roleConfidence）。

    优先级：临时文件 > 答案卷 > 空白卷 > 考生作答文档 > 考生作答文件
    > 附加素材 > 未知。confidence 目前规则命中一律 1.0，
    后续引入启发式建议（如内容特征）时再分级。
    """
    path = Path(path)
    evidence = []

    if is_temporary_word_file(path):
        return {"role": "unknown", "confidence": 1.0, "evidence": ["Office临时锁定文件"]}

    if is_answer_supporting_document(path):
        matched = [kw for kw in ANSWER_SUPPORTING_KEYWORDS
                   if kw.lower() in _normalized_name_for_match(path)]
        evidence.append(f"答案卷关键词: {','.join(matched)}")
        return {"role": "answer_key", "confidence": 1.0, "evidence": evidence}

    if is_blank_supporting_document(path):
        matched = [kw for kw in BLANK_SUPPORTING_KEYWORDS
                   if kw.lower() in _normalized_name_for_match(path)]
        evidence.append(f"空白卷关键词: {','.join(matched)}")
        return {"role": "blank_paper", "confidence": 1.0, "evidence": evidence}

    if is_student_answer_document(path):
        evidence.append("Word文档 + 路径含学号")
        return {"role": "candidate_answer", "confidence": 1.0, "evidence": evidence}

    category = answer_file_category(path)
    if category:
        evidence.append(f"作答文件分类: {category}")
        return {"role": "candidate_answer", "confidence": 1.0, "evidence": evidence}

    if is_ignored_attachment(path):
        evidence.append("素材/样张/帮助文件豁免清单")
        return {"role": "attachment", "confidence": 1.0, "evidence": evidence}

    return {"role": "unknown", "confidence": 0.0, "evidence": ["未命中任何角色规则"]}
