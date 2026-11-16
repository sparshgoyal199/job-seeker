"""
Centralized Configuration and Path Management.
"""

import sys
from pathlib import Path
from dotenv import load_dotenv

# Ensure Windows terminal handles emojis and utf-8 cleanly
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Load environment variables from .env
load_dotenv(PROJECT_ROOT / ".env")

DB_PATH = PROJECT_ROOT / "jobviareferral.db"
LOGS_DIR = PROJECT_ROOT / "logs"
LOGS_DIR.mkdir(exist_ok=True)

RESUME_PATH = PROJECT_ROOT / "resume.pdf"
PROFILE_PATH = PROJECT_ROOT / "candidate_profile.json"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
