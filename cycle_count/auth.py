"""Login, password hashing and access-control decorators.

Passwords are stored in cc_users.password_hash (werkzeug pbkdf2 format).
Hashes imported from the old Django app (`pbkdf2_sha256$...`) are accepted
too and upgraded to the werkzeug format on the next successful login.
"""
import base64
import hashlib
import hmac
from functools import wraps

from flask import abort, g, redirect, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from . import db

SESSION_UID = 'uid'
SESSION_PWV = 'pwv'
MIN_PASSWORD_LENGTH = 6


def hash_password(password):
    # pbkdf2 keeps the hash ~100 chars (scrypt hashes are ~160).
    return generate_password_hash(password, method='pbkdf2:sha256')


def _check_django_pbkdf2(stored, password):
    try:
        algorithm, iterations, salt, expected = stored.split('$', 3)
        iterations = int(iterations)
    except ValueError:
        return False
    if algorithm != 'pbkdf2_sha256':
        return False
    dk = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), iterations)
    return hmac.compare_digest(base64.b64encode(dk).decode().strip(), expected)


def is_legacy_hash(stored):
    return bool(stored) and stored.startswith('pbkdf2_sha256$')


def verify_password(stored, password):
    if not stored or not password:
        return False
    if is_legacy_hash(stored):
        return _check_django_pbkdf2(stored, password)
    try:
        return check_password_hash(stored, password)
    except (ValueError, TypeError):
        # e.g. the old 'managed-by-django' placeholder
        return False


def _password_version(user):
    """Changes whenever the password hash changes → resets invalidate sessions."""
    return hashlib.sha256((user.get('password_hash') or '').encode()).hexdigest()[:16]


def authenticate(username, password):
    user = db.get_user_by_username(username)
    if not user or not user['active'] or not verify_password(user['password_hash'], password):
        return None
    if is_legacy_hash(user['password_hash']):
        new_hash = hash_password(password)
        db.set_user_password(user['id'], new_hash)
        user['password_hash'] = new_hash
    return user


def login_user(user):
    session.clear()
    session.permanent = True
    session[SESSION_UID] = user['id']
    session[SESSION_PWV] = _password_version(user)


def logout_user():
    session.clear()


def load_current_user():
    """Re-read the user on every request so deactivation, role changes and
    password resets take effect immediately."""
    g.user = None
    uid = session.get(SESSION_UID)
    if uid is None:
        return
    user = db.get_user_by_id(uid)
    if (not user or not user['active']
            or session.get(SESSION_PWV) != _password_version(user)):
        session.clear()
        return
    user.pop('password_hash', None)
    g.user = user


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.get('user') is None:
            return redirect(url_for('cc.login'))
        return view(*args, **kwargs)
    return wrapper


def role_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if g.get('user') is None:
                return redirect(url_for('cc.login'))
            if g.user['role'] not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapper
    return decorator
