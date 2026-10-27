-- Gideon database (gideon_db). Import this in phpMyAdmin (Import tab), or just start server.py:
-- it runs this file on startup (and upgrades an older database), so nothing is duplicated.

CREATE DATABASE IF NOT EXISTS gideon_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE gideon_db;

-- People who can log in. Passwords are stored as plain text (no hashing).
-- Each wrong password uses up one of the 5 login attempts. At 0 the account is disabled.
-- A correct password puts the attempts back to 5.
-- role: 'admin' sees the admin dashboard, 'user' sees the study companion.
-- api_key: this user's own Google Gemini API key (used when they talk to Gideon).
CREATE TABLE IF NOT EXISTS users (
  id             INT UNSIGNED NOT NULL AUTO_INCREMENT,
  username       VARCHAR(50) NOT NULL,
  first_name     VARCHAR(50) NULL,
  role           ENUM('user','admin') NOT NULL DEFAULT 'user',
  password       VARCHAR(255) NOT NULL,
  api_key        VARCHAR(255) NULL,
  login_attempts_left  TINYINT UNSIGNED NOT NULL DEFAULT 5,
  status         ENUM('active','disabled') NOT NULL DEFAULT 'active',
  disabled_at    DATETIME NULL,
  created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_login_at  DATETIME NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_users_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- One row per signed-in browser. Only a hash of the cookie token is kept.
CREATE TABLE IF NOT EXISTS login_sessions (
  token_hash  CHAR(64) NOT NULL,
  user_id     INT UNSIGNED NOT NULL,
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at  DATETIME NOT NULL,
  PRIMARY KEY (token_hash),
  KEY idx_login_sessions_expiry (expires_at),
  CONSTRAINT fk_login_sessions_user FOREIGN KEY (user_id)
    REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- A chat is a topic or subject (for example "Biology exam"). Each chat has its own history and memory.
CREATE TABLE IF NOT EXISTS chats (
  id          INT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id     INT UNSIGNED NOT NULL,
  title       VARCHAR(100) NOT NULL,
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_chats_user (user_id, updated_at),
  CONSTRAINT fk_chats_user FOREIGN KEY (user_id)
    REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- One row per time you press "Start talking". It belongs to one chat.
CREATE TABLE IF NOT EXISTS conversations (
  id          INT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id     INT UNSIGNED NULL,
  chat_id     INT UNSIGNED NULL,
  started_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  ended_at    DATETIME NULL,
  model       VARCHAR(64) NOT NULL,
  voice       VARCHAR(32) NOT NULL,
  PRIMARY KEY (id),
  KEY idx_conversations_user (user_id, id),
  KEY idx_conversations_chat (chat_id, id),
  CONSTRAINT fk_conversations_user FOREIGN KEY (user_id)
    REFERENCES users (id) ON DELETE CASCADE,
  CONSTRAINT fk_conversations_chat FOREIGN KEY (chat_id)
    REFERENCES chats (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- One row per spoken or typed turn (not per word), so the table stays small and readable.
CREATE TABLE IF NOT EXISTS messages (
  id               INT UNSIGNED NOT NULL AUTO_INCREMENT,
  conversation_id  INT UNSIGNED NOT NULL,
  role             ENUM('user','gideon') NOT NULL,
  channel          ENUM('voice','text') NOT NULL,
  content          TEXT NOT NULL,
  created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_messages_conversation (conversation_id, id),
  CONSTRAINT fk_messages_conversation FOREIGN KEY (conversation_id)
    REFERENCES conversations (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Timers are run by Python code. They are stored so they survive a page refresh.
CREATE TABLE IF NOT EXISTS timers (
  id                INT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id           INT UNSIGNED NULL,
  conversation_id   INT UNSIGNED NULL,
  label             VARCHAR(100) NOT NULL,
  duration_seconds  INT UNSIGNED NOT NULL,
  started_at        DATETIME NOT NULL,
  ends_at           DATETIME NOT NULL,
  status            ENUM('running','finished','cancelled','missed') NOT NULL DEFAULT 'running',
  PRIMARY KEY (id),
  KEY idx_timers_status (status, ends_at),
  KEY idx_timers_user (user_id, status),
  CONSTRAINT fk_timers_user FOREIGN KEY (user_id)
    REFERENCES users (id) ON DELETE CASCADE,
  CONSTRAINT fk_timers_conversation FOREIGN KEY (conversation_id)
    REFERENCES conversations (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Easy-to-read views: open these in phpMyAdmin to browse your data.
CREATE OR REPLACE VIEW v_users AS
SELECT id, username, first_name, role, status, login_attempts_left,
       CASE WHEN api_key IS NULL OR api_key = '' THEN 'no' ELSE 'yes' END AS has_api_key,
       disabled_at, created_at, last_login_at
FROM users
ORDER BY id;

CREATE OR REPLACE VIEW v_chat_history AS
SELECT
  u.username                                     AS user_name,
  ch.title                                       AS chat,
  c.id                                           AS conversation,
  c.started_at                                   AS conversation_started,
  m.created_at                                   AS sent_at,
  CASE m.role WHEN 'user' THEN 'You' ELSE 'Gideon' END AS speaker,
  m.channel                                      AS via,
  m.content                                      AS message
FROM messages m
JOIN conversations c ON c.id = m.conversation_id
LEFT JOIN users u ON u.id = c.user_id
LEFT JOIN chats ch ON ch.id = c.chat_id
ORDER BY c.id, m.id;

CREATE OR REPLACE VIEW v_timers AS
SELECT
  u.username                      AS user_name,
  t.id,
  t.label,
  SEC_TO_TIME(t.duration_seconds) AS duration,
  t.started_at,
  t.ends_at,
  t.status
FROM timers t
LEFT JOIN users u ON u.id = t.user_id
ORDER BY t.id DESC;