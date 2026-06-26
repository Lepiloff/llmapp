"""Production settings — strict HTTPS, narrow allowlists, no debug."""
from __future__ import annotations

from decouple import Csv, config
from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F401,F403
from .base import MIDDLEWARE  # noqa: F401  - re-imported so we can mutate it below

DEBUG = False
ALLOWED_HOSTS = config("ALLOWED_HOSTS", cast=Csv())

# SECRET_KEY hardening — base.py keeps an insecure default so dev / CI boot
# without env. Production MUST provide a real key; refuse to start otherwise.
SECRET_KEY = config("SECRET_KEY")
_INSECURE_SECRET_KEY = "insecure-dev-key-change-me"
if not SECRET_KEY or SECRET_KEY == _INSECURE_SECRET_KEY:
    raise ImproperlyConfigured(
        "SECRET_KEY is missing or set to the insecure development default. "
        "Set a real, high-entropy SECRET_KEY in the production environment."
    )

# CSRF — Django requires the scheme+host of any cross-origin form/XHR
# submission to be listed here. Default empty (only same-origin requests
# accepted). Set CSRF_TRUSTED_ORIGINS env var to a comma-separated list
# like "https://llmappmarket.com,https://www.llmappmarket.com".
CSRF_TRUSTED_ORIGINS = config("CSRF_TRUSTED_ORIGINS", default="", cast=Csv())

# HTTPS / cookies
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365  # 1 year
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Strict CORS in production — explicit list only.
CORS_ALLOWED_ORIGINS = config("CORS_ALLOWED_ORIGINS", cast=Csv())
CORS_ALLOW_ALL_ORIGINS = False

# Static files. WhiteNoise serves the collected static bundle directly from
# the gunicorn process with cache-busting hashes and per-asset Cache-Control
# headers; the optional nginx profile in docker-compose.yml still works on
# top (nginx terminates TLS and proxies, WhiteNoise handles the static MIME
# layer). The middleware MUST sit immediately after SecurityMiddleware per
# WhiteNoise docs.
#
# CSPMiddleware sits at the END of the chain so its response header isn't
# stripped by other middleware that mutate the response in-flight.
_security_idx = MIDDLEWARE.index("django.middleware.security.SecurityMiddleware")
MIDDLEWARE = (
    MIDDLEWARE[: _security_idx + 1]
    + ["whitenoise.middleware.WhiteNoiseMiddleware"]
    + MIDDLEWARE[_security_idx + 1 :]
    + ["apps.core.csp.CSPMiddleware"]
)

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}
