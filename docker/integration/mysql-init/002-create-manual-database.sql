-- 人工报表导入通道（C 类数据源）的 raw 库。
-- 独立成 002 而非改 001：001 已在存量环境执行过，改它会让已初始化的数据
-- 目录不再重放（docker-entrypoint-initdb.d 只在数据目录为空时跑）。
CREATE DATABASE IF NOT EXISTS raw_manual_test
    CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

GRANT ALL PRIVILEGES ON raw_manual_test.* TO 'public_data_test'@'%';
FLUSH PRIVILEGES;
