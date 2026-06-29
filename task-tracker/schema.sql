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
  INDEX idx_status (status),
  INDEX idx_created (created_at)
) ENGINE=InnoDB;

-- Before / after photos. Files live on disk; only the path is stored here.
CREATE TABLE IF NOT EXISTS task_photos (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  task_id     INT NOT NULL,
  photo_type  ENUM('before', 'after') NOT NULL,
  file_path   VARCHAR(255) NOT NULL,
  timestamp   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
  UNIQUE KEY uniq_task_phototype (task_id, photo_type),
  INDEX idx_task (task_id)
) ENGINE=InnoDB;
