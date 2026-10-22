"""
Django settings for cycle_count_project project.

These settings support running the cycle_count Django app with a hardcoded
bootstrap admin user (id='admin', password='nimda') for development/testing
when the production SQL Server databases are not available.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# ------------------------------------------------------------------ #
# SECURITY WARNING: keep the secret key used in production secret!     #
# ------------------------------------------------------------------ #
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', 'dev-insecure-key-change-in-production-abc123')

DEBUG = os.environ.get('DJANGO_DEBUG', 'True').lower() == 'true'

ALLOWED_HOSTS = ['*']  # Override in production with specific hosts

# ------------------------------------------------------------------ #
# Application definition                                              #
# ------------------------------------------------------------------ #
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # Local apps
    'cycle_count',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'cycle_count_project.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'cycle_count_project.wsgi.application'

# ------------------------------------------------------------------ #
# Database — SQLite for the Django-managed tables (sessions, auth)    #
# The cycle_count app uses raw SQL via pyodbc for SQL Server,         #
# which is configured through CC_SQL_CONN below.                      #
# ------------------------------------------------------------------ #
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

# ------------------------------------------------------------------ #
# SQL Server connection string (used by cycle_count.db module)       #
# Override with CC_SQL_CONN environment variable in production.      #
# ------------------------------------------------------------------ #
CC_SQL_CONN = os.environ.get(
    'CC_SQL_CONN',
    (
        r'DRIVER={ODBC Driver 17 for SQL Server};'
        r'SERVER=10.216.180.197\VWMT;DATABASE=visteon_test;'
        r'UID=vwmt;PWD=vwmt4palmela;'
        r'TrustServerCertificate=yes;Connection Timeout=10;'
    )
)

# ------------------------------------------------------------------ #
# Internationalization                                                 #
# ------------------------------------------------------------------ #
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# ------------------------------------------------------------------ #
# Static files (CSS, JS, Images)                                       #
# ------------------------------------------------------------------ #
STATIC_URL = '/static/'
STATICFILES_DIRS = [
    BASE_DIR / 'static',
]

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ------------------------------------------------------------------ #
# Bootstrap admin — hardcoded credentials for testing without DB       #
# username: admin  password: nimda                                     #
# ------------------------------------------------------------------ #
CC_BOOTSTRAP_ADMIN = {
    'id': 0,
    'username': 'admin',
    'full_name': 'System Administrator',
    'email': 'admin@visteon.com',
    'role': 'admin',
    'active': 1,
}