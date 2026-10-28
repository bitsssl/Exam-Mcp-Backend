-- 考生答卷文件明细表
SET NAMES utf8mb4;

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
