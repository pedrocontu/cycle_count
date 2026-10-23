import json
import re
from datetime import datetime

import pytest

from .conftest import (PART, FakeSMTP, flashes, last_request_id, login, make_user,
                       new_request, q, q1)


def html(r):
    return r.get_data(as_text=True)


@pytest.fixture
def as_(app, users):
    """as_('analyst1') → logged-in test client (cached per user)."""
    clients = {}

    def get(username):
        if username not in clients:
            c = app.test_client()
            assert login(c, username).status_code == 302
            clients[username] = c
        return clients[username]
    return get


@pytest.fixture
def claimed(as_, stock, sql):
    """A request created by analyst1 and claimed by counter1 → rid."""
    assert new_request(as_('analyst1')).status_code == 302
    rid = last_request_id(sql)
    assert as_('counter1').post(f'/requests/{rid}/claim/').status_code == 302
    return rid


def entries(sql, rid):
    return {e['storage_location']: e for e in q(
        sql, 'SELECT * FROM cc_count_entries WHERE request_id = ?', rid)}


def req_row(sql, rid):
    return q1(sql, 'SELECT * FROM cc_count_requests WHERE id = ?', rid)


# ------------------------------------------------------------------ create

def test_create_request_sends_email_and_computes_routes(as_, users, stock, sql, externals):
    r = new_request(as_('analyst1'), cc_emails='a@x.com; b@y.pt  a@x.com')
    assert r.status_code == 302
    rid = last_request_id(sql)
    row = req_row(sql, rid)
    assert row['status'] == 'pending' and row['requested_by'] == users['analyst']['id']
    assert row['cc_emails'] == 'a@x.com, b@y.pt, a@x.com'
    assert len(FakeSMTP.sent) == 1
    msg = FakeSMTP.sent[0]['msg']
    assert msg['To'] == 'counter1@example.com, counter2@example.com'
    assert 'admin1@example.com' in msg['Cc'] and 'analyst1@example.com' in msg['Cc']
    assert 'b@y.pt' in msg['Cc'] and msg['Cc'].count('a@x.com') == 1
    assert row['email_message_id'] == msg['Message-ID']
    part = q1(sql, 'SELECT routes, explosion_json FROM cc_parts WHERE part_number = ?', PART)
    assert part['routes'] == 'R1|R2' and json.loads(part['explosion_json'])['groups']


@pytest.mark.parametrize('overrides,message', [
    ({'part_number': ''}, 'Part number is required.'),
    ({'part_number': 'X' * 41}, 'Part number is too long.'),
    ({'count_date': ''}, 'Date is required.'),
    ({'count_date': '31/12/2026'}, 'Date must be a valid date (YYYY-MM-DD).'),
    ({'count_date': '2026-02-30'}, 'Date must be a valid date (YYYY-MM-DD).'),
    ({'reason': '   '}, 'Reason is required.'),
    ({'priority': '7'}, 'Priority must be 1, 2, or 3.'),
    ({'priority': 'abc'}, 'Priority must be 1, 2, or 3.'),
    ({'cc_emails': 'not-an-email'}, 'Invalid email address(es) in CC: not-an-email'),
])
def test_create_request_validation(as_, users, sql, overrides, message):
    r = new_request(as_('analyst1'), **overrides)
    assert r.status_code == 200 and message in flashes(r)
    assert q1(sql, 'SELECT COUNT(*) AS n FROM cc_count_requests')['n'] == 0
    # the form keeps what was typed
    if overrides.get('reason') is None and 'part_number' not in overrides:
        assert 'value="PN-100"' in html(r)


def test_create_request_email_failure_is_reported(as_, users, sql, monkeypatch):
    def boom(*a, **k):
        raise OSError('smtp down')
    monkeypatch.setattr('cycle_count.email_utils.smtplib.SMTP', boom)
    r = new_request(as_('analyst1'))
    assert last_request_id(sql) is not None
    r = as_('analyst1').get('/')
    assert 'but email failed: smtp down' in html(r)


def test_explosion_error_does_not_store_routes(as_, users, sql, externals):
    externals.explosion = {'groups': [], 'error': 'Azure connect failed'}
    new_request(as_('analyst1'))
    assert q1(sql, 'SELECT routes FROM cc_parts WHERE part_number = ?', PART) is None
    page = html(as_('analyst1').get('/'))
    assert 'Computing routes…' in page


def test_counter_cannot_create_request(as_, users):
    assert new_request(as_('counter1')).status_code == 403
    assert as_('counter1').get('/requests/new/').status_code == 403


# ------------------------------------------------------------------ claim

def test_claim_snapshots_stock(as_, claimed, sql):
    rid = claimed
    row = req_row(sql, rid)
    assert row['status'] == 'in_progress' and row['started_at'] is not None
    e = entries(sql, rid)
    # two P110 rows are summed: 10 + 0 + 2 and 5 → 17 ; value 20+4+10 = 34
    assert e['P110']['system_qty'] == 17 and e['P110']['unit_value'] == pytest.approx(2.0)
    # 'abc' (not a number) counts as 0 instead of aborting the claim
    assert e['P200']['system_qty'] == 3 and e['P200']['unit_value'] == pytest.approx(2.0)
    assert e['P300']['system_qty'] == 0 and e['P300']['unit_value'] is None
    # POU-1 is mapped to P105 → manual P105 entry with the part's unit cost
    assert e['P105']['is_manual'] and e['P105']['unit_value'] == pytest.approx(1.5)
    assert e['P105']['currency'] == 'EUR'


def test_claim_twice_or_by_other_counter_fails(as_, claimed):
    r = as_('counter2').post(f'/requests/{claimed}/claim/', follow_redirects=True)
    assert 'That request is no longer available to claim.' in flashes(r)


def test_claim_is_atomic(as_, users, stock, sql, monkeypatch):
    new_request(as_('analyst1'))
    rid = last_request_id(sql)

    def broken(con, rid_, part):
        con.execute("INSERT INTO cc_count_entries (request_id, storage_location) VALUES (?, 'X')",
                    (rid_,))
        raise RuntimeError('stock table unavailable')
    monkeypatch.setattr('cycle_count.db._populate_entries', broken)
    r = as_('counter1').post(f'/requests/{rid}/claim/')
    assert r.status_code == 500
    row = req_row(sql, rid)
    assert row['status'] == 'pending' and row['assigned_to'] is None
    assert entries(sql, rid) == {}


def test_only_counters_can_claim(as_, users, stock, sql):
    new_request(as_('analyst1'))
    rid = last_request_id(sql)
    for u in ('analyst1', 'admin1', 'prio1'):
        assert as_(u).post(f'/requests/{rid}/claim/').status_code == 403
    assert as_('counter1').post('/requests/99999/claim/').status_code == 404


# ------------------------------------------------------------------ count screen

def test_count_screen_renders(as_, claimed):
    page = html(as_('counter1').get(f'/counts/{claimed}/'))
    assert 'name="qty__P110"' in page and 'name="qty__P105"' in page
    assert 'name="qty__P300"' not in page          # zero stock, not counted → hidden
    assert 'Finish count' in page and 'disabled' not in page.split('id="qty__P110"')[1][:300]
    assert 'HU-1' in page and 'HU-3' in page and 'Rack' in page
    assert 'POU-9' in page                           # unmapped POU card
    assert 'L1' in page and 'MOD-A' in page          # machine usage
    assert 'Capacitor 10uF' in page
    # P400 is not in the count → offered in "add location"
    add_select = page.split('id="add_sl"')[1].split('</select>')[0]
    assert '<option value="P400">' in add_select and 'value="P110"' not in add_select


@pytest.mark.parametrize('user', ['admin1', 'analyst1', 'counter2'])
def test_count_screen_readonly_for_others(as_, claimed, user):
    page = html(as_(user).get(f'/counts/{claimed}/'))
    assert 'Finish count' not in page
    assert 'id="qty__P110"' in page and 'disabled' in page.split('id="qty__P110"')[1][:300]


def test_pending_or_missing_count_screen(as_, users, stock, sql):
    new_request(as_('analyst1'))
    rid = last_request_id(sql)
    r = as_('analyst1').get(f'/counts/{rid}/', follow_redirects=True)
    assert f'Count #{rid} has not been started yet.' in flashes(r)
    assert as_('analyst1').get('/counts/424242/').status_code == 404


def test_usage_lookup_failure_degrades(as_, claimed, externals):
    externals.usage_error = ConnectionError('mysql down')
    page = html(as_('counter1').get(f'/counts/{claimed}/'))
    assert 'MySQL lookup failed: ConnectionError: mysql down' in page


def test_missing_ref_codes_folder_degrades(app, as_, claimed):
    app.config['CC_REF_CODES_DIR'] = '/does/not/exist'
    from cycle_count import usage
    usage.configure(app.config['CC_MYSQL'], '/does/not/exist')
    assert as_('counter1').get(f'/counts/{claimed}/').status_code == 200
    assert as_('counter1').get(f'/counts/{claimed}/explosion/').status_code == 200


# ------------------------------------------------------------------ save

def save(client, rid, action='save', **fields):
    data = {'action': action}
    data.update(fields)
    return client.post(f'/counts/{rid}/save/', data=data)


def test_save_progress(as_, claimed, sql):
    meta = {'feeders': [], 'explosion': [], 'p105_pou': [], 'hus': [{'sl': 'P110', 'hu': 'HU-1'}]}
    r = save(as_('counter1'), claimed, qty__P110='16', qty__P105='3,5', qty__P200='',
             count_notes='  found extra  ', meta_json=json.dumps(meta))
    assert r.status_code == 302
    e = entries(sql, claimed)
    assert e['P110']['counted_qty'] == 16 and e['P105']['counted_qty'] == 3.5
    assert e['P200']['counted_qty'] is None
    row = req_row(sql, claimed)
    assert row['count_notes'] == 'found extra' and json.loads(row['progress_meta']) == meta
    # invalid meta does not overwrite the saved one
    save(as_('counter1'), claimed, qty__P110='16', qty__P105='3.5', meta_json='{broken')
    save(as_('counter1'), claimed, qty__P110='16', qty__P105='3.5', meta_json='[1, 2]')
    assert json.loads(req_row(sql, claimed)['progress_meta']) == meta
    page = html(as_('counter1').get(f'/counts/{claimed}/'))
    assert 'value="16"' in page and 'value="3.5"' in page


def test_save_rules(as_, claimed, sql):
    assert as_('counter2').post(f'/counts/{claimed}/save/', data={'qty__P110': '1'}).status_code == 403
    assert as_('admin1').post(f'/counts/{claimed}/save/', data={'qty__P110': '1'}).status_code == 403
    assert entries(sql, claimed)['P110']['counted_qty'] is None
    assert as_('counter1').post('/counts/31337/save/').status_code == 404


def test_add_and_delete_locations(as_, claimed, sql):
    c = as_('counter1')
    r = save(c, claimed, 'add_location', add_storage_location='p400', qty__P110='5')
    assert r.status_code == 302
    e = entries(sql, claimed)
    assert e['P400']['is_manual'] and e['P400']['plant'] == 'PT01'
    assert e['P400']['unit_value'] == pytest.approx(1.5) and e['P110']['counted_qty'] == 5
    r = save(c, claimed, 'add_location', add_storage_location='P400')
    assert 'Location P400 is already in this count.' in flashes(c.get(f'/counts/{claimed}/'))
    save(c, claimed, 'add_location', add_storage_location="P4'; DROP--")
    assert 'Invalid Storage Location code.' in flashes(c.get(f'/counts/{claimed}/'))
    save(c, claimed, 'add_location', add_storage_location='')
    assert 'Pick a Storage Location to add.' in flashes(c.get(f'/counts/{claimed}/'))
    save(c, claimed, 'delete_location', delete_storage_location='P110')
    assert 'Cannot remove P110: only manual locations can be deleted.' in flashes(c.get(f'/counts/{claimed}/'))
    save(c, claimed, 'delete_location', delete_storage_location='P400')
    assert 'P400' not in entries(sql, claimed)


def test_finish_count_sends_threaded_reply(as_, claimed, sql):
    FakeSMTP.sent.clear()
    created_mid = req_row(sql, claimed)['email_message_id']
    meta = {'feeders': [
        {'line_name': 'L1', 'machine_name': 'M1', 'feed': '1', 'bank': 'A', 'track': '3', 'count': '4'},
        {'manual': True, 'label': '<b>shelf</b>', 'count': '2'}],
        'explosion': [{'top': 'TOP-A', 'part': 'SUB-1', 'qty_per': '2', 'cum': '2',
                       'count': '1', 'subtotal': '2'}],
        'p105_pou': [{'pou_id': 'POU-1', 'slot_id': 'S1', 'fifo': '1', 'fifo_qty': '7',
                      'confirmed': True}],
        'hus': []}
    r = save(as_('counter1'), claimed, 'finish', qty__P110='17', qty__P105='15', qty__P200='1',
             count_notes='ok', meta_json=json.dumps(meta))
    assert r.status_code == 302
    row = req_row(sql, claimed)
    assert row['status'] == 'done' and row['completed_at'] is not None
    msg = FakeSMTP.sent[-1]['msg']
    assert msg['In-Reply-To'] == created_mid and msg['Subject'].startswith('Re: [Cycle Count')
    assert 'analyst1@example.com' in msg['To'] and 'admin1@example.com' in msg['Cc']
    body = msg.get_body(('html',)).get_content()
    assert '&lt;b&gt;shelf&lt;/b&gt;' in body and '<b>shelf' not in body
    assert 'POU-1' in body and 'TOP-A' in body
    text = msg.get_body(('plain',)).get_content()
    # P110 17 vs 17 ; P105 15 vs 0 → +15 × 1.5 ; P200 1 vs 3 → -2 × 2
    assert 'TOTAL' in text and '18.50' in text
    assert '22.50' in text and '-4.00' in text      # money always with 2 decimals
    # finished → no more edits
    assert save(as_('counter1'), claimed, qty__P110='1').status_code == 409
    assert entries(sql, claimed)['P110']['counted_qty'] == 17


def test_finish_without_meta_uses_saved_meta(as_, claimed, sql):
    save(as_('counter1'), claimed, meta_json=json.dumps(
        {'feeders': [{'manual': True, 'label': 'saved-line', 'count': '3'}]}))
    FakeSMTP.sent.clear()
    save(as_('counter1'), claimed, 'finish', qty__P105='3')
    assert 'saved-line' in FakeSMTP.sent[-1]['msg'].get_body(('html',)).get_content()


def test_finish_email_failure_still_finishes(as_, claimed, sql, monkeypatch):
    def boom(*a, **k):
        raise OSError('smtp down')
    monkeypatch.setattr('cycle_count.email_utils.smtplib.SMTP', boom)
    save(as_('counter1'), claimed, 'finish', qty__P110='1')
    assert req_row(sql, claimed)['status'] == 'done'
    assert 'but email failed: smtp down' in html(as_('counter1').get('/'))


# ------------------------------------------------------------------ review

@pytest.fixture
def done(as_, claimed, sql):
    save(as_('counter1'), claimed, 'finish', qty__P110='20', qty__P105='2',
         meta_json=json.dumps({'feeders': [{'manual': True, 'label': 'x', 'count': '2'}]}))
    assert req_row(sql, claimed)['status'] == 'done'
    return claimed


def test_kanban_totals(as_, done, sql):
    page = html(as_('admin1').get('/requests/'))
    # QOH 17+3+0 = 20 (P105 0) ; counted 22 ; delta +2 ; valor (3×2)+(2×1.5)+(-3×2) = 3
    # unit value = stock-weighted average of the entries' unit values = (17×2 + 3×2) / 20
    row = page.split(f'<tr class="clickable" data-href="/counts/{done}/"')[1].split('</tr>')[0]
    cells = [c.strip() for c in re.findall(r'<td class="num"[^>]*>(.*?)</td>', row, re.S)]
    assert cells[:5] == ['20', '22', '+2', '2.00000', '+3.00'], cells
    assert 'value="adjust"' in row and 'value="recount"' in row


def test_review_adjust_via_fetch(as_, done, sql, users):
    r = as_('admin1').post(f'/requests/{done}/review/', data={'outcome': 'adjust'},
                           headers={'X-Requested-With': 'fetch'})
    assert r.status_code == 204
    row = req_row(sql, done)
    assert row['status'] == 'reviewed' and row['review_outcome'] == 'adjust'
    assert row['reviewed_by'] == users['admin']['id']
    page = html(as_('admin1').get('/requests/?status=reviewed'))
    assert f'#{done}' in page and 'Adjust' in page
    # reviewing again is refused
    r = as_('admin1').post(f'/requests/{done}/review/', data={'outcome': 'adjust'},
                           headers={'X-Requested-With': 'fetch'})
    assert r.status_code == 409


def test_review_not_adjust_via_form(as_, done, sql):
    r = as_('admin1').post(f'/requests/{done}/review/', data={'outcome': 'not_adjust'},
                           follow_redirects=True)
    assert f'Request #{done} marked as Reviewed / Not Adjust.' in flashes(r)
    assert req_row(sql, done)['review_outcome'] == 'not_adjust'


def test_review_invalid_outcome(as_, done):
    r = as_('admin1').post(f'/requests/{done}/review/', data={'outcome': 'maybe'},
                           headers={'X-Requested-With': 'fetch'})
    assert r.status_code == 400
    r = as_('admin1').post(f'/requests/{done}/review/', data={}, follow_redirects=True)
    assert 'Pick a valid review outcome.' in flashes(r)


def test_only_admin_reviews(as_, done):
    for u in ('analyst1', 'counter1', 'prio1'):
        assert as_(u).post(f'/requests/{done}/review/', data={'outcome': 'adjust'}).status_code == 403


def test_recount_clears_everything(as_, done, sql):
    r = as_('admin1').post(f'/requests/{done}/review/', data={'outcome': 'recount'},
                           headers={'X-Requested-With': 'fetch'})
    assert r.status_code == 204
    row = req_row(sql, done)
    assert row['status'] == 'pending' and row['assigned_to'] is None
    assert row['progress_meta'] is None and row['count_notes'] is None
    assert entries(sql, done) == {}
    # a new counter gets a clean screen
    as_('counter2').post(f'/requests/{done}/claim/')
    page = html(as_('counter2').get(f'/counts/{done}/'))
    assert '"label": "x"' not in page and '"x"' not in page.split('initial-meta-data')[1][:200]
    assert entries(sql, done)['P110']['counted_qty'] is None


def test_review_only_done(as_, claimed):
    r = as_('admin1').post(f'/requests/{claimed}/review/', data={'outcome': 'adjust'},
                           follow_redirects=True)
    assert 'Only completed counts can be reviewed.' in flashes(r)


def test_reviewed_page_with_more_than_2100_requests(as_, users, sql):
    """The old IN (?, ?, ...) query broke past SQL Server's 2100-parameter limit."""
    uid = users['analyst']['id']
    sql.execute(f'''
        INSERT INTO cc_count_requests (part_number, count_date, reason, priority,
                                       requested_by, status, review_outcome)
        SELECT TOP 2300 'PN-BULK', '2026-01-01', 'bulk', 3, {uid}, 'reviewed', 'adjust'
        FROM sys.all_objects a CROSS JOIN sys.all_objects b''')
    sql.execute('''INSERT INTO cc_count_entries (request_id, storage_location, system_qty,
                                                 counted_qty, unit_value)
                   SELECT id, 'P110', 10, 12, 0.5 FROM cc_count_requests''')
    r = as_('admin1').get('/requests/?status=reviewed')
    assert r.status_code == 200
    page = html(r)
    assert page.count('data-part="PN-BULK"') == 2300 and '+1.00' in page


# ------------------------------------------------------------------ cancel / delete

def test_cancel_rules(as_, users, stock, sql):
    new_request(as_('analyst1'))
    rid = last_request_id(sql)
    assert as_('analyst2').post(f'/requests/{rid}/cancel/').status_code == 403
    assert as_('counter1').post(f'/requests/{rid}/cancel/').status_code == 403
    assert as_('analyst1').get(f'/requests/{rid}/cancel/').status_code == 405
    assert req_row(sql, rid)['status'] == 'pending'
    r = as_('analyst1').post(f'/requests/{rid}/cancel/', follow_redirects=True)
    assert f'Request #{rid} cancelled.' in flashes(r)
    r = as_('analyst1').post(f'/requests/{rid}/cancel/', follow_redirects=True)
    assert 'Only pending or in-progress requests can be cancelled.' in flashes(r)
    # admin can cancel anybody's in-progress count; the counter is then locked out
    new_request(as_('analyst2'))
    rid2 = last_request_id(sql)
    as_('counter1').post(f'/requests/{rid2}/claim/')
    as_('admin1').post(f'/requests/{rid2}/cancel/')
    assert req_row(sql, rid2)['status'] == 'cancelled'
    assert save(as_('counter1'), rid2, qty__P110='1').status_code == 409
    r = as_('counter1').get(f'/counts/{rid2}/', follow_redirects=True)
    assert f'Count #{rid2} is cancelled.' in flashes(r)


def test_cancel_button_shown_to_owner(as_, users, sql):
    new_request(as_('analyst1'))
    rid = last_request_id(sql)
    assert f'/requests/{rid}/cancel/' in html(as_('analyst1').get('/'))
    assert f'/requests/{rid}/cancel/' in html(as_('admin1').get('/'))
    assert f'/requests/{rid}/cancel/' not in html(as_('counter1').get('/'))


def test_delete_rules(as_, users, stock, sql):
    new_request(as_('analyst1'))
    keep = last_request_id(sql)
    for _ in range(3):
        new_request(as_('analyst1'))
    ids = [r['id'] for r in q(sql, 'SELECT id FROM cc_count_requests WHERE id <> ? ORDER BY id', keep)]
    as_('counter1').post(f'/requests/{ids[0]}/claim/')
    for rid in ids:
        as_('analyst1').post(f'/requests/{rid}/cancel/')
    # pending request cannot be deleted
    r = as_('admin1').post(f'/requests/{keep}/delete/', follow_redirects=True)
    assert 'Only cancelled requests can be deleted.' in flashes(r)
    assert as_('analyst1').post(f'/requests/{ids[1]}/delete/').status_code == 403
    assert as_('admin1').get(f'/requests/{ids[1]}/delete/').status_code == 405
    r = as_('admin1').post(f'/requests/{ids[1]}/delete/', follow_redirects=True)
    assert f'Request #{ids[1]} deleted.' in flashes(r)
    assert as_('admin1').get('/requests/delete_all_cancelled/').status_code == 405
    r = as_('admin1').post('/requests/delete_all_cancelled/', follow_redirects=True)
    assert 'Deleted 2 cancelled request(s).' in flashes(r)
    assert [r['id'] for r in q(sql, 'SELECT id FROM cc_count_requests')] == [keep]
    assert q1(sql, 'SELECT COUNT(*) AS n FROM cc_count_entries')['n'] == 0
    r = as_('admin1').post('/requests/delete_all_cancelled/', follow_redirects=True)
    assert 'No cancelled requests to delete.' in flashes(r)


def test_get_never_changes_data(as_, claimed, sql):
    """Every state-changing URL must refuse GET (the old app acted on GET)."""
    c = as_('admin1')
    for url in [f'/requests/{claimed}/claim/', f'/requests/{claimed}/review/',
                f'/requests/{claimed}/cancel/', f'/requests/{claimed}/delete/',
                '/requests/delete_all_cancelled/', f'/counts/{claimed}/save/',
                '/admin/users/1/toggle/', '/admin/users/1/delete/', '/admin/users/1/reset/',
                '/pou/map/', '/logout/']:
        assert c.get(url).status_code == 405, url
    assert as_('counter1').get(f'/counts/{claimed}/save/').status_code == 405
    assert req_row(sql, claimed)['status'] == 'in_progress'


# ------------------------------------------------------------------ dashboard

def test_dashboards_per_role(as_, users, stock, sql):
    new_request(as_('analyst1'), part='PN-EARLY', priority='1')
    early = last_request_id(sql)
    new_request(as_('prio1'), part='PN-PRIO', priority='3')
    prio = last_request_id(sql)
    # priority users' requests come first even with a lower priority number
    page = html(as_('counter2').get('/'))
    assert page.index('PN-PRIO') < page.index('PN-EARLY')
    as_('analyst1').post(f'/requests/{early}/cancel/')
    new_request(as_('analyst1'), priority='1')
    normal = last_request_id(sql)
    as_('counter1').post(f'/requests/{normal}/claim/')

    page = html(as_('counter1').get('/'))
    assert 'My active counts' in page and 'Resume count' in page and 'Start count' in page
    page = html(as_('counter2').get('/'))
    assert 'No counts in progress.' in page
    page = html(as_('analyst1').get('/'))
    assert 'My requests' in page and 'PN-PRIO' not in page
    page = html(as_('admin1').get('/'))
    assert 'In progress' in page and 'View count' in page

    data = as_('counter2').get('/api/dashboard.json').get_json()
    assert data['pending'] == [prio] and data['mine'] == []
    data = as_('admin1').get('/api/dashboard.json').get_json()
    assert data['pending'] == [prio] and data['mine'] == [normal]
    data = as_('analyst1').get('/api/dashboard.json').get_json()
    assert data['pending'] == [] and data['mine'] == [early, normal]


def test_route_states_on_cards(as_, users, sql, externals):
    externals.explosion = {'groups': [], 'error': None}       # computed, no parents
    new_request(as_('analyst1'))
    page = html(as_('analyst1').get('/'))
    assert 'none found' in page and 'Computing routes' not in page
    assert as_('analyst1').get('/api/dashboard.json').get_json()['awaiting'] == []
    new_request(as_('analyst1'), part='PN-NEW')
    sql.execute("DELETE FROM cc_parts WHERE part_number = 'PN-NEW'")
    rid = last_request_id(sql)
    assert as_('analyst1').get('/api/dashboard.json').get_json()['awaiting'] == [rid]


# ------------------------------------------------------------------ explosion

def test_explosion_fragment(as_, claimed, sql, externals):
    c = as_('counter1')
    calls = externals.explode_calls
    page = html(c.get(f'/counts/{claimed}/explosion/'))
    assert externals.explode_calls == calls            # served from cache
    assert 'data-routes="R1|R2"' in page and 'TOP-A' in page and 'TOP-B' in page
    assert 'data-qty-per="2.0"' in page and '(no route)' in page
    page = html(c.get(f'/counts/{claimed}/explosion/?ref=mod-c'))
    assert 'TOP-B' in page and 'TOP-A' not in page and 'Filtered by' in page
    page = html(c.get(f'/counts/{claimed}/explosion/?ref=NOPE'))
    assert 'No parent assemblies match' in page
    c.get(f'/counts/{claimed}/explosion/?refresh=1')
    assert externals.explode_calls == calls + 1


def test_explosion_error_keeps_existing_routes(as_, claimed, sql, externals):
    externals.explosion = {'groups': [], 'error': 'Explosion query failed: boom'}
    page = html(as_('counter1').get(f'/counts/{claimed}/explosion/?refresh=1'))
    assert 'Explosion query failed: boom' in page
    assert q1(sql, 'SELECT routes FROM cc_parts WHERE part_number = ?', PART)['routes'] == 'R1|R2'


# ------------------------------------------------------------------ lookup

def test_lookup(as_, users, stock):
    page = html(as_('counter1').get(f'/lookup/?material={PART}'))
    assert 'Capacitor 10uF' in page and '1.50000' in page
    assert '3 location(s)' in page                    # P300 is all zeros → hidden
    assert 'HU-3' in page and 'HU-4' in page
    page = html(as_('counter1').get('/lookup/?material=UNKNOWN'))
    assert 'No stock &gt; 0 found' in page and 'No part details' in page
    assert 'Search' in html(as_('counter1').get('/lookup/'))


# ------------------------------------------------------------------ POU

def test_pou_map(as_, claimed, sql):
    r = as_('counter1').post('/pou/map/', data={'pou_id': 'POU-9', 'storage_location': 'p110',
                                               'rid': str(claimed)})
    assert r.status_code == 302 and r.headers['Location'].endswith(f'/counts/{claimed}/')
    assert q1(sql, "SELECT storage_location FROM cc_pou_storage WHERE pou_id = 'POU-9'")[
        'storage_location'] == 'P110'
    as_('counter1').get(f'/counts/{claimed}/')            # seeding on load
    assert entries(sql, claimed)['P110']['is_manual'] is False   # already existed
    r = as_('counter1').post('/pou/map/', data={'pou_id': 'POU-9', 'storage_location': 'P999'},
                             follow_redirects=True)
    assert 'POU and storage location are required.' in flashes(r)
    assert as_('analyst1').post('/pou/map/', data={'pou_id': 'POU-9',
                                                   'storage_location': 'P105'}).status_code == 403


# ------------------------------------------------------------------ users

def test_users_page_works(as_, users):
    """Was a 404 in the Django app (/admin/ swallowed it)."""
    page = html(as_('admin1').get('/admin/users/'))
    assert 'analyst1' in page and 'Carla Counter' in page
    for u in ('analyst1', 'counter1', 'prio1'):
        assert as_(u).get('/admin/users/').status_code == 403
        assert as_(u).get('/admin/users/new/').status_code == 403


@pytest.mark.parametrize('form,message', [
    ({'username': ''}, 'All fields are required.'),
    ({'username': 'bad name'}, 'Username may only contain letters, digits, ".", "_" and "-".'),
    ({'email': 'nope'}, 'Invalid email address.'),
    ({'role': 'superuser'}, 'Invalid role.'),
    ({'password': '123'}, 'Password must be at least 6 characters.'),
    ({'username': 'analyst1'}, 'User "analyst1" already exists.'),
])
def test_create_user_validation(as_, users, form, message):
    data = {'username': 'newbie', 'full_name': 'New Bie', 'email': 'n@x.com',
            'password': 'longpass', 'role': 'analyst'}
    data.update(form)
    r = as_('admin1').post('/admin/users/new/', data=data)
    assert message in flashes(r)


def test_create_user_and_login(app, as_, users):
    r = as_('admin1').post('/admin/users/new/', data={
        'username': 'newbie', 'full_name': 'New Bie', 'email': 'n@x.com',
        'password': 'longpass', 'role': 'cycle_counter'}, follow_redirects=True)
    assert 'User "newbie" created.' in flashes(r)
    c = app.test_client()
    assert login(c, 'newbie', 'longpass').status_code == 302
    assert 'Pending requests' in html(c.get('/'))


def test_toggle_delete_reset_rules(as_, users, sql, stock):
    admin = as_('admin1')
    me = users['admin']['id']
    r = admin.post(f'/admin/users/{me}/toggle/', follow_redirects=True)
    assert 'You cannot deactivate your own account.' in flashes(r)
    r = admin.post(f'/admin/users/{me}/delete/', follow_redirects=True)
    assert 'You cannot delete your own account.' in flashes(r)
    uid = users['counter2']['id']
    admin.post(f'/admin/users/{uid}/toggle/')
    assert q1(sql, 'SELECT active FROM cc_users WHERE id = ?', uid)['active'] is False
    admin.post(f'/admin/users/{uid}/toggle/')
    assert q1(sql, 'SELECT active FROM cc_users WHERE id = ?', uid)['active'] is True
    r = admin.post(f'/admin/users/{uid}/reset/', data={'new_password': '123'}, follow_redirects=True)
    assert 'Password must be at least 6 characters.' in flashes(r)
    r = admin.post(f'/admin/users/{uid}/delete/', follow_redirects=True)
    assert 'User "counter2" deleted.' in flashes(r)
    assert admin.post(f'/admin/users/{uid}/delete/').status_code == 404
    assert admin.post('/admin/users/9999/toggle/').status_code == 404
    assert admin.post('/admin/users/9999/reset/', data={'new_password': 'whatever'}).status_code == 404


def test_cannot_delete_referenced_user(as_, done, users, sql):
    admin = as_('admin1')
    as_('admin1').post(f'/requests/{done}/review/', data={'outcome': 'adjust'})
    for key in ('analyst', 'counter'):
        r = admin.post(f'/admin/users/{users[key]["id"]}/delete/', follow_redirects=True)
        assert 'Deactivate instead of deleting.' in ' '.join(flashes(r))
    # the reviewer is referenced only through reviewed_by
    other_admin = make_user('admin2', 'admin')
    c = admin.application.test_client()
    login(c, 'admin2')
    r = c.post(f'/admin/users/{users["admin"]["id"]}/delete/', follow_redirects=True)
    assert 'Deactivate instead of deleting.' in ' '.join(flashes(r))
    assert other_admin


# ------------------------------------------------------------------ errors / display

def test_query_errors_are_500_not_503(app, as_, users, monkeypatch):
    from cycle_count import db

    def bad():
        with db._connect() as con:
            con.execute('SELECT * FROM no_such_table')
    monkeypatch.setattr(db, 'list_requests', lambda **k: bad())
    r = as_('analyst1').get('/')
    assert r.status_code == 500 and 'Unexpected error' in html(r)


def test_error_pages(as_, users):
    r = as_('analyst1').get('/no/such/page')
    assert r.status_code == 404 and 'Page not found.' in html(r) and 'Back to dashboard' in html(r)
    r = as_('analyst1').get('/admin/users/')
    assert r.status_code == 403 and 'permission' in html(r)


def test_database_down_shows_503(app, users):
    c = app.test_client()
    login(c, 'analyst1')
    from cycle_count import db
    good = db._conn_str
    db.configure(good.replace('DATABASE=cc_pytest', 'DATABASE=cc_missing_db'))
    try:
        r = c.get('/')
        assert r.status_code == 503 and 'database is not reachable' in html(r)
    finally:
        db.configure(good)


def test_times_shown_in_lisbon_time(as_, done, sql):
    sql.execute("UPDATE cc_count_requests SET completed_at = '2026-07-01T10:00:00' WHERE id = ?", done)
    page = html(as_('admin1').get('/requests/'))
    assert '2026-07-01 11:00' in page          # WEST = UTC+1 in July


def test_html_is_escaped_everywhere(as_, users, stock, sql):
    evil = '<script>alert(1)</script>'
    new_request(as_('analyst1'), part='PN-<b>', reason=evil)
    rid = last_request_id(sql)
    page = html(as_('admin1').get('/'))
    assert '<script>alert(1)' not in page and '&lt;script&gt;alert(1)' in page
    for url in ['/', '/requests/']:
        page = html(as_('admin1').get(url))
        assert 'PN-<b>' not in page and 'PN-&lt;b&gt;' in page
    body = FakeSMTP.sent[-1]['msg'].get_body(('html',)).get_content()
    assert '<script>alert' not in body
    as_('counter1').post(f'/requests/{rid}/claim/')
    page = html(as_('counter1').get(f'/counts/{rid}/'))
    assert 'PN-<b>' not in page


def test_priority_label_and_dates(as_, users, sql):
    new_request(as_('analyst1'), priority='1', count_date='2026-12-24')
    page = html(as_('analyst1').get('/'))
    assert '1 — High' in page and '2026-12-24' in page
    assert datetime  # imported for clarity of intent
