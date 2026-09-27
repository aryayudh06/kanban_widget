"""
config.py
---------
Central configuration for the floating Kanban widget.

SECURITY NOTE:
The Supabase "anon" key is safe to ship in a desktop client -- it is a
public, low-privilege key. Real data protection comes from Postgres
Row Level Security (RLS) policies (see supabase_setup.sql), which
ensure the anon key can only ever touch rows owned by the currently
authenticated user. NEVER put a service_role key in this file or
anywhere in a distributed application.

Fill these in from your Supabase project's Settings -> API page,
or set them as environment variables (recommended for anything you
plan to share or commit to version control).
"""

import os

# --- Supabase project credentials -------------------------------------------
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://project.supabase.co")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")

# --- keyring service identifiers ---------------------------------------------
# These namespace the credentials this app stores in the Windows Credential
# Manager (via the `keyring` library) so they don't collide with other apps.
KEYRING_SERVICE_NAME = "FloatingKanbanWidget"
KEYRING_ACCESS_TOKEN_KEY = "supabase_access_token"
KEYRING_REFRESH_TOKEN_KEY = "supabase_refresh_token"
KEYRING_USER_EMAIL_KEY = "supabase_user_email"

# --- Local cache ---------------------------------------------------------
APP_DATA_DIR = os.path.join(os.path.expanduser("~"), ".floating_kanban")
LOCAL_DB_PATH = os.path.join(APP_DATA_DIR, "kanban_cache.db")

# --- App behavior ---------------------------------------------------------
APP_NAME = "Floating Kanban"
GLOBAL_HOTKEY = "<ctrl>+<alt>+t"  # pynput GlobalHotKeys format, show/hide toggle
STARTUP_REGISTRY_KEY_NAME = "FloatingKanbanWidget"
SYNC_RETRY_INTERVAL_SECONDS = 15  # how often to retry pending offline changes
REALTIME_RECONNECT_DELAY_SECONDS = 5

# --- Window appearance ---------------------------------------------------------
WINDOW_WIDTH = 900          # default size in "expanded" (fullscreen-editable) mode
WINDOW_HEIGHT = 600
MIN_EXPANDED_WIDTH = 560
MIN_EXPANDED_HEIGHT = 360

COMPACT_WIDTH = 260         # size used in "compact" (minimized floating) mode
COMPACT_HEIGHT = 170
MIN_COMPACT_WIDTH = 220
MIN_COMPACT_HEIGHT = 120

WINDOW_OPACITY_BG = "rgba(30, 30, 46, 0.92)"
ACCENT_COLOR = "#89b4fa"
DEFAULT_CARD_COLOR = "#313244"

# --- Deadlines & warnings ---------------------------------------------------
DEADLINE_DUE_SOON_HOURS = 24     # a card turns "due soon" (amber) inside this window
DEADLINE_CHECK_INTERVAL_MS = 60_000  # how often visible cards re-check their deadline color
COLOR_OVERDUE = "#f38ba8"
COLOR_DUE_SOON = "#f9e2af"
COLOR_DONE = "#586074"

os.makedirs(APP_DATA_DIR, exist_ok=True)
