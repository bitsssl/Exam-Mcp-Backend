-- users 表补充联系电话字段
-- MySQL 8.0+ 可直接执行。

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS phone VARCHAR(20) NULL COMMENT '联系电话' AFTER fullname;
