import os
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))


def _abs(path):
    """Anchor relative paths to the project dir so cwd never matters."""
    return path if os.path.isabs(path) else os.path.join(BASE_DIR, path)


class Config:
    # 'mysql' for production (default), 'sqlite' for zero-setup local dev/testing
    DB_BACKEND = os.getenv("DB_BACKEND", "mysql").lower()
    SQLITE_PATH = _abs(os.getenv("SQLITE_PATH", "dev.db"))

    DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
    DB_PORT = int(os.getenv("DB_PORT", "3306"))
    DB_USER = os.getenv("DB_USER", "root")
    DB_PASSWORD = os.getenv("DB_PASSWORD", "")
    DB_NAME = os.getenv("DB_NAME", "task_tracker")

    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
    DEBUG = os.getenv("FLASK_DEBUG", "0") == "1"

    # Folder where /uploads/YYYY/MM/DD/ trees are created
    UPLOAD_ROOT = _abs(os.getenv("UPLOAD_ROOT", "uploads"))

    # Max upload size (16 MB) — phone photos are well under this
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024
