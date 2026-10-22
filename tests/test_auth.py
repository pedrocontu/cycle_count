import base64
import hashlib
import sqlite3

import pytest

from cycle_count import auth, create_app, db

from .conftest import PASSWORD, flashes, login, make_user, q1


def django_hash(password, salt='abcdefgh12345678', iterations=600000):
    dk = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), iterations)
    return f'pbkdf2_sha256${iterations}${salt}${base64.b64encode(dk).decode()}'


def test_login_success_and_failures(client, users):
    r = login(client, 'analyst1', 'wrong')
    assert r.status_code == 200 and 'Invalid username or password.' in flashes(r)
    r = login(client, 'nobody', PASSWORD)
    assert 'Invalid username or password.' in flashes(r)
    r = login(client, 'analyst1')
    assert r.status_code == 302 and r.headers['Location'] == '/'
    assert client.get('/').status_code == 200
    # already logged in → /login/ redirects
    assert client.get('/login/').status_code == 302


def test_hardcoded_bootstrap_admin_is_gone(client, users):
    r = login(client, 'admin', 'nimda')
    assert 'Invalid username or password.' in flashes(r)
    assert client.get('/').status_code == 302


def test_inactive_user_cannot_login(client, users):
    make_user('gone', 'analyst', active=False)
    r = login(client, 'gone')
    assert 'Invalid username or password.' in flashes(r)


def test_old_placeholder_password_cannot_login(client, users, sql):
    sql.execute("UPDATE cc_users SET password_hash = 'managed-by-django' WHERE username = 'analyst1'")
    assert 'Invalid username or password.' in flashes(login(client, 'analyst1', 'managed-by-django'))


def test_logout_requires_post(client, users):
    login(client, 'analyst1')
    assert client.get('/logout/').status_code == 405
    assert client.get('/').status_code == 200
    r = client.post('/logout/')
    assert r.status_code == 302
    assert client.get('/').status_code == 302


def test_deactivated_user_session_is_kicked(app, users):
    victim, admin = app.test_client(), app.test_client()
    login(victim, 'analyst1')
    login(admin, 'admin1')
    assert victim.get('/').status_code == 200
    admin.post(f'/admin/users/{users["analyst"]["id"]}/toggle/')
    assert victim.get('/').status_code == 302


def test_role_change_takes_effect_immediately(app, users, sql):
    c = app.test_client()
    login(c, 'admin1')
    assert c.get('/admin/users/').status_code == 200
    sql.execute("UPDATE cc_users SET role = 'analyst' WHERE username = 'admin1'")
    assert c.get('/admin/users/').status_code == 403


def test_password_reset_invalidates_sessions(app, users):
    victim, admin = app.test_client(), app.test_client()
    login(victim, 'analyst1')
    login(admin, 'admin1')
    admin.post(f'/admin/users/{users["analyst"]["id"]}/reset/', data={'new_password': 'newpass1'})
    assert victim.get('/').status_code == 302
    assert login(victim, 'analyst1', 'newpass1').status_code == 302


def test_legacy_django_hash_login_is_upgraded(client, users, sql):
    sql.execute('UPDATE cc_users SET password_hash = ? WHERE username = ?',
                django_hash('django-pass'), 'analyst1')
    assert 'Invalid username or password.' in flashes(login(client, 'analyst1', 'nope'))
    assert login(client, 'analyst1', 'django-pass').status_code == 302
    stored = q1(sql, "SELECT password_hash FROM cc_users WHERE username = 'analyst1'")['password_hash']
    assert stored.startswith('pbkdf2:sha256:')
    assert auth.verify_password(stored, 'django-pass')
    assert client.get('/').status_code == 200   # session still valid after upgrade


def test_unauthenticated_users_are_redirected(client, users):
    for url in ['/', '/api/dashboard.json', '/requests/', '/requests/new/', '/counts/1/',
                '/counts/1/explosion/', '/lookup/', '/admin/users/', '/admin/users/new/']:
        r = client.get(url)
        assert r.status_code == 302 and r.headers['Location'].endswith('/login/'), url


def test_app_refuses_to_start_without_secret_key(monkeypatch):
    monkeypatch.delenv('CC_SECRET_KEY', raising=False)
    with pytest.raises(RuntimeError, match='CC_SECRET_KEY'):
        create_app({'SECRET_KEY': None})


def test_debug_is_off_by_default(app):
    assert not app.debug


def test_no_credentials_in_source():
    """Connection strings, passwords and server addresses come from the
    environment only."""
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parents[1]
    sources = [p for p in root.rglob('*')
               if p.suffix in ('.py', '.html', '.txt', '.sql') and p.is_file()
               and not {'.git', 'tests', 'venv', '.venv'} & set(p.parts)]
    assert sources
    for path in sources:
        text = path.read_text(errors='ignore')
        assert not re.search(r'PWD=(?!password;)[^;{}\s]+', text), path
        assert not re.search(r"password\s*=\s*'[^']+'", text), path
        # internal (private-range) server addresses
        assert not re.search(r'\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))(?:\.\d{1,3}){2,3}\b',
                             text), path
        assert 'nimda' not in text, path


def test_csrf_is_enforced(conn_str, sql, externals, ref_codes_dir):
    app = create_app({'TESTING': True, 'SECRET_KEY': 'x', 'CC_SQL_CONN': conn_str,
                      'CC_REF_CODES_DIR': ref_codes_dir})
    make_user('analyst1', 'analyst')
    c = app.test_client()
    r = c.post('/login/', data={'username': 'analyst1', 'password': PASSWORD})
    assert r.status_code == 400
    page = c.get('/login/').get_data(as_text=True)
    token = page.split('name="csrf_token" value="')[1].split('"')[0]
    r = c.post('/login/', data={'username': 'analyst1', 'password': PASSWORD, 'csrf_token': token})
    assert r.status_code == 302
    # token from the page works for later forms (and does not expire after 1h)
    assert app.config['WTF_CSRF_TIME_LIMIT'] is None
    r = c.post('/requests/new/', data={'part_number': 'X', 'count_date': '2026-01-01',
                                       'reason': 'r', 'priority': '1'})
    assert r.status_code == 400 and 'session expired' in r.get_data(as_text=True)


def test_cli_create_admin(app, monkeypatch):
    monkeypatch.setattr('getpass.getpass', lambda prompt='': 'adminpass')
    res = app.test_cli_runner().invoke(args=['create-admin', 'boss', '--email', 'b@x.com'])
    assert 'created' in res.output, res.output
    user = db.get_user_by_username('boss')
    assert user['role'] == 'admin' and auth.verify_password(user['password_hash'], 'adminpass')
    res = app.test_cli_runner().invoke(args=['create-admin', 'boss'])
    assert 'already exists' in res.output


def test_cli_create_admin_rejects_short_or_mismatched(app, monkeypatch):
    monkeypatch.setattr('getpass.getpass', lambda prompt='': 'abc')
    assert 'at least' in app.test_cli_runner().invoke(args=['create-admin', 'x']).output
    answers = iter(['goodpass1', 'different'])
    monkeypatch.setattr('getpass.getpass', lambda prompt='': next(answers))
    assert 'do not match' in app.test_cli_runner().invoke(args=['create-admin', 'x']).output
    assert db.get_user_by_username('x') is None


def test_cli_import_django_passwords(app, users, tmp_path, client):
    path = tmp_path / 'db.sqlite3'
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE auth_user (username TEXT, password TEXT)')
    con.executemany('INSERT INTO auth_user VALUES (?, ?)', [
        ('analyst1', django_hash('from-django')),
        ('unknown', django_hash('x')),
        ('counter1', '!unusable'),
    ])
    con.commit()
    con.close()
    res = app.test_cli_runner().invoke(args=['import-django-passwords', str(path)])
    assert 'Imported 1 password(s); skipped 2.' in res.output, res.output
    assert login(client, 'analyst1', 'from-django').status_code == 302
