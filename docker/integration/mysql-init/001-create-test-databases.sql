CREATE DATABASE IF NOT EXISTS raw_dingtalk_test
    CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS raw_wdt_test
    CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS mart_ops_test
    CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE USER IF NOT EXISTS 'public_data_test'@'%'
    IDENTIFIED BY 'public-data-test-password';
GRANT ALL PRIVILEGES ON raw_dingtalk_test.* TO 'public_data_test'@'%';
GRANT ALL PRIVILEGES ON raw_wdt_test.* TO 'public_data_test'@'%';
GRANT ALL PRIVILEGES ON mart_ops_test.* TO 'public_data_test'@'%';
FLUSH PRIVILEGES;
