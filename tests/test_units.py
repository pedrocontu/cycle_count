from datetime import date, datetime

import openpyxl
import pytest
from flask import Flask

from cycle_count import bom, email_utils, filters, usage
from cycle_count.db import _plant_matches

pytestmark = pytest.mark.no_db


def test_plant_matches():
    assert _plant_matches(None, 'P110')
    assert _plant_matches('P110', 'P110')
    assert _plant_matches('P110/P300', 'P300')
    assert not _plant_matches('P110/P300', 'P30')
    assert not _plant_matches('P200', 'P110')


def test_parse_path_and_merge():
    steps = bom._parse_path('SUB,TOP', '2,3')
    assert [s['cum_to_here'] for s in steps] == [2.0, 6.0]
    assert bom._parse_path('A,B', '2,x')[1]['qty_per'] == 0.0
    assert bom._parse_path('', '') == []
    paths = [{'level': 1, 'steps': bom._parse_path('S1,MID,TOP', '1,2,1')},
             {'level': 1, 'steps': bom._parse_path('S1,MID,TOP', '1,3,1')}]
    merged = bom._merge_intermediates(paths)
    assert [m['part'] for m in merged] == ['S1', 'MID']
    mid = merged[1]
    assert mid['total_cum'] == 5.0 and len(mid['chains']) == 2
    assert mid['chains'][0]['path_str'] == 'TOP → MID → S1'


def test_explode_without_config_or_connection(monkeypatch):
    bom.configure(None)
    assert bom.explode_part('X')['error'] == 'CC_AZURE_CONN is not configured.'
    assert bom.explode_part('')['error'] is None
    bom.configure('DSN=nothing')

    def fail(*a, **k):
        raise RuntimeError('network')
    monkeypatch.setattr(bom.pyodbc, 'connect', fail)
    assert bom.explode_part('X')['error'] == 'Azure connect failed: RuntimeError: network'


def test_explode_query_failure_closes_connection(monkeypatch):
    closed = []

    class Cur:
        def execute(self, *a):
            raise RuntimeError('bad sql')

    class Con:
        def cursor(self):
            return Cur()

        def close(self):
            closed.append(True)
    bom.configure('DSN=x')
    monkeypatch.setattr(bom.pyodbc, 'connect', lambda *a, **k: Con())
    assert bom.explode_part('X')['error'] == 'Explosion query failed: RuntimeError: bad sql'
    assert closed == [True]


def test_explode_groups(monkeypatch):
    class Row:
        def __init__(self, level, path, qty_path, cum):
            self.LEVEL, self.PATH, self.QTY_PATH, self.CUMULATIVE_QTY = level, path, qty_path, cum

    class Cur:
        def execute(self, *a):
            pass

        def fetchall(self):
            return [Row(0, 'TOP1', '2', 2), Row(1, 'MID,TOP2', '2,4', 8), Row(1, 'MID,TOP2', '1,1', None)]

    class Con:
        def cursor(self):
            return Cur()

        def close(self):
            pass
    bom.configure('DSN=x')
    monkeypatch.setattr(bom.pyodbc, 'connect', lambda *a, **k: Con())
    out = bom.explode_part('PN')
    assert [g['top'] for g in out['groups']] == ['TOP1', 'TOP2']
    assert out['groups'][1]['merged'][0]['total_cum'] == 3.0
    assert out['groups'][1]['paths'][1]['cumulative'] == 0.0


def _write_ref(path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(['Model', 'Desc', 'PN', 'Route'])
    for r in rows:
        ws.append(r)
    wb.save(path)


def test_ref_codes_loading_and_cache(tmp_path, monkeypatch):
    usage._route_cache.update(mtime=None, path=None, data=None)
    usage.configure({}, str(tmp_path))
    assert usage.get_route_mapping() == ({}, None)
    f = tmp_path / 'REF CODES-CIMPLE_1.xlsx'
    _write_ref(f, [['m1', 'd', 'pn-1', 'R1'], ['M1', 'd', 'PN-2', 'R9'], [None, 'x', 'x', 'x'],
                   ['M2', 'd', None, '  '], [' ', 'd', 'PN-3', 'R3']])
    mapping, path = usage.get_route_mapping()
    assert mapping == {'M1': 'R1'} and path == str(f)
    p2m, _ = usage.get_part_to_models()
    assert p2m == {'PN-1': ['M1'], 'PN-2': ['M1']}
    # broken workbook → no routes, no crash
    f.write_bytes(b'not an xlsx')
    import os
    os.utime(f, (1, 1))
    assert usage.get_route_mapping() == ({}, None)


def test_ref_codes_short_rows(tmp_path):
    usage._route_cache.update(mtime=None, path=None, data=None)
    usage.configure({}, str(tmp_path))
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(['Model', 'Desc'])
    ws.append(['M1', 'only two columns'])
    wb.save(tmp_path / 'REF CODES-CIMPLE_2.xlsx')
    assert usage.get_route_mapping()[0] == {}


def test_machine_usage(monkeypatch, tmp_path):
    usage._route_cache.update(mtime=None, path=None, data=None)
    _write_ref(tmp_path / 'REF CODES-CIMPLE_1.xlsx', [['MA', 'd', 'X', 'R1'], ['MB', 'd', 'X', 'R1']])
    usage.configure({'host': 'h'}, str(tmp_path))
    assert usage.get_machine_usage('')['rows'] == []
    monkeypatch.setattr(usage, 'fetch_usage_rows', lambda pn: [
        {'line_name': 'L', 'machine_name': 'M', 'feed': 1, 'bank': 2, 'track': 3,
         'models': 'MA, MB,, MC', 'last_used': None}])
    out = usage.get_machine_usage('12-34-5')
    assert out['queried_pn'] == '12345' and out['route_file'] == 'REF CODES-CIMPLE_1.xlsx'
    assert out['rows'][0]['models'] == ['MA', 'MB', 'MC'] and out['rows'][0]['routes'] == ['R1']


def test_machine_usage_without_mysql_config():
    usage.configure({}, None)
    out = usage.get_machine_usage('PN')
    assert 'CC_MYSQL_HOST is not configured' in out['error']


def test_clean_addrs():
    assert email_utils._clean_addrs(['a@x.com, B@x.com', None, ['b@X.com', 'c@x.com'], '  ']) == \
        ['a@x.com', 'B@x.com', 'c@x.com']
    assert email_utils._clean_addrs('one@x.com') == ['one@x.com']


def test_send_email_rules(monkeypatch):
    sent = []

    class SMTP:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            sent.append('tls')

        def send_message(self, msg, to_addrs):
            sent.append((msg, to_addrs))
    monkeypatch.setattr(email_utils.smtplib, 'SMTP', SMTP)
    email_utils.configure('h', 25, True, 'from@x.com', 'x.com')
    with pytest.raises(ValueError):
        email_utils.send_email([], 's', 't')
    mid = email_utils.send_email(['a@x.com'], 'S', 'body', '<p>b</p>', cc=['A@x.com', 'c@x.com'],
                                 in_reply_to='<id@x>')
    msg, to = sent[-1]
    assert sent[0] == 'tls' and to == ['a@x.com', 'c@x.com'] and msg['Cc'] == 'c@x.com'
    assert msg['In-Reply-To'] == '<id@x>' and msg['Message-ID'] == mid
    # only CC recipients → no empty To header
    email_utils.send_email([], 'S', 'b', cc=['c@x.com'])
    assert sent[-1][0]['To'] is None
    email_utils.configure(None, 25, False, 'f', 'x.com')
    with pytest.raises(RuntimeError):
        email_utils.send_email(['a@x.com'], 's', 't')


def test_filters():
    assert filters.plain_num(None) == '' and filters.plain_num(3.0) == '3'
    assert filters.plain_num(2.50) == '2.5' and filters.plain_num('abc') == 'abc'
    assert filters.plain_num(0) == '0'
    assert filters.fixed(1.5, 5) == '1.50000' and filters.fixed(None) == ''
    assert filters.fixed(-0.004, 2) == '-0.00'
    assert filters.priority_label(1) == 'High' and filters.priority_label('x') == 'x'
    assert filters.split_routes('R1||R2| ') == ['R1', 'R2'] and filters.split_routes(None) == []
    assert filters.dt(None) == '—' and filters.dt(datetime(2026, 1, 2, 3, 4)) == '2026-01-02 03:04'
    app = Flask(__name__)
    app.config['CC_DISPLAY_TZ'] = 'Europe/Lisbon'
    with app.app_context():
        assert filters.localdt(datetime(2026, 1, 15, 12, 0)) == '2026-01-15 12:00'
        assert filters.localdt(datetime(2026, 7, 15, 12, 0)) == '2026-07-15 13:00'
        assert filters.localdt('2026-07-15T12:00:00') == '2026-07-15 13:00'
        assert filters.localdt(date(2026, 1, 1)) == '2026-01-01'
        assert filters.localdt(None) == '—' and filters.localdt('garbage') == 'garbage'


def test_email_money_format():
    from cycle_count.emails import _money
    assert _money(124.616) == '124.62' and _money(-220.604) == '-220.60'
    assert _money(-0.004) == '0.00' and _money(None) == '' and _money(0) == '0.00'
