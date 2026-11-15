-- 정기결제(빌링) (backend #51). 운영 DB 는 AUTO_CREATE_TABLES 가 꺼져 있어 수동으로 실행한다.
-- plans · payments · subscriptions 는 이미 있는 테이블이다. 없으면 먼저 알려 주세요 (SHOW TABLES LIKE 'p%';)
-- TiDB 웹 편집기는 여러 문장을 한 번에 넣으면 일부만 실행하므로 한 문장씩 실행한다.

ALTER TABLE payments ADD COLUMN IF NOT EXISTS user_id INT NULL COMMENT '회원별 결제 조회 (backend #51)';
ALTER TABLE payments ADD INDEX ix_payments_user_id (user_id);

ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS billing_key_id INT NULL COMMENT '결제 수단 (backend #51)';
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS canceled_at DATETIME NULL;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS cancel_reason VARCHAR(200) NULL;

CREATE TABLE IF NOT EXISTS billing_keys (
  id INT AUTO_INCREMENT PRIMARY KEY,
  user_id INT NOT NULL,
  customer_key VARCHAR(64) NOT NULL COMMENT '토스 customerKey (회원별 고정)',
  billing_key_enc VARBINARY(512) NOT NULL COMMENT '암호화한 빌링키',
  card_company VARCHAR(30) NULL,
  card_number_masked VARCHAR(30) NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
  mode VARCHAR(10) NOT NULL COMMENT 'TEST·LIVE',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  revoked_at DATETIME NULL,
  INDEX ix_billing_keys_user_id (user_id),
  INDEX ix_billing_keys_customer_key (customer_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS billing_charges (
  id INT AUTO_INCREMENT PRIMARY KEY,
  subscription_id INT NOT NULL,
  billing_key_id INT NOT NULL,
  period_start DATE NOT NULL,
  period_end DATE NOT NULL,
  amount INT NOT NULL,
  order_id VARCHAR(64) NOT NULL,
  idempotency_key VARCHAR(64) NOT NULL,
  attempt INT NOT NULL DEFAULT 1,
  status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
  failure_code VARCHAR(100) NULL,
  failure_message VARCHAR(500) NULL,
  next_retry_at DATETIME NULL,
  payment_id INT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_billing_charge_period (subscription_id, period_start),
  UNIQUE KEY uq_billing_charges_order_id (order_id),
  UNIQUE KEY uq_billing_charges_idempotency_key (idempotency_key),
  INDEX ix_billing_charges_status (status),
  INDEX ix_billing_charges_next_retry_at (next_retry_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS payment_cancels (
  id INT AUTO_INCREMENT PRIMARY KEY,
  payment_id INT NOT NULL,
  cancel_amount INT NOT NULL,
  cancel_reason VARCHAR(200) NOT NULL,
  transaction_key VARCHAR(64) NULL,
  canceled_by_admin_id INT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX ix_payment_cancels_payment_id (payment_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS billing_webhook_events (
  id INT AUTO_INCREMENT PRIMARY KEY,
  event_type VARCHAR(50) NULL,
  payment_key VARCHAR(255) NULL,
  order_id VARCHAR(255) NULL,
  payload_json JSON NULL,
  verified BOOLEAN NOT NULL DEFAULT FALSE,
  processed_at DATETIME NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX ix_billing_webhook_events_payment_key (payment_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
