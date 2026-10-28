-- 관리자 감사 로그 (backend #49). 앱 시작 시 create_all 로도 만들어지지만, 운영에서는 이 스크립트로 먼저 만든다.
-- 설계: teeup-v2-backoffice docs/03.구현문서/데이터베이스설계서.md 4.1·4.8

CREATE TABLE IF NOT EXISTS admin_audit_logs (
  id BIGINT NOT NULL AUTO_INCREMENT,
  request_id VARCHAR(36) NOT NULL COMMENT '요청 단위 UUID. 일괄 처리 건별 행을 묶음',
  actor_admin_id INT NULL,
  actor_email VARCHAR(255) NOT NULL COMMENT '삭제된 관리자도 식별되도록 스냅샷',
  actor_role VARCHAR(30) NULL,
  action VARCHAR(30) NOT NULL,
  target_type VARCHAR(40) NULL,
  target_id VARCHAR(64) NULL,
  before_json JSON NULL,
  after_json JSON NULL,
  reason VARCHAR(500) NULL,
  meta_json JSON NULL,
  method VARCHAR(10) NOT NULL,
  path VARCHAR(255) NOT NULL,
  status_code SMALLINT NOT NULL,
  result VARCHAR(10) NOT NULL COMMENT 'SUCCESS·FAIL',
  ip VARCHAR(45) NOT NULL,
  user_agent VARCHAR(500) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY ix_audit_created (created_at),
  KEY ix_audit_actor_created (actor_admin_id, created_at),
  KEY ix_audit_target (target_type, target_id),
  KEY ix_audit_action_created (action, created_at),
  KEY ix_audit_request (request_id),
  CONSTRAINT fk_audit_actor FOREIGN KEY (actor_admin_id) REFERENCES admins (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS audit_monthly_reviews (
  id INT NOT NULL AUTO_INCREMENT,
  month VARCHAR(7) NOT NULL COMMENT 'YYYY-MM',
  reviewer_admin_id INT NOT NULL,
  summary_json JSON NULL COMMENT '점검 시점 집계 스냅샷',
  memo TEXT NULL,
  reviewed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_audit_review_month (month),
  CONSTRAINT fk_audit_review_admin FOREIGN KEY (reviewer_admin_id) REFERENCES admins (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 운영 권고: 앱 DB 계정에서 수정·삭제 권한을 회수한다 (보관 정리는 별도 계정 배치로, 2년 경과분만)
-- REVOKE UPDATE, DELETE ON teeupdb.admin_audit_logs FROM 'golfmaster'@'%';
