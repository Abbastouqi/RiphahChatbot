"""Central configuration: paths, models, and the Riphah data-source map.

Every ID table below was read off the live site (the chained <select> elements on
/admissions/fee-structure/ and /admissions/dates/). If Riphah renumbers a
faculty or adds a campus, this file is the only place that changes.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

# --- paths ---
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"          # cached HTML/PDF, so re-parsing never re-fetches
DB_PATH = DATA_DIR / "kb.sqlite3"

# --- models ---
EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-large")
EMBED_DIMENSIONS = int(os.getenv("EMBED_DIMENSIONS", "1536"))
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5")
# Which provider answers text chat: "anthropic" (Claude), "openai", or
# "ollama" (a local model, free — good for testing without API credits).
# Voice always runs on OpenAI Realtime regardless of this switch.
TEXT_PROVIDER = os.getenv("TEXT_PROVIDER", "anthropic").strip().lower()
OPENAI_TEXT_MODEL = os.getenv("OPENAI_TEXT_MODEL", "gpt-5.6-terra")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
OLLAMA_TEXT_MODEL = os.getenv("OLLAMA_TEXT_MODEL", "qwen3.6:27b")
REALTIME_MODEL = os.getenv("REALTIME_MODEL", "gpt-realtime-2.1")
REALTIME_VOICE = os.getenv("REALTIME_VOICE", "marin")

# --- API access control (production) ---
# Comma-separated developer keys. Empty (the default) leaves the API open —
# fine for local dev; ALWAYS set in production. Clients send X-API-Key.
API_KEYS = {k.strip() for k in os.getenv("API_KEYS", "").split(",") if k.strip()}
# Comma-separated allowed browser origins. "*" (default) is dev-only.
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]

# --- crawler ---
BASE = "https://riphah.edu.pk"
SITEMAP = f"{BASE}/sitemap.xml"
NEWS_SITEMAP = f"{BASE}/news/sitemap_index.xml"

# Endpoints behind the JS dropdowns. Discovered in /admissions/fee-structure.js
# and /admissions/dates.js — plain GETs, no headless browser needed.
FEE_ENDPOINT = f"{BASE}/programs/program-finder-fee-structure.php"   # ?f=&au=&pc=&cl=
DATES_ENDPOINT = f"{BASE}/programs/program-finder-admissions.php"    # ?df=&pt=&p=
FINDER_ENDPOINT = f"{BASE}/programs/program-finder.php"             # ?tcs=&m=&pc=&cl=&pt=&f=&au=
PROGRAM_DETAIL = f"{BASE}/programs/detail/"                         # ?p=<id>

CRAWL_DELAY = float(os.getenv("CRAWL_DELAY_SECONDS", "0.5"))
USER_AGENT = os.getenv("CRAWL_USER_AGENT", "RiphahVoiceAgent/1.0")

# robots.txt disallows these prefixes. Honour them.
ROBOTS_DISALLOW = ("/cp/", "/test/", "/000/", "/404/")

# --- Riphah taxonomy ---
FACULTIES = {
    0: "Cross Faculty Academic Units",
    1: "Faculty of Health & Medical Sciences (FHMS)",
    2: "Faculty of Engineering & Applied Science (FEAS)",
    3: "Faculty of Computing (FC)",
    4: "Faculty of Social Sciences & Humanities (FSSH)",
    5: "Faculty of Pharmaceutical Sciences (FPS)",
    6: "Faculty of Management Sciences (FMS)",
    7: "Faculty of Rehabilitation & Allied Health Sciences (FRAHS)",
    8: "Faculty of Veterinary Sciences (FVS)",
}

CITIES = {1: "Islamabad", 2: "Rawalpindi", 3: "Lahore", 4: "Malakand"}

# The seven physical campuses. `cl` values from the #cid select.
CAMPUSES = {
    1: ("I-14 Campus", "Islamabad"),
    3: ("G-7 City Campus", "Islamabad"),
    4: ("Gulberg Green Campus", "Islamabad"),
    2: ("Al-Mizan Campus", "Rawalpindi"),
    5: ("Raiwind Campus", "Lahore"),
    6: ("Gulberg Campus", "Lahore"),
    7: ("Malakand Campus", "Malakand"),
    -1: ("Off Campus Location", "Various"),
}

# Four further campuses appear on /contact/ but run their own websites, so they
# are NOT in the fee-structure dropdown and NOT in riphah.edu.pk's sitemap. The
# agent must not imply the fee data it has covers these.
SATELLITE_CAMPUSES = {
    "Faisalabad Campus": ("Satiana Road, Faisalabad", "https://www.riphahfsd.edu.pk/"),
    "Sahiwal Campus": ("Multan Bypass Road, Sahiwal", "https://riphahsahiwal.edu.pk/"),
    "Peshawar Campus": ("Warsik Road, Peshawar", "https://riphahpsh.edu.pk/"),
    "Gujranwala Campus": ("Gondalwala Road, near GMC, Gujranwala",
                          "https://riphahgrw.edu.pk/"),
}

PROGRAM_TYPES = {
    "U": "Undergraduate Degree",
    "M": "Graduate Degree",
    "D": "Doctoral Degree",
    "CD": "Certificate / Diploma",
    "AD": "Associate Degree",
}

# Faculty landing-page slugs — used to tag prose chunks with a faculty.
FACULTY_SLUGS = {
    "fhms": "Faculty of Health & Medical Sciences (FHMS)",
    "feas": "Faculty of Engineering & Applied Science (FEAS)",
    "fc": "Faculty of Computing (FC)",
    "fssh": "Faculty of Social Sciences & Humanities (FSSH)",
    "fps": "Faculty of Pharmaceutical Sciences (FPS)",
    "fms": "Faculty of Management Sciences (FMS)",
    "frahs": "Faculty of Rehabilitation & Allied Health Sciences (FRAHS)",
    "fvs": "Faculty of Veterinary Sciences (FVS)",
}

# --- chunking ---
CHUNK_TARGET_CHARS = 1400
CHUNK_OVERLAP_CHARS = 200

# --- retrieval ---
DEFAULT_TOP_K = 6
MIN_SIMILARITY = 0.20   # below this, treat as "not in the knowledge base"

# --- languages the voice agent advertises support for ---
SUPPORTED_LANGUAGES = [
    ("en", "English"),
    ("ur", "Urdu / اردو"),
    ("ps", "Pashto / پښتو"),
    ("pa", "Punjabi / پنجابی"),
    ("ar", "Arabic / العربية"),
]


def campus_label(cl: int) -> str:
    name, city = CAMPUSES.get(cl, ("Unknown Campus", "Unknown"))
    return f"{name} ({city})"


def ensure_dirs() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
