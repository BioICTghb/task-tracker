-- Task Tracker — MySQL schema
-- Run once:  mysql -u root -p < schema.sql

CREATE DATABASE IF NOT EXISTS task_tracker
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE task_tracker;

-- One task per pasted line
CREATE TABLE IF NOT EXISTS tasks (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  title       TEXT NOT NULL,
  status      ENUM('open', 'started', 'completed') NOT NULL DEFAULT 'open',
  created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  started_at  TIMESTAMP NULL DEFAULT NULL,
  completed_at TIMESTAMP NULL DEFAULT NULL,
  is_extra    TINYINT NOT NULL DEFAULT 0,  -- 1 = unplanned job created by a technician
  client_key  VARCHAR(64) NULL,            -- idempotency key for offline-created extra jobs
  started_by   VARCHAR(100) NULL,          -- operator who took the before photo
  completed_by VARCHAR(100) NULL,          -- operator who took the after photo
  UNIQUE KEY uniq_client_key (client_key),
  INDEX idx_status (status),
  INDEX idx_created (created_at)
) ENGINE=InnoDB;

-- Before / after photos. Files live on disk; only the path is stored here.
CREATE TABLE IF NOT EXISTS task_photos (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  task_id     INT NOT NULL,
  -- before/after are the bookends; progress1..4 are optional photos taken in between
  photo_type  ENUM('before', 'after',
                   'progress1', 'progress2', 'progress3', 'progress4') NOT NULL,
  file_path   VARCHAR(255) NOT NULL,
  timestamp   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
  UNIQUE KEY uniq_task_phototype (task_id, photo_type),
  INDEX idx_task (task_id)
) ENGINE=InnoDB;

-- Login accounts for managers and technicians
CREATE TABLE IF NOT EXISTS users (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  username      VARCHAR(64) NOT NULL UNIQUE,
  password_hash VARCHAR(255) NOT NULL,
  display_name  VARCHAR(100) NOT NULL,
  role          ENUM('manager','technician') NOT NULL DEFAULT 'technician',
  active        TINYINT NOT NULL DEFAULT 1,
  created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;
