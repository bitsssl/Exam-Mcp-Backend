"""
智能阅卷业务数据模型

这些表承载新增的阅卷业务数据；用户和 MCP 服务继续复用原系统的
users、mcp_services 表。
"""
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
)

from app.core.utils import now_beijing
from app.models.engine import Base


class BizSubject(Base):
    """科目表"""

    __tablename__ = "biz_subject"
    __table_args__ = (
        Index("idx_biz_subject_creator", "creator_id"),
        Index("idx_biz_subject_no_creator", "subject_no", "creator_id"),
        {"extend_existing": True},
    )

    subject_id = Column(Integer, primary_key=True, autoincrement=True)
    subject_no = Column(String(50), nullable=False)
    subject_name = Column(String(100), nullable=False)
    creator_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=now_beijing)
    remark = Column(String(255), nullable=True)


class BizExam(Base):
    """考试表"""

    __tablename__ = "biz_exam"
    __table_args__ = (
        Index("idx_biz_exam_subject", "subject_id"),
        Index("idx_biz_exam_creator", "creator_id"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    subject_id = Column(Integer, ForeignKey("biz_subject.subject_id"), nullable=False)
    exam_name = Column(String(100), nullable=False)
    exam_time = Column(DateTime, nullable=True)
    answer_key_path = Column(String(255), nullable=True)
    blank_paper_path = Column(String(255), nullable=True)
    creator_id = Column(Integer, ForeignKey("users.id"), nullable=False)


class BizExamPackage(Base):
    """试卷包表"""

    __tablename__ = "biz_exam_package"
    __table_args__ = (
        Index("idx_biz_exam_package_exam", "exam_id"),
        {"extend_existing": True},
    )

    package_id = Column(Integer, primary_key=True, autoincrement=True)
    exam_id = Column(Integer, ForeignKey("biz_exam.id"), nullable=False)
    package_name = Column(String(100), nullable=False)
    file_path = Column(String(255), nullable=False)
    extract_path = Column(String(255), nullable=True)
    uploaded_at = Column(DateTime, nullable=False, default=now_beijing)

class BizLocalExamPackage(Base):
    """本地试卷包登记表。"""

    __tablename__ = "biz_local_exam_package"
    __table_args__ = (
        Index("idx_biz_local_package_exam", "subject_id", "exam_id"),
        Index("idx_biz_local_package_creator", "creator_id"),
        Index("idx_biz_local_package_fingerprint", "exam_id", "creator_id", "package_fingerprint"),
        {"extend_existing": True},
    )

    local_package_id = Column(Integer, primary_key=True, autoincrement=True)
    subject_id = Column(Integer, ForeignKey("biz_subject.subject_id"), nullable=False)
    exam_id = Column(Integer, ForeignKey("biz_exam.id"), nullable=False)
    creator_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    package_name = Column(String(100), nullable=False)
    package_fingerprint = Column(String(128), nullable=False)
    quick_fingerprint = Column(String(128), nullable=True)
    file_fingerprint = Column(String(128), nullable=True)
    folder_name = Column(String(255), nullable=True)
    json_file_name = Column(String(255), nullable=True)
    json_file_size = Column(Integer, nullable=True)
    student_count = Column(Integer, nullable=False, default=0)
    image_count = Column(Integer, nullable=False, default=0)
    word_document_count = Column(Integer, nullable=False, default=0)
    total_file_size = Column(Integer, nullable=True)
    total_image_size = Column(Integer, nullable=True)
    total_word_size = Column(Integer, nullable=True)
    student_no_hash = Column(String(128), nullable=True)
    structure_summary = Column(JSON, nullable=True)
    answer_index = Column(JSON, nullable=True)
    file_manifest = Column(JSON, nullable=True)
    parsed_students = Column(JSON, nullable=True)
    parsed_image_manifest = Column(JSON, nullable=True)
    reference_answers = Column(JSON, nullable=True)
    blank_paper_json = Column(JSON, nullable=True)
    answer_key_json = Column(JSON, nullable=True)
    parsed_storage_path = Column(String(500), nullable=True)
    status = Column(String(20), nullable=False, default="active")
    created_at = Column(DateTime, nullable=False, default=now_beijing)
    updated_at = Column(DateTime, nullable=False, default=now_beijing, onupdate=now_beijing)


class BizAnswerSheet(Base):
    """学生答卷表"""

    __tablename__ = "biz_answer_sheet"
    __table_args__ = (
        Index("idx_biz_answer_exam_student", "exam_id", "student_no"),
        Index("idx_biz_answer_question", "question_id"),
        Index("idx_biz_answer_package", "package_id"),
        {"extend_existing": True},
    )

    answer_id = Column(Integer, primary_key=True, autoincrement=True)
    student_no = Column(String(50), nullable=False)
    exam_id = Column(Integer, ForeignKey("biz_exam.id"), nullable=False)
    package_id = Column(
        Integer,
        ForeignKey("biz_exam_package.package_id"),
        nullable=True,
    )
    question_id = Column(Integer, nullable=True)
    upload_type = Column(String(20), nullable=False)
    file_path = Column(String(255), nullable=True)
    text_content = Column(Text, nullable=True)
    page_mapping = Column(JSON, nullable=True)
    uploaded_at = Column(DateTime, nullable=False, default=now_beijing)


class BizAnswerFile(Base):
    """考生答卷文件明细表"""

    __tablename__ = "biz_answer_file"
    __table_args__ = (
        Index("idx_biz_answer_file_answer", "answer_id"),
        Index("idx_biz_answer_file_package", "package_id"),
        Index("idx_biz_answer_file_student", "exam_id", "package_id", "student_no"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    answer_id = Column(Integer, ForeignKey("biz_answer_sheet.answer_id"), nullable=False)
    exam_id = Column(Integer, ForeignKey("biz_exam.id"), nullable=False)
    package_id = Column(Integer, ForeignKey("biz_exam_package.package_id"), nullable=False)
    student_no = Column(String(50), nullable=False)
    file_name = Column(String(255), nullable=False)
    file_path = Column(String(255), nullable=True)
    file_type = Column(String(20), nullable=False)
    relative_path = Column(String(500), nullable=False)
    file_size = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=now_beijing)


class BizExamGradingStandard(Base):
    """考试阅卷标准版本表"""

    __tablename__ = "biz_exam_grading_standard"
    __table_args__ = (
        Index("idx_biz_exam_standard_exam", "subject_id", "exam_id"),
        Index("idx_biz_exam_standard_creator", "creator_id"),
        Index("idx_biz_exam_standard_version", "exam_id", "version_no"),
        {"extend_existing": True},
    )

    standard_id = Column(Integer, primary_key=True, autoincrement=True)
    subject_id = Column(Integer, ForeignKey("biz_subject.subject_id"), nullable=False)
    exam_id = Column(Integer, ForeignKey("biz_exam.id"), nullable=False)
    creator_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    version_no = Column(Integer, nullable=False)
    standard_items = Column(JSON, nullable=False)
    remark = Column(String(255), nullable=True)
    created_at = Column(DateTime, nullable=False, default=now_beijing)
    updated_at = Column(DateTime, nullable=False, default=now_beijing, onupdate=now_beijing)

class BizExamGradingStandardItem(Base):
    """考试阅卷标准题目明细表。"""

    __tablename__ = "biz_exam_grading_standard_item"
    __table_args__ = (
        Index("idx_biz_exam_standard_item_standard", "standard_id"),
        Index("idx_biz_exam_standard_item_exam_question", "subject_id", "exam_id", "question_no"),
        Index("idx_biz_exam_standard_item_creator", "creator_id"),
        Index("idx_biz_exam_standard_item_question_version", "exam_id", "question_no", "question_version_no"),
        {"extend_existing": True},
    )

    item_id = Column(Integer, primary_key=True, autoincrement=True)
    standard_id = Column(Integer, ForeignKey("biz_exam_grading_standard.standard_id"), nullable=False)
    subject_id = Column(Integer, ForeignKey("biz_subject.subject_id"), nullable=False)
    exam_id = Column(Integer, ForeignKey("biz_exam.id"), nullable=False)
    creator_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    question_no = Column(String(50), nullable=False)
    question_type = Column(String(50), nullable=False)
    mcp_service_id = Column(String(128), nullable=False)
    mcp_service_name = Column(String(255), nullable=True)
    question_text = Column(Text, nullable=True)
    reference_answer = Column(Text, nullable=True)
    grading_criteria = Column(Text, nullable=True)
    question_version_no = Column(Integer, nullable=False, default=1)
    source_standard_version_no = Column(Integer, nullable=True)
    mcp_params = Column(JSON, nullable=True)
    max_score = Column(Numeric(8, 2), nullable=True)
    remark = Column(String(255), nullable=True)
    created_at = Column(DateTime, nullable=False, default=now_beijing)
    updated_at = Column(DateTime, nullable=False, default=now_beijing, onupdate=now_beijing)

class BizExamGradingTemplate(Base):
    """考试阅卷模板表。"""

    __tablename__ = "biz_exam_grading_template"
    __table_args__ = (
        Index("idx_biz_exam_template_exam", "subject_id", "exam_id"),
        Index("idx_biz_exam_template_creator", "creator_id"),
        Index("idx_biz_exam_template_created", "created_at"),
        {"extend_existing": True},
    )

    template_id = Column(Integer, primary_key=True, autoincrement=True)
    subject_id = Column(Integer, ForeignKey("biz_subject.subject_id"), nullable=False)
    exam_id = Column(Integer, ForeignKey("biz_exam.id"), nullable=False)
    creator_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    template_name = Column(String(100), nullable=False, default="科目考试阅卷模板")
    template_items = Column(JSON, nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=now_beijing)
    updated_at = Column(DateTime, nullable=False, default=now_beijing, onupdate=now_beijing)

class BizLocalGradingRecord(Base):
    """本地试卷包评分结果表。"""

    __tablename__ = "biz_local_grading_record"
    __table_args__ = (
        Index("idx_biz_local_record_package_question", "local_package_id", "student_no", "question_no"),
        Index("idx_biz_local_record_exam_student", "exam_id", "student_no"),
        Index("idx_biz_local_record_fingerprint", "package_fingerprint"),
        {"extend_existing": True},
    )

    record_id = Column(Integer, primary_key=True, autoincrement=True)
    local_package_id = Column(
        Integer,
        ForeignKey("biz_local_exam_package.local_package_id"),
        nullable=False,
    )
    subject_id = Column(Integer, ForeignKey("biz_subject.subject_id"), nullable=False)
    exam_id = Column(Integer, ForeignKey("biz_exam.id"), nullable=False)
    creator_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    package_fingerprint = Column(String(128), nullable=False)
    student_no = Column(String(50), nullable=False)
    question_no = Column(String(50), nullable=False)
    question_label = Column(String(100), nullable=True)
    grading_mode = Column(String(30), nullable=True)
    mcp_service_id = Column(String(128), nullable=True)
    mcp_service_name = Column(String(255), nullable=True)
    template_id = Column(Integer, nullable=True)
    template_name = Column(String(100), nullable=True)
    standard_label = Column(String(255), nullable=True)
    standard_id = Column(
        Integer,
        ForeignKey("biz_exam_grading_standard.standard_id"),
        nullable=True,
    )
    standard_version_no = Column(Integer, nullable=True)
    question_version_no = Column(Integer, nullable=True)
    score = Column(Numeric(8, 2), nullable=True)
    max_score = Column(Numeric(8, 2), nullable=True)
    grading_desc = Column(Text, nullable=True)
    question_text = Column(Text, nullable=True)
    answer_text = Column(Text, nullable=True)
    image_name = Column(String(255), nullable=True)
    mcp_raw_json = Column(JSON, nullable=True)
    is_trial = Column(Boolean, nullable=False, default=False)
    round_note = Column(String(255), nullable=True)
    round_starred = Column(Boolean, nullable=False, default=False)
    graded_at = Column(DateTime, nullable=False, default=now_beijing)
    updated_at = Column(DateTime, nullable=False, default=now_beijing, onupdate=now_beijing)
