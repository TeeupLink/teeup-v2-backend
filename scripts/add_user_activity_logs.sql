-- 회원 활동 로그 (backend #50). 앱 시작 시 create_all 로도 만들어지지만, 운영에서는 이 스크립트로 먼저 만든다.
-- 설계: teeup-v2-backoffice docs/03.구현문서/데이터베이스설계서.md 4.2. 보관 1년 (scripts/purge_user_activity_logs.py)

CREATE TABLE IF NOT EXISTS user_activity_logs (
  id BIGINT NOT NULL AUTO_INCREMENT,
  user_id INT NOT NULL,
  event_type VARCHAR(40) NOT NULL,
  target_type VARCHAR(30) NULL,
  target_id INT NULL,
  summary VARCHAR(255) NOT NULL COMMENT '표시용 요약. 대상이 삭제돼도 의미 유지',
  meta_json JSON NULL COMMENT 'device, app_version, provider 등',
  ip VARCHAR(45) NULL,
  created_at DATETIME NOT NULL,
  PRIMARY KEY (id),
  KEY ix_act_user_created (user_id, created_at),
  KEY ix_act_created (created_at),
  KEY ix_act_type_created (event_type, created_at),
  CONSTRAINT fk_act_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
