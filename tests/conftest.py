"""Test fixtures.

The tests run against a real SQL Server (the cc_* schema in schema.sql is
created in a throw-away database). Point CC_TEST_SQL_SERVER at a server you
may create databases on, e.g. a local container:

  docker run -d -e ACCEPT_EULA=Y -e 'MSSQL_SA_PASSWORD=Test_Passw0rd!' \
      -p 1433:1433 mcr.microsoft.com/mssql/server:2022-latest
  export CC_TEST_SQL_SERVER='DRIVER={ODBC Driver 18 for SQL Server};SERVER=127.0.0.1,1433;UID=sa;PWD=Test_Passw0rd!;TrustServerCertificate=yes'

The external sources (Azure BOM, MySQL feeder usage, SMTP) are replaced by
fakes; the REF CODES workbook is a real .xlsx written to a temp folder.
"""
import html
import os
import re
from pathlib import Path

import openpyxl
import pyodbc
import pytest
from werkzeug.security import generate_password_hash

from cycle_count import auth, bom, create_app, db, email_utils, usage, views

SERVER = os.environ.get('CC_TEST_SQL_SERVER')
TEST_DB = 'cc_pytest'
TABLES_IN_DELETE_ORDER = [
    'cc_count_entries', 'cc_count_requests', 'cc_users', 'cc_parts',
    'cc_warehouse_stock', 'cc_ewm_stock', 'cc_stock_types', 'cc_mars_pou',
    'cc_pou_storage', 'cc_storage_location_desc', 'cc_storage_type_desc',
]
PASSWORD = 'secret123'


def pytest_collection_modifyitems(config, items):
    if SERVER:
        return
    skip = pytest.mark.skip(reason='CC_TEST_SQL_SERVER not set')
    for item in items:
        if 'no_db' not in item.keywords:
            item.add_marker(skip)


def pytest_configure(config):
    config.addinivalue_line('markers', 'no_db: test does not need SQL Server')


@pytest.fixture(scope='session')
def conn_str():
    master = pyodbc.connect(SERVER, autocommit=True)
    master.execute(f"IF DB_ID('{TEST_DB}') IS NOT NULL "
                   f"BEGIN ALTER DATABASE {TEST_DB} SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
                   f"DROP DATABASE {TEST_DB}; END")
    master.execute(f'CREATE DATABASE {TEST_DB}')
    master.close()
    cs = SERVER.rstrip(';') + f';DATABASE={TEST_DB}'
    con = pyodbc.connect(cs, autocommit=True)
    con.execute((Path(__file__).parent / 'schema.sql').read_text())
    con.close()
    return cs


class Sql:
    """Autocommit connection for arranging / asserting data. Every statement's
    cursor is drained and closed so it never keeps locks open."""

    def __init__(self, conn_str):
        self.raw = pyodbc.connect(conn_str, autocommit=True)
        # A pooled ODBC connection keeps the autocommit mode of its previous
        # user (the app, autocommit off); set it explicitly.
        self.raw.autocommit = True

    def execute(self, query, *params):
        cur = self.raw.cursor()
        try:
            if params:
                cur.execute(query, params)
            else:
                cur.execute(query)
            while cur.nextset():
                pass
        finally:
            cur.close()

    def close(self):
        self.raw.close()


@pytest.fixture
def sql(conn_str):
    con = Sql(conn_str)
    for t in TABLES_IN_DELETE_ORDER:
        con.execute(f'DELETE FROM {t}')
    yield con
    con.close()


def q(sql_con, query, *params):
    cur = sql_con.raw.cursor()
    try:
        if params:
            cur.execute(query, params)
        else:
            cur.execute(query)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        cur.close()


def q1(sql_con, query, *params):
    rows = q(sql_con, query, *params)
    return rows[0] if rows else None


# ---------------------------------------------------------------- fakes

class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def send_message(self, msg, to_addrs=None):
        FakeSMTP.sent.append({'msg': msg, 'to_addrs': list(to_addrs or [])})


SAMPLE_EXPLOSION = {
    'error': None,
    'groups': [
        {'top': 'TOP-A', 'paths': [
            {'level': 1, 'cumulative': 2.0, 'steps': [
                {'part': 'SUB-1', 'qty_per': 2.0, 'cum_to_here': 2.0},
                {'part': 'TOP-A', 'qty_per': 1.0, 'cum_to_here': 2.0}]}]},
        {'top': 'TOP-B', 'paths': [
            {'level': 1, 'cumulative': 3.0, 'steps': [
                {'part': 'SUB-2', 'qty_per': 3.0, 'cum_to_here': 3.0},
                {'part': 'TOP-B', 'qty_per': 1.0, 'cum_to_here': 3.0}]}]},
    ],
}
for _g in SAMPLE_EXPLOSION['groups']:
    _g['merged'] = bom._merge_intermediates(_g['paths'])

SAMPLE_USAGE = [
    {'line_name': 'L1', 'machine_name': 'M1', 'feed': 1, 'bank': 'A', 'track': 3,
     'models': 'MOD-A, MOD-B', 'last_used': None},
    {'line_name': 'L2', 'machine_name': 'M2', 'feed': 2, 'bank': 'B', 'track': 4,
     'models': 'MOD-C', 'last_used': None},
]


class Externals:
    """Controls the fake Azure / MySQL sources for one test."""

    def __init__(self):
        self.explosion = SAMPLE_EXPLOSION
        self.explode_calls = 0
        self.usage_rows = SAMPLE_USAGE
        self.usage_error = None

    def explode_part(self, part_number):
        self.explode_calls += 1
        return self.explosion

    def fetch_usage_rows(self, pn):
        if self.usage_error:
            raise self.usage_error
        return [dict(r) for r in self.usage_rows]


@pytest.fixture
def ref_codes_dir(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(['Model', 'Description', 'PN', 'Route'])
    ws.append(['MOD-A', 'model a', 'TOP-A', 'R1'])
    ws.append(['MOD-B', 'model b', 'TOP-A', 'R2'])
    ws.append(['MOD-C', 'model c', 'TOP-B', None])
    ws.append([None, None, None, None])
    wb.save(tmp_path / 'REF CODES-CIMPLE_2026.xlsx')
    return str(tmp_path)


@pytest.fixture
def externals(monkeypatch):
    ext = Externals()
    monkeypatch.setattr(bom, 'explode_part', ext.explode_part)
    monkeypatch.setattr(usage, 'fetch_usage_rows', ext.fetch_usage_rows)
    FakeSMTP.sent = []
    monkeypatch.setattr(email_utils.smtplib, 'SMTP', FakeSMTP)

    class SyncThread:
        def __init__(self, target, daemon=None):
            self.target = target

        def start(self):
            self.target()
    monkeypatch.setattr(views, 'Thread', SyncThread)
    # Same hash format as production, fewer iterations to keep the suite fast.
    monkeypatch.setattr(auth, 'hash_password',
                        lambda pw: generate_password_hash(pw, method='pbkdf2:sha256:1000'))
    return ext


@pytest.fixture
def app(conn_str, sql, externals, ref_codes_dir):
    app = create_app({
        'TESTING': True,
        'SECRET_KEY': 'test-secret',
        'WTF_CSRF_ENABLED': False,
        'CC_SQL_CONN': conn_str,
        'CC_AZURE_CONN': 'unused',
        'CC_REF_CODES_DIR': ref_codes_dir,
        'CC_SMTP_HOST': 'smtp.test',
    })
    usage._route_cache.update(mtime=None, path=None, data=None)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


# ---------------------------------------------------------------- data

def make_user(username, role, full_name=None, email=None, active=True, password=PASSWORD):
    db.create_user(username, full_name or username.title(), email or f'{username}@example.com',
                   role, auth.hash_password(password))
    user = db.get_user_by_username(username)
    if not active:
        db.set_user_active(user['id'], False)
    return user


@pytest.fixture
def users(app):
    return {
        'admin': make_user('admin1', 'admin', 'Ana Admin'),
        'analyst': make_user('analyst1', 'analyst', 'Rui Analyst'),
        'analyst2': make_user('analyst2', 'analyst', 'Other Analyst'),
        'priority': make_user('prio1', 'priority', 'Pri User'),
        'counter': make_user('counter1', 'cycle_counter', 'Carla Counter'),
        'counter2': make_user('counter2', 'cycle_counter', 'Second Counter'),
    }


PART = 'PN-100'


@pytest.fixture
def stock(sql):
    """Warehouse/EWM/POU data for PART."""
    sql.execute(
        '''INSERT INTO cc_warehouse_stock VALUES
           (?, 'P110', 'PT01', 'EUR', '10', '0', '2', NULL, 20, 0, 4, NULL),
           (?, 'P110', 'PT01', 'EUR', '5',  NULL, NULL, NULL, 10, NULL, NULL, NULL),
           (?, 'P200', 'PT01', 'EUR', 'abc', '3', NULL, NULL, 0, 6, NULL, NULL),
           (?, 'P300', 'PT01', 'EUR', '0', '0', '0', '0', 0, 0, 0, 0)''',
        PART, PART, PART, PART)
    sql.execute(
        '''INSERT INTO cc_stock_types VALUES
           ('F1', NULL, 'Unrestricted'), ('Q1', 'P200', 'Quality Inspection'),
           ('B1', 'P110/P300', 'Blocked')''')
    sql.execute(
        '''INSERT INTO cc_ewm_stock VALUES
           (?, 'F1', 'ST1', 'BIN-01', 'HU-1', 4, 'B001'),
           (?, 'F1', 'ST1', 'BIN-01', 'HU-2', 6, 'B002'),
           (?, 'B1', 'ST2', 'BIN-09', 'HU-3', 2, NULL),
           (?, 'Q1', 'ST3', 'BIN-Q',  'HU-4', 3, NULL)''',
        PART, PART, PART, PART)
    sql.execute(
        '''INSERT INTO cc_mars_pou VALUES
           ('POU-1', 'S1', 1, 7, ?), ('POU-1', 'S2', 2, 8, ?), ('POU-9', 'S9', 1, 1, ?)''',
        PART, PART, PART)
    sql.execute("INSERT INTO cc_pou_storage VALUES ('POU-1', 'P105')")
    sql.execute(
        '''INSERT INTO cc_storage_location_desc VALUES
           ('P105', 'Production line'), ('P110', 'Warehouse'), ('P200', 'Quality'),
           ('P300', 'Blocked area'), ('P400', 'Overflow')''')
    sql.execute("INSERT INTO cc_storage_type_desc VALUES ('PT01', 'ST1', 'Rack'), ('PT01', 'ST2', 'Floor')")
    sql.execute(
        "INSERT INTO cc_parts (part_number, description, part_type, status, unit_cost, synced_at) "
        "VALUES (?, 'Capacitor 10uF', 'PUR', 'AC', 1.5, SYSUTCDATETIME())", PART)


def login(client, username, password=PASSWORD):
    return client.post('/login/', data={'username': username, 'password': password})


def new_request(client, part=PART, **overrides):
    data = {'part_number': part, 'count_date': '2026-11-02', 'reason': 'Variance',
            'priority': '2', 'cc_emails': ''}
    data.update(overrides)
    return client.post('/requests/new/', data=data)


def last_request_id(sql):
    row = q1(sql, 'SELECT MAX(id) AS id FROM cc_count_requests')
    return row['id']


def flashes(response):
    return [html.unescape(m) for m in
            re.findall(r'<li class="[^"]*">([^<]*)</li>', response.get_data(as_text=True))]
