"""Configuration, read from environment variables (or a local .env file).

No credentials live in the code: every connection string / password must be
provided by the environment.  See .env.example for the full list.
"""
import os
from datetime import timedelta


def _bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


def _int(name, default):
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, '') else default


def from_env():
    return {
        'SECRET_KEY': os.environ.get('CC_SECRET_KEY'),

        # Databases / external sources
        'CC_SQL_CONN': os.environ.get('CC_SQL_CONN'),
        'CC_AZURE_CONN': os.environ.get('CC_AZURE_CONN'),
        'CC_MYSQL': {
            'host': os.environ.get('CC_MYSQL_HOST'),
            'port': _int('CC_MYSQL_PORT', 3306),
            'user': os.environ.get('CC_MYSQL_USER'),
            'password': os.environ.get('CC_MYSQL_PASSWORD'),
            'database': os.environ.get('CC_MYSQL_DATABASE', 'vdb'),
            'connect_timeout': 8,
            'read_timeout': 15,
        },
        'CC_REF_CODES_DIR': os.environ.get(
            'CC_REF_CODES_DIR', r'W:\sharedir\MP&L\Inventory\Utils\REF CODES'),

        # Email
        'CC_SMTP_HOST': os.environ.get('CC_SMTP_HOST', 'vistsmtp.visteon.com'),
        'CC_SMTP_PORT': _int('CC_SMTP_PORT', 25),
        'CC_SMTP_TLS': _bool('CC_SMTP_TLS', False),
        'CC_MAIL_FROM': os.environ.get('CC_MAIL_FROM', 'noreply@visteon.com'),
        'CC_MAIL_DOMAIN': os.environ.get('CC_MAIL_DOMAIN', 'visteon.com'),

        # Display
        'CC_DISPLAY_TZ': os.environ.get('CC_DISPLAY_TZ', 'Europe/Lisbon'),

        # Security
        'SESSION_COOKIE_HTTPONLY': True,
        'SESSION_COOKIE_SAMESITE': 'Lax',
        'SESSION_COOKIE_SECURE': _bool('CC_SESSION_COOKIE_SECURE', False),
        'PERMANENT_SESSION_LIFETIME': timedelta(days=_int('CC_SESSION_DAYS', 14)),
        # Counters keep the count screen open for hours: the CSRF token must
        # stay valid for the whole session, not the Flask-WTF default of 1h.
        'WTF_CSRF_TIME_LIMIT': None,
    }
