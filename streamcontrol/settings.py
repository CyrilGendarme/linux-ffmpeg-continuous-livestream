from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-secret-key")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = [host.strip() for host in os.environ.get("CONTROL_ALLOWED_HOSTS", "*").split(",") if host.strip()]
INSTANCE_API_KEY = os.environ.get("INSTANCE_API_KEY", "test-secret-key")

# The stream config edited by the web UI, and the runner's working directory
# (relative paths in the config are resolved against it).
STREAM_WORKDIR = BASE_DIR
STREAM_CONFIG_PATH = Path(os.environ.get("STREAM_CONFIG_PATH", str(BASE_DIR / "config.json")))
# Folders whose videos are offered as background loops (os.pathsep-separated),
# besides the folder of the current background.
BACKGROUND_VIDEO_DIRS = [
    Path(folder).expanduser()
    for folder in os.environ.get(
        "BACKGROUND_VIDEO_DIRS", str(BASE_DIR / "submodules" / "obscustomsetup" / "assets")
    ).split(os.pathsep)
    if folder
]
# The OBS overlays' countdown target, kept in sync with countdown_hour/countdown_minute.
STARTING_SOON_SCHEDULE_PATH = Path(
    os.environ.get(
        "STARTING_SOON_SCHEDULE_PATH",
        str(BASE_DIR / "submodules" / "obscustomsetup" / "scripts" / "startingSoonSchedule.js"),
    )
)

# Served over HTTPS only.
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "no-referrer"
X_FRAME_OPTIONS = "DENY"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_extensions",
    "control_api",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "streamcontrol.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
