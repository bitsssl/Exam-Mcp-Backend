-- 智能阅卷业务表建表脚本
-- 说明：
-- 1. 原系统 users、mcp_services 等表继续保留并复用。
-- 2. 本脚本只创建新增 biz_* 阅卷业务表。
-- 3. 请先确保 users、mcp_services 表已经存在。

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

CREATE TABLE IF NOT EXISTS biz_subject (
    subject_id BIGINT NOT NULL AUTO_INCREMENT COMMENT '科目唯一标识',
    subject_no VARCHAR(50) NOT NULL COMMENT '科目号',
    subject_name VARCHAR(100) NOT NULL COMMENT '科目名',
    creator_id BIGINT NOT NULL COMMENT '创建人，对应 users.id',
    created_at DATETIME NOT NULL COMMENT '创建时间',
    remark VARCHAR(255) NULL COMMENT '备注说明',
    PRIMARY KEY (subject_id),
    KEY idx_biz_subject_creator (creator_id),
    KEY idx_biz_subject_no_creator (subject_no, creator_id),
    CONSTRAINT fk_biz_subject_creator
        FOREIGN KEY (creator_id) REFERENCES users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='科目表';

CREATE TABLE IF NOT EXISTS biz_exam (
    id BIGINT NOT NULL AUTO_INCREMENT COMMENT '考试唯一标识',
    subject_id BIGINT NOT NULL COMMENT '关联科目ID',
    exam_name VARCHAR(100) NOT NULL COMMENT '考试名称',
    exam_time DATETIME NULL COMMENT '考试时间',
    answer_key_path VARCHAR(255) NULL COMMENT '参考答案文件路径',
    blank_paper_path VARCHAR(255) NULL COMMENT '空白试卷文件路径',
    creator_id BIGINT NOT NULL COMMENT '创建人，对应 users.id',
    PRIMARY KEY (id),
    KEY idx_biz_exam_subject (subject_id),
    KEY idx_biz_exam_creator (creator_id),
    CONSTRAINT fk_biz_exam_subject
        FOREIGN KEY (subject_id) REFERENCES biz_subject (subject_id),
    CONSTRAINT fk_biz_exam_creator
        FOREIGN KEY (creator_id) REFERENCES users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='考试表';

CREATE TABLE IF NOT EXISTS biz_exam_package (
    package_id BIGINT NOT NULL AUTO_INCREMENT COMMENT '试卷包唯一标识',
    exam_id BIGINT NOT NULL COMMENT '关联考试ID',
    package_name VARCHAR(100) NOT NULL COMMENT '试卷包名称',
    file_path VARCHAR(255) NOT NULL COMMENT '原始压缩包文件路径',
    extract_path VARCHAR(255) NULL COMMENT '解压后的文件夹路径',
    uploaded_at DATETIME NOT NULL COMMENT '上传时间',
    PRIMARY KEY (package_id),
    KEY idx_biz_exam_package_exam (exam_id),
    CONSTRAINT fk_biz_exam_package_exam
        FOREIGN KEY (exam_id) REFERENCES biz_exam (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='试卷包表';

CREATE TABLE IF NOT EXISTS biz_question (
    question_id BIGINT NOT NULL AUTO_INCREMENT COMMENT '题目唯一标识',
    exam_id BIGINT NOT NULL COMMENT '关联考试ID',
    major_question_no VARCHAR(20) NOT NULL COMMENT '大题目号',
    minor_question_no VARCHAR(20) NULL COMMENT '小题目号',
    question_content TEXT NULL COMMENT '题目内容说明',
    PRIMARY KEY (question_id),
    KEY idx_biz_question_exam (exam_id),
    KEY idx_biz_question_no (exam_id, major_question_no, minor_question_no),
    CONSTRAINT fk_biz_question_exam
        FOREIGN KEY (exam_id) REFERENCES biz_exam (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='试题表';

CREATE TABLE IF NOT EXISTS biz_answer_sheet (
    answer_id BIGINT NOT NULL AUTO_INCREMENT COMMENT '答卷唯一标识',
    student_no VARCHAR(50) NOT NULL COMMENT '学号',
    exam_id BIGINT NOT NULL COMMENT '关联考试ID',
    package_id BIGINT NULL COMMENT '关联试卷包ID',
    question_id BIGINT NULL COMMENT '关联题目ID，整份答卷可为空',
    upload_type VARCHAR(20) NOT NULL COMMENT '上传类型：Excel, Word, Image, PDF, Zip',
    file_path VARCHAR(255) NULL COMMENT '文件存储路径',
    text_content TEXT NULL COMMENT '文本作答内容',
    page_mapping JSON NULL COMMENT '题号与PDF页码映射',
    uploaded_at DATETIME NOT NULL COMMENT '上传时间',
    PRIMARY KEY (answer_id),
    KEY idx_biz_answer_exam_student (exam_id, student_no),
    KEY idx_biz_answer_question (question_id),
    KEY idx_biz_answer_package (package_id),
    CONSTRAINT fk_biz_answer_exam
        FOREIGN KEY (exam_id) REFERENCES biz_exam (id),
    CONSTRAINT fk_biz_answer_package
        FOREIGN KEY (package_id) REFERENCES biz_exam_package (package_id),
    CONSTRAINT fk_biz_answer_question
        FOREIGN KEY (question_id) REFERENCES biz_question (question_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='学生答卷表';

CREATE TABLE IF NOT EXISTS biz_answer_file (
    id BIGINT NOT NULL AUTO_INCREMENT COMMENT '答卷文件明细唯一标识',
    answer_id BIGINT NOT NULL COMMENT '关联学生答卷ID',
    exam_id BIGINT NOT NULL COMMENT '关联考试ID',
    package_id BIGINT NOT NULL COMMENT '关联试卷包ID',
    student_no VARCHAR(50) NOT NULL COMMENT '学号',
    file_name VARCHAR(255) NOT NULL COMMENT '文件名',
    file_path VARCHAR(255) NULL COMMENT '所在试卷包或文件路径',
    file_type VARCHAR(20) NOT NULL COMMENT '文件类型',
    relative_path VARCHAR(500) NOT NULL COMMENT '文件在试卷包中的相对路径',
    file_size BIGINT NULL COMMENT '文件大小，单位字节',
    created_at DATETIME NOT NULL COMMENT '创建时间',
    PRIMARY KEY (id),
    KEY idx_biz_answer_file_answer (answer_id),
    KEY idx_biz_answer_file_package (package_id),
    KEY idx_biz_answer_file_student (exam_id, package_id, student_no),
    CONSTRAINT fk_biz_answer_file_answer
        FOREIGN KEY (answer_id) REFERENCES biz_answer_sheet (answer_id),
    CONSTRAINT fk_biz_answer_file_exam
        FOREIGN KEY (exam_id) REFERENCES biz_exam (id),
    CONSTRAINT fk_biz_answer_file_package
        FOREIGN KEY (package_id) REFERENCES biz_exam_package (package_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='考生答卷文件明细表';

CREATE TABLE IF NOT EXISTS biz_grading_template (
    template_id BIGINT NOT NULL AUTO_INCREMENT COMMENT '模板唯一标识',
    exam_id BIGINT NOT NULL COMMENT '关联考试ID',
    template_name VARCHAR(100) NOT NULL COMMENT '模板名称',
    remark VARCHAR(255) NULL COMMENT '模板说明',
    creator_id BIGINT NOT NULL COMMENT '创建人，对应 users.id',
    created_at DATETIME NOT NULL COMMENT '创建日期',
    PRIMARY KEY (template_id),
    KEY idx_biz_template_exam (exam_id),
    KEY idx_biz_template_creator (creator_id),
    CONSTRAINT fk_biz_template_exam
        FOREIGN KEY (exam_id) REFERENCES biz_exam (id),
    CONSTRAINT fk_biz_template_creator
        FOREIGN KEY (creator_id) REFERENCES users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='阅卷模板主表';

CREATE TABLE IF NOT EXISTS biz_grading_standard (
    standard_id BIGINT NOT NULL AUTO_INCREMENT COMMENT '题阅标准唯一标识',
    question_id BIGINT NOT NULL COMMENT '关联试题ID',
    mcp_service_id BIGINT NOT NULL COMMENT '绑定的 MCP 服务ID，对应 mcp_services.id',
    grading_criteria JSON NOT NULL COMMENT '评分标准及MCP参数',
    version_no VARCHAR(50) NOT NULL COMMENT '版本号',
    version_name VARCHAR(100) NULL COMMENT '版本命名',
    parent_std_id BIGINT NULL COMMENT '父标准ID',
    question_content TEXT NULL COMMENT '题目详细内容',
    reference_answer TEXT NULL COMMENT '参考答案',
    PRIMARY KEY (standard_id),
    KEY idx_biz_standard_question (question_id),
    KEY idx_biz_standard_mcp_service (mcp_service_id),
    KEY idx_biz_standard_parent (parent_std_id),
    CONSTRAINT fk_biz_standard_question
        FOREIGN KEY (question_id) REFERENCES biz_question (question_id),
    CONSTRAINT fk_biz_standard_mcp_service
        FOREIGN KEY (mcp_service_id) REFERENCES mcp_services (id),
    CONSTRAINT fk_biz_standard_parent
        FOREIGN KEY (parent_std_id) REFERENCES biz_grading_standard (standard_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='题阅标准表';

CREATE TABLE IF NOT EXISTS biz_template_detail (
    id BIGINT NOT NULL AUTO_INCREMENT COMMENT '明细记录唯一标识',
    template_id BIGINT NOT NULL COMMENT '关联模板主表ID',
    exam_id BIGINT NOT NULL COMMENT '关联考试ID',
    question_id BIGINT NOT NULL COMMENT '关联题目ID',
    standard_id BIGINT NOT NULL COMMENT '关联评阅标准ID',
    PRIMARY KEY (id),
    KEY idx_biz_template_detail_template (template_id),
    KEY idx_biz_template_detail_exam (exam_id),
    KEY idx_biz_template_detail_question (question_id),
    CONSTRAINT fk_biz_template_detail_template
        FOREIGN KEY (template_id) REFERENCES biz_grading_template (template_id),
    CONSTRAINT fk_biz_template_detail_exam
        FOREIGN KEY (exam_id) REFERENCES biz_exam (id),
    CONSTRAINT fk_biz_template_detail_question
        FOREIGN KEY (question_id) REFERENCES biz_question (question_id),
    CONSTRAINT fk_biz_template_detail_standard
        FOREIGN KEY (standard_id) REFERENCES biz_grading_standard (standard_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='阅卷模板明细表';

CREATE TABLE IF NOT EXISTS biz_grading_record (
    record_id BIGINT NOT NULL AUTO_INCREMENT COMMENT '阅卷记录唯一标识',
    exam_id BIGINT NOT NULL COMMENT '关联考试ID',
    standard_id BIGINT NOT NULL COMMENT '关联题阅标准ID',
    student_no VARCHAR(50) NOT NULL COMMENT '考号/学号',
    major_question_no VARCHAR(20) NOT NULL COMMENT '大题目号',
    minor_question_no VARCHAR(20) NULL COMMENT '小题目号',
    score DECIMAL(5,2) NULL COMMENT '评分得分',
    grading_desc TEXT NULL COMMENT '评分依据及说明',
    mcp_raw_json JSON NULL COMMENT 'MCP服务返回原始JSON',
    is_trial TINYINT NOT NULL DEFAULT 0 COMMENT '0-正式评阅，1-试阅',
    trial_version VARCHAR(50) NULL COMMENT '试阅批次版本号',
    graded_at DATETIME NOT NULL COMMENT '评阅时间',
    PRIMARY KEY (record_id),
    KEY idx_biz_record_exam_student (exam_id, student_no),
    KEY idx_biz_record_standard (standard_id),
    KEY idx_biz_record_question_no (major_question_no, minor_question_no),
    CONSTRAINT fk_biz_record_exam
        FOREIGN KEY (exam_id) REFERENCES biz_exam (id),
    CONSTRAINT fk_biz_record_standard
        FOREIGN KEY (standard_id) REFERENCES biz_grading_standard (standard_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='阅卷结果/评分记录表';

SET FOREIGN_KEY_CHECKS = 1;
