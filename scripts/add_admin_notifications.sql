-- 관리자 알림 (backoffice F-016). 앱 시작 시 create_all 로도 생긴다.
CREATE TABLE IF NOT EXISTS admin_notifications (
  id INT AUTO_INCREMENT PRIMARY KEY,
  admin_id INT NOT NULL,
  type VARCHAR(40) NOT NULL,
  title VARCHAR(255) NOT NULL,
  content TEXT NULL,
  link VARCHAR(255) NULL,
  read_at DATETIME NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT fk_admin_noti_admin FOREIGN KEY (admin_id) REFERENCES admins(id) ON DELETE CASCADE,
  INDEX ix_admin_noti_admin_created (admin_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
