"""题目结构构建服务（解析通用化 · 第四步交付物）。

解决三个问题：
  1. 空白卷解析：空白卷（无答案的标准试卷）此前只存文件不解析，
     现在用与答案卷相同的规则解析器提取题目骨架（题号+题干），
     作为每个无作答考生的试卷结构（structureSource=blank_paper）；
  2. 考生骨架兜底：KS 目录为空/无作答文档的考生，题目结构优先取
     空白卷骨架（比"复制另一个考生的解析结果"更权威），
     无空白卷时维持原行为（考生模板兜底）；
  3. 满分提取：答案卷的分值只存在于评分情况文本里（如"表结构完整10分"），
     这里用规则提取成数字 max_score，供确认页展示与评分 clamp 使用。

本模块只读输入文档、产出结构数据，不改写任何已有解析产物；
由 docs/parsing_schemas/verify_paper_structure.py 用真实答案卷验证。
"""
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.teacher_scoring_document_parser import process_reference_document

# ==================== 满分提取 ====================

# 优先级从高到低尝试的模式；显式"满分N分"与"共N分"优先于采分点求和
_MAX_SCORE_PATTERNS = (
    # 本题满分：10分 / 本题满分10分 / （本题满分35分）
    re.compile(r"满分\s*[:：]?\s*(\d+(?:\.\d+)?)\s*分"),
    # 共35分 / 合计20分
    re.compile(r"(?:共|合计|总计)\s*(\d+(?:\.\d+)?)\s*分"),
)
# "分值：2.5 2.5" / "分值\n2\n1.5\n1.5"（数值清单后跟SQL等正文，数值段自然截断）
_SCORE_VALUE_LIST_RE = re.compile(
    r"分值\s*[:：]?\s*((?:\d+(?:\.\d+)?\s+)*\d+(?:\.\d+)?)"
)
# 采分点行内分值："（2分）/（占1.5分）/（各1分）"
_POINT_SCORE_RE = re.compile(r"[（(]\s*(?:占|各|共)?\s*(\d+(?:\.\d+)?)\s*分\s*[)）]")


def extract_max_score(*texts) -> Optional[float]:
    """从题干/参考答案/评分细则文本中提取本题满分。

    规则（优先级从高到低）：
      1. 显式"满分N分" / "共N分"，取第一个命中；
      2. "分值"标签后的数值清单求和（如"分值：2.5 2.5"，SQL题常见格式）；
      3. 采分点行内"（N分）/（占N分）"求和（采分点分值之和即本题满分）；
      4. 都提不到返回 None（由确认页人工补填，见边界清单）。
    """
    for text in texts:
        text = str(text or "")
        for pattern in _MAX_SCORE_PATTERNS:
            match = pattern.search(text)
            if match:
                return float(match.group(1))

    for text in texts:
        match = _SCORE_VALUE_LIST_RE.search(str(text or ""))
        if match:
            values = [float(v) for v in match.group(1).split()]
            if values:
                return round(sum(values), 2)

    point_scores: List[float] = []
    for text in texts:
        for match in _POINT_SCORE_RE.finditer(str(text or "")):
            point_scores.append(float(match.group(1)))
    if point_scores:
        return round(sum(point_scores), 2)
    return None


def enrich_reference_answers_with_max_score(reference_answers: List[dict]) -> int:
    """为扁平化参考答案条目补充 max_score 数值字段（已有值的不覆盖）。

    Returns: 补充了 max_score 的条目数。
    """
    enriched = 0
    for item in reference_answers:
        if not isinstance(item, dict) or item.get("max_score") not in (None, ""):
            continue
        max_score = extract_max_score(
            item.get("question_text"),
            item.get("reference_answer"),
            item.get("grading_criteria"),
        )
        if max_score is not None:
            item["max_score"] = max_score
            enriched += 1
    return enriched


# ==================== 空白卷解析与考生骨架 ====================

def parse_blank_paper(path, output_dir) -> Dict[str, Any]:
    """解析空白卷文档，产出题目骨架（规则模式，不启用 LLM）。

    复用答案卷解析器：空白卷与答案卷是同一套文档结构，只是没有
    参考答案/评分情况栏，规则解析天然兼容（对应字段为空）。

    图片保存到 output_dir/media/blank_paper/（与考生 media 目录平级），
    并产出与考生图片清单同格式的 images 列表——只有进入包的
    parsed_image_manifest，前端图片解析器才能显示这些参考图片。
    """
    result = process_reference_document(
        path,
        output_dir,
        json_filename="blank-paper.json",
        image_dir_name="media/blank_paper",
        image_prefix="blank",
        llm_options=None,
    )
    result["blank_paper_json"] = result.pop("document")
    result.pop("reference_answers", None)
    result["parser_mode"] = "rule"
    # 汇总参考图片为标准图片清单条目（与 _parse_student_document 的 images 同构）
    image_dir = Path(output_dir) / "media" / "blank_paper"
    images: List[dict] = []
    if image_dir.is_dir():
        for image_path in sorted(image_dir.iterdir(), key=lambda p: p.name.lower()):
            if not image_path.is_file():
                continue
            images.append({
                "name": image_path.name,
                "relative_path": f"media/blank_paper/{image_path.name}",
                "storage_path": str(image_path),
                "file_type": image_path.suffix.lstrip(".").lower(),
                "file_size": image_path.stat().st_size,
            })
    result["images"] = images
    return result


def _is_background_material(stem: str) -> bool:
    """识别“背景资料”类非作答小题：只是题干材料，不参与评分。

    空白卷里这类小题标题为“背景资料”/“背景材料”（可带序号），
    没有作答要求；若当作正常小题会生成多余的评阅目标。
    """
    title = str(stem or "").strip().split("\n", 1)[0]
    title = re.sub(r"^[\d一二三四五六七八九十]+[.、．]\s*", "", title)
    return title.startswith("背景资料") or title.startswith("背景材料")


def blank_paper_to_student_template(blank_document: Dict[str, Any]) -> List[dict]:
    """把空白卷骨架转换成考生 result 形态的题目结构。

    输出与 students.json 的 result 数组同构（大题 -> sub_question 小题），
    供 _blank_student_from_template 使用：每个无作答考生拿到的
    就是这份"空白试卷"作为自己的试卷，作答列全空、分数为 0。

    前端阅卷界面只渲染到第二层（大题 -> 小题），工程题的操作步骤
    （文档里的"子小题"（1）（2）…）与参考图片必须提升到小题层才能显示：
      - 子小题题干逐行并入小题题干；
      - 子小题的参考图片引用汇总到小题 images（media/blank_paper/...）。
    """
    template: List[dict] = []
    for major in (blank_document or {}).get("大题") or []:
        if not isinstance(major, dict):
            continue
        title = str(major.get("标题") or "").strip() or "未命名大题"
        sub_questions: List[dict] = []
        for question in major.get("小题") or []:
            if not isinstance(question, dict):
                continue
            stem = str(question.get("题干") or "").strip()
            if not stem:
                continue
            images: List[str] = []
            for image_name in (question.get("参考答案图片") or []):
                name = str(image_name).strip()
                if name:
                    images.append(f"media/blank_paper/{name}")
            steps: List[str] = []
            for sub in (question.get("子小题") or []):
                if not isinstance(sub, dict):
                    continue
                sub_stem = str(sub.get("题干") or "").strip()
                if not sub_stem:
                    continue
                steps.append(sub_stem)
                for image_name in (sub.get("参考答案图片") or []):
                    name = str(image_name).strip()
                    if name and f"media/blank_paper/{name}" not in images:
                        images.append(f"media/blank_paper/{name}")
            if steps:
                stem = "\n".join([stem, *steps])
            sub_questions.append({
                "question": stem,
                "answer": "",
                "images": images,
                "score": 0,
                "comment": "",
                "sub_question": [],
                # 背景资料只是题干材料，不是作答小题，前端据此排除评阅目标
                "is_background": _is_background_material(stem),
            })
        template.append({
            "question": title,
            "answer": "",
            "images": [],
            "score": 0,
            "comment": "",
            "sub_question": sub_questions,
        })
    return template
