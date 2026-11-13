"""Django settings. Every value comes from LAMNEZIA_* environment variables (see .env.example)."""

from datetime import timedelta
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env()
env.prefix = "LAMNEZIA_"
env.read_env(BASE_DIR / ".env")

SECRET_KEY = env.str("SECRET_KEY")
DEBUG = env.bool("DEBUG", default=False)
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=[])

# The panel runs behind nginx or Caddy, which terminates TLS and sets X-Forwarded-Proto and
# X-Forwarded-For. They are believed only from TRUSTED_PROXIES: by default this host and the
# docker networks, where nginx on the host and the bundled Caddy come from.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
TRUSTED_PROXIES = env.list(
    "TRUSTED_PROXIES",
    default=["127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"],
)
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
# Pages put the CSRF token into forms and HTMX headers, so scripts never need the cookie.
CSRF_COOKIE_HTTPONLY = True
# No SECURE_SSL_REDIRECT: nginx redirects to HTTPS, and healthchecks come over plain HTTP.
SECURE_HSTS_SECONDS = env.int("HSTS_SECONDS", default=0 if DEBUG else 365 * 24 * 60 * 60)
CSRF_FAILURE_VIEW = "apps.core.errors.csrf_failure"
# No inline scripts and no eval: Alpine is the CSP build, handlers live in static/js/ui.js.
# HTMX, Alpine x-show and the Django admin set inline styles.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; object-src 'none'; base-uri 'self'; form-action 'self'; "
    "frame-ancestors 'none'"
)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.forms",
    "django_otp",
    "django_otp.plugins.otp_totp",
    "django_otp.plugins.otp_static",
    "axes",
    "apps.core",
    "apps.accounts",
    "apps.servers",
    "apps.ssh",
    "apps.operations",
    "apps.protocols",
    "apps.keys",
    "apps.plans",
    "apps.traffic",
    "apps.portal",
    "apps.dashboard",
    "apps.guides",
    "apps.notifications",
    "apps.support",
    "apps.bots",
    "apps.audit",
    "apps.backups",
    "apps.billing",
    # Django apps that extend the panel, such as payment providers (docs/wiki Billing-plugins).
    *env.list("PLUGINS", default=[]),
]

MIDDLEWARE = [
    # First: everything after it, the HTTPS check and the client address, reads its result.
    "config.security.TrustedProxyMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "config.security.ContentSecurityPolicyMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "apps.accounts.middleware.HtmxSignInMiddleware",
    "apps.accounts.middleware.TwoFactorMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Last, as django-axes requires: it turns a refused login into the lockout page.
    "axes.middleware.AxesMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.site",
            ],
        },
    },
]

DATABASES = {"default": env.db("DATABASE_URL", default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}")}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Redis carries the Channels layer, the Celery broker and the cache. Without it (local runs
# without docker) WebSocket groups and the cache stay in-process and Celery has no broker.
REDIS_URL = env.str("REDIS_URL", default="")
if REDIS_URL:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {"hosts": [REDIS_URL]},
        }
    }
else:
    CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
CELERY_BROKER_URL = REDIS_URL or "memory://"
CACHES = {
    "default": (
        {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": REDIS_URL}
        if REDIS_URL
        else {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
    )
}

AUTH_USER_MODEL = "accounts.User"
# django-axes goes first: it refuses a locked-out login before the password is checked.
AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "accounts:login"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Administrators must set up an authenticator app before they can use the panel.
REQUIRE_ADMIN_2FA = env.bool("REQUIRE_ADMIN_2FA", default=True)
OTP_TOTP_ISSUER = "Lamnezia"

# Login lockout (django-axes): after LOGIN_FAILURE_LIMIT wrong passwords for one account from
# one address, that pair waits LOGIN_LOCKOUT_MINUTES. A successful login resets the counter.
AXES_FAILURE_LIMIT = env.int("LOGIN_FAILURE_LIMIT", default=5)
AXES_COOLOFF_TIME = timedelta(minutes=env.int("LOGIN_LOCKOUT_MINUTES", default=15))
AXES_LOCKOUT_PARAMETERS = [["username", "ip_address"]]
AXES_RESET_ON_SUCCESS = True
AXES_LOCKOUT_CALLABLE = "apps.accounts.views.lockout"
# The client address is the last X-Forwarded-For entry from a trusted proxy (TRUSTED_PROXIES).
AXES_CLIENT_IP_CALLABLE = "apps.audit.services.journal.client_ip"

# Open registration: at most REGISTRATION_RATE_LIMIT new accounts an hour from one address
# (0: no limit). A sign-in link from a letter works for MAGIC_LINK_MINUTES.
REGISTRATION_RATE_LIMIT = env.int("REGISTRATION_RATE_LIMIT", default=5)
MAGIC_LINK_MINUTES = env.int("MAGIC_LINK_MINUTES", default=15)
# A code that links Telegram or Discord works once, for LINK_CODE_MINUTES (DESIGN.md 6.8);
# a Telegram Login Widget answer is accepted for MESSENGER_LOGIN_MAX_AGE after Telegram signed it.
LINK_CODE_MINUTES = 10
MESSENGER_LOGIN_MAX_AGE = timedelta(minutes=10)
# Subscription links: at most SUBSCRIPTION_RATE_LIMIT fetches a minute from one address
# (0: no limit); more get 429.
SUBSCRIPTION_RATE_LIMIT = env.int("SUBSCRIPTION_RATE_LIMIT", default=30)
# Support: at most SUPPORT_RATE_LIMIT new requests an hour from one user (0: no limit).
SUPPORT_RATE_LIMIT = env.int("SUPPORT_RATE_LIMIT", default=5)
# ... and at most SUPPORT_MESSAGE_RATE_LIMIT messages a minute in chats and requests (0: no limit).
SUPPORT_MESSAGE_RATE_LIMIT = env.int("SUPPORT_MESSAGE_RATE_LIMIT", default=10)
# Guides: at most SUGGESTION_RATE_LIMIT article suggestions a day from one user (0: no limit).
SUGGESTION_RATE_LIMIT = env.int("SUGGESTION_RATE_LIMIT", default=3)
# "Server does not work" reports this many minutes apart group into one incident.
SUPPORT_INCIDENT_WINDOW_MINUTES = env.int("SUPPORT_INCIDENT_WINDOW_MINUTES", default=30)
# A request page asks for the result of its server check every SUPPORT_CHECK_POLL_SECONDS.
SUPPORT_CHECK_POLL_SECONDS = 3

# Address of the panel in links the bots send; by default https:// and LAMNEZIA_DOMAIN.
_DOMAIN = env.str("DOMAIN", default="")
SITE_URL = env.str("SITE_URL", default=f"https://{_DOMAIN}" if _DOMAIN else "http://localhost:8000")
# Bots: at most BOT_RATE_LIMIT commands a minute from one messenger account (0: no limit).
BOT_RATE_LIMIT = env.int("BOT_RATE_LIMIT", default=20)
# A step of a bot dialog (a comment to a report) waits this many minutes for the answer.
BOT_DIALOG_MINUTES = 10
# Telegram: updates come by a webhook at SITE_URL/bots/telegram/ instead of long polling; Caddy or
# nginx passes it to the bots service on BOTS_WEBHOOK_PORT. The panel needs a public HTTPS domain.
TELEGRAM_WEBHOOK = env.bool("TELEGRAM_WEBHOOK", default=False)
BOTS_WEBHOOK_PORT = 8081
# The bots service checks the panel for "turn on/off/restart" and reports the bots' state every
# BOT_SUPERVISOR_SECONDS; a report older than BOT_SUPERVISOR_STALE means the service does not run.
# A bot that failed to start tries again after BOT_RETRY_SECONDS.
BOT_SUPERVISOR_SECONDS = 5
BOT_SUPERVISOR_STALE = timedelta(seconds=3 * BOT_SUPERVISOR_SECONDS)
BOT_RETRY_SECONDS = 60

# Outgoing mail (password reset): the SMTP server from the panel settings, or LAMNEZIA_EMAIL_URL
# while none is set there. The default prints letters to the web container log.
_email = env.email_url("EMAIL_URL", default="consolemail://")
EMAIL_FALLBACK_BACKEND = _email.pop("EMAIL_BACKEND")
vars().update(_email)
EMAIL_BACKEND = "apps.core.mail.SiteEmailBackend"
EMAIL_TIMEOUT = 15
DEFAULT_FROM_EMAIL = env.str("DEFAULT_FROM_EMAIL", default="Lamnezia <noreply@localhost>")

# The interface language comes from the switcher cookie, then from the browser's Accept-Language.
LANGUAGE_CODE = "ru"
LANGUAGES = [("ru", "Русский"), ("en", "English")]
LOCALE_PATHS = [BASE_DIR / "locale"]
TIME_ZONE = env.str("TIME_ZONE", default="UTC")
USE_I18N = True
FORMAT_MODULE_PATH = ["config.formats"]
USE_TZ = True

# Fernet key for secrets in the database (SSH credentials, private keys). Losing it loses them.
ENCRYPTION_KEY = env.str("ENCRYPTION_KEY")

# SSH timeouts in seconds: connect and log in, one command, waiting for the server lock.
SSH_CONNECT_TIMEOUT = env.float("SSH_CONNECT_TIMEOUT", default=15)
SSH_COMMAND_TIMEOUT = env.float("SSH_COMMAND_TIMEOUT", default=120)
SSH_LOCK_TIMEOUT = env.float("SSH_LOCK_TIMEOUT", default=300)

# Background jobs. Results and progress live in the Operation journal, not in Celery results.
CELERY_TIMEZONE = TIME_ZONE
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_TASK_IGNORE_RESULT = True
# SSH jobs are long: a worker process takes the next job only when it is free.
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
# Server polling has its own queue and worker, so long operations do not hold it up.
CELERY_TASK_ROUTES = ("apps.operations.queues.route",)
# A page that shows a running operation reloads its progress every PROGRESS_POLL_SECONDS.
PROGRESS_POLL_SECONDS = 2
# Seconds between traffic readings from the servers (DESIGN.md 5.5).
TRAFFIC_INTERVAL = env.int("TRAFFIC_INTERVAL", default=300)
# A peer is online if its latest handshake is newer than this many seconds.
TRAFFIC_ONLINE_WINDOW = 180
# Bearer token for the Prometheus endpoint /metrics; empty keeps the endpoint off.
METRICS_TOKEN = env.str("METRICS_TOKEN", default="")
# Seconds between comparisons of the panel keys with the server files (drift).
RECONCILE_INTERVAL = env.int("RECONCILE_INTERVAL", default=900)
# Seconds between copies of the protocol files of every server into the panel, for a restore.
PROTOCOL_BACKUP_INTERVAL = env.int("PROTOCOL_BACKUP_INTERVAL", default=3600)
# Seconds between server health readings; a server is offline after two intervals without an answer.
MONITOR_INTERVAL = env.int("MONITOR_INTERVAL", default=60)
# Days of server readings; readings older than MONITOR_DETAIL_DAYS keep one per quarter of an hour.
MONITOR_HISTORY_DAYS = env.int("MONITOR_HISTORY_DAYS", default=7)
MONITOR_DETAIL_DAYS = env.int("MONITOR_DETAIL_DAYS", default=1)
# Retention: finished operations and sent or failed notification deliveries are deleted after
# this many days; hourly traffic older than TRAFFIC_HOURLY_DAYS is folded into one row a day.
OPERATION_RETENTION_DAYS = env.int("OPERATION_RETENTION_DAYS", default=90)
NOTIFICATION_RETENTION_DAYS = env.int("NOTIFICATION_RETENTION_DAYS", default=90)
TRAFFIC_HOURLY_DAYS = env.int("TRAFFIC_HOURLY_DAYS", default=35)
CELERY_BEAT_SCHEDULE = {
    "operations-fail-stale": {"task": "operations.fail_stale", "schedule": 10 * 60},
    "traffic-collect": {"task": "traffic.collect", "schedule": TRAFFIC_INTERVAL},
    # Limits are applied as often as traffic is read: newer numbers would not come sooner.
    "traffic-enforce-limits": {"task": "traffic.enforce_limits", "schedule": TRAFFIC_INTERVAL},
    "traffic-apply-speed-limits": {
        "task": "traffic.apply_speed_limits",
        "schedule": TRAFFIC_INTERVAL,
    },
    "keys-reconcile": {"task": "keys.reconcile", "schedule": RECONCILE_INTERVAL},
    "backups-save-protocols": {
        "task": "backups.save_protocols",
        "schedule": PROTOCOL_BACKUP_INTERVAL,
    },
    # A reading that waited in the queue longer than an interval is stale: the next one replaces it.
    "servers-monitor": {
        "task": "servers.monitor",
        "schedule": MONITOR_INTERVAL,
        "options": {"expires": MONITOR_INTERVAL},
    },
    "notifications-warn-expiring": {"task": "notifications.warn_expiring", "schedule": 60 * 60},
    "notifications-recover-stuck": {"task": "notifications.recover_stuck", "schedule": 10 * 60},
    "servers-thin-history": {"task": "servers.thin_history", "schedule": 60 * 60},
    "operations-prune": {"task": "operations.prune", "schedule": 24 * 60 * 60},
    "notifications-prune": {"task": "notifications.prune", "schedule": 24 * 60 * 60},
    "traffic-roll-up": {"task": "traffic.roll_up", "schedule": 24 * 60 * 60},
    "guides-refresh-release": {"task": "guides.refresh_release", "schedule": 6 * 60 * 60},
    "guides-check-materials": {"task": "guides.check_materials", "schedule": 7 * 24 * 60 * 60},
    "billing-expire-orders": {"task": "billing.expire_orders", "schedule": 60 * 60},
}
# An order not paid this long after checkout is marked expired; a payment that still arrives
# counts all the same.
ORDER_EXPIRES_AFTER = timedelta(days=1)
# Notification delivery: seconds to wait for a channel, attempts, delay before the second attempt
# (it doubles with every next one).
NOTIFICATION_TIMEOUT = 10
NOTIFICATION_MAX_ATTEMPTS = 5
NOTIFICATION_RETRY_DELAY = 30
# A delivery still "sending" this long after it was taken lost its worker: it is sent again.
NOTIFICATION_STUCK_AFTER = timedelta(minutes=10)
# Chat messages of one user within this time make one notification, sent when it ends.
NOTIFICATION_JOIN_WINDOW = timedelta(minutes=2)
# An answer of support the user neither read nor answered this long reminds them in their own
# channel.
SUPPORT_REMINDER_DELAY = timedelta(minutes=2)
# Users hear that their access ends this long before the end.
ACCESS_EXPIRY_NOTICE = timedelta(days=3)
# A server check warns when less disk space than this is free (bytes).
SERVER_MIN_FREE_DISK = 1024**3
# Seconds after which a waiting or running operation counts as interrupted and can be retried.
OPERATION_TIMEOUT = env.int("OPERATION_TIMEOUT", default=3600)

# Amnezia app downloads (guides). The token is optional and only raises the GitHub API limit.
GITHUB_TOKEN = env.str("GITHUB_TOKEN", default="")
GITHUB_TIMEOUT = 10
# Beat asks GitHub every 6 hours (guides-refresh-release); pages read the cache or the database.
RELEASES_CACHE_TIMEOUT = 6 * 60 * 60
# Seconds to wait for a useful material link in the weekly check (guides-check-materials).
MATERIALS_CHECK_TIMEOUT = 15
AMNEZIA_APP_STORE_URL = env.str(
    "AMNEZIA_APP_STORE_URL", default="https://apps.apple.com/app/id1600529900"
)
AMNEZIA_GOOGLE_PLAY_URL = env.str(
    "AMNEZIA_GOOGLE_PLAY_URL",
    default="https://play.google.com/store/apps/details?id=org.amnezia.vpn",
)

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

FORM_RENDERER = "config.forms.FormRenderer"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"
