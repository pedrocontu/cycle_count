"""End-to-end tests in a real Chromium (Playwright) against a live server.

Set CC_TEST_CHROMIUM to a Chromium binary if Playwright's own browser is not
installed (e.g. /opt/pw-browsers/chromium-1194/chrome-linux/chrome).
"""
import json
import os
import threading

import pytest
from werkzeug.serving import make_server

from .conftest import PART, last_request_id, q, q1

playwright_api = pytest.importorskip('playwright.sync_api')


@pytest.fixture(scope='session')
def browser():
    with playwright_api.sync_playwright() as p:
        exe = os.environ.get('CC_TEST_CHROMIUM')
        b = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def server(app):
    app.config['WTF_CSRF_ENABLED'] = True          # exercise the real CSRF tokens
    srv = make_server('127.0.0.1', 0, app, threaded=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f'http://127.0.0.1:{srv.server_port}'
    srv.shutdown()


class Page:
    """Playwright page that records JS errors and auto-accepts confirm()."""

    def __init__(self, browser, base):
        self.ctx = browser.new_context()
        self.p = self.ctx.new_page()
        self.p.set_default_timeout(8000)
        self.base = base
        self.errors = []
        self.dialogs = []
        self.p.on('pageerror', lambda e: self.errors.append(str(e)))
        self.p.on('console', lambda m: m.type == 'error' and self.errors.append(m.text))

        def on_dialog(d):
            self.dialogs.append(d.message)
            d.accept()
        self.p.on('dialog', on_dialog)

    def go(self, path):
        self.p.goto(self.base + path)
        return self.p

    def login(self, username):
        self.go('/login/')
        self.p.fill('#username', username)
        self.p.fill('#password', 'secret123')
        self.p.click('button[type=submit]')
        self.p.wait_for_url(self.base + '/')

    def val(self, selector):
        return self.p.input_value(selector)

    def text(self, selector):
        return self.p.inner_text(selector).strip()

    def close(self):
        assert self.errors == [], self.errors
        self.ctx.close()


@pytest.fixture
def pages(browser, server):
    opened = []

    def make(username=None):
        pg = Page(browser, server)
        if username:
            pg.login(username)
        opened.append(pg)
        return pg
    yield make
    for pg in opened:
        pg.close()


@pytest.fixture
def counting(pages, users, stock, sql):
    """Request created by analyst1 (via the UI) and claimed by counter1."""
    a = pages('analyst1')
    a.go('/requests/new/')
    a.p.fill('#part_number', PART)
    a.p.fill('#reason', 'Variance after inventory')
    a.p.select_option('#priority', '2')
    a.p.click('button:has-text("Create request")')
    a.p.wait_for_selector('.flashes')
    rid = last_request_id(sql)
    c = pages('counter1')
    c.p.click('button:has-text("Start count")')
    c.p.wait_for_url(f'{c.base}/counts/{rid}/')
    return c, rid


def entry(sql, rid, sl):
    return q1(sql, 'SELECT * FROM cc_count_entries WHERE request_id = ? AND storage_location = ?',
              rid, sl)


def save_progress(c):
    c.p.click('button:has-text("Save progress")')
    c.p.wait_for_selector('.flashes li:has-text("Progress saved.")')


# ---------------------------------------------------------------- tests

def test_typed_p105_survives_reload(counting, sql):
    """The old page blanked P105 on every load (and the next save wiped it)."""
    c, rid = counting
    c.p.fill('#qty__P105', '30')
    c.p.fill('#qty__P110', '16')
    assert c.text('#t-cnt') == '46'
    save_progress(c)
    assert c.val('#qty__P105') == '30' and c.val('#qty__P110') == '16'
    c.p.reload()
    assert c.val('#qty__P105') == '30'
    save_progress(c)
    assert entry(sql, rid, 'P105')['counted_qty'] == 30


def test_totals_and_deltas(counting):
    c, _ = counting
    c.p.fill('#qty__P110', '20')       # QOH 17, unit 2 → +3, +6.00
    card = c.p.locator('.sl-card[data-sl="P110"]')
    assert card.locator('[data-delta-cell]').inner_text() == '+3'
    assert card.locator('[data-valor-cell]').inner_text() == '+6.00'
    assert c.text('#t-sys') == '20'                   # 17 + 3 (P200) + 0 (P105)
    c.p.fill('#qty__P200', '1')                       # -2 × 2 = -4
    assert c.text('#t-delta') == '+1' and c.text('#t-valor') == '+2.00'
    assert 'delta-neg' in c.p.locator('.sl-card[data-sl="P200"] [data-delta-cell]').get_attribute('class')


def test_feeders_fill_p105_and_are_restored(counting, sql):
    c, rid = counting
    inputs = c.p.locator('tr.usage-data-row .usage-qty')
    inputs.nth(0).fill('4')
    inputs.nth(1).fill('6')
    assert c.val('#qty__P105') == '10' and c.text('#usage-total') == '10'
    c.p.click('#add-usage-line')
    manual = c.p.locator('tr.usage-manual')
    manual.locator('input[type=text]').fill('Shelf <b>7</b>')
    manual.locator('.usage-qty').fill('5')
    assert c.val('#qty__P105') == '15'
    save_progress(c)
    assert c.p.locator('tr.usage-manual input[type=text]').input_value() == 'Shelf <b>7</b>'
    assert c.val('#qty__P105') == '15' and c.text('#usage-total') == '15'
    meta = json.loads(q1(sql, 'SELECT progress_meta FROM cc_count_requests WHERE id = ?', rid)['progress_meta'])
    assert [f.get('count') for f in meta['feeders']] == ['4', '6', '5']
    c.p.locator('tr.usage-manual button').click()
    assert c.val('#qty__P105') == '10'
    inputs.nth(0).fill('')
    inputs.nth(1).fill('')
    assert c.val('#qty__P105') == ''


def test_enter_moves_to_next_feeder_and_shows_resume_bar(counting):
    c, _ = counting
    first = c.p.locator('tr.usage-data-row .usage-qty').nth(0)
    first.click()
    assert 'show' in c.p.get_attribute('#feeder-resume', 'class')
    assert 'L1 · M1' in c.text('#feeder-resume') and 'MOD-A' in c.text('#feeder-resume')
    first.press('Enter')
    assert c.p.evaluate("document.activeElement === document.querySelectorAll('.usage-qty')[1]")


def test_hu_tree_sums_into_location(counting):
    c, _ = counting
    card = c.p.locator('.sl-card[data-sl="P110"]')
    card.locator('details.type-tree > summary').click()
    card.locator('details.st-row').first.locator('summary span').first.click()
    card.locator('details.bin-row summary span').first.click()
    hu1 = card.locator('li[data-hu="HU-1"] .hu-check')
    hu1.check()
    assert c.val('#qty__P110') == '4'
    card.locator('li[data-hu="HU-1"] .hu-qty').fill('5')
    assert c.val('#qty__P110') == '5'
    card.locator('.bin-check').first.check()            # HU-1 (5) + HU-2 (6)
    assert c.val('#qty__P110') == '11'
    assert card.locator('.st-check').first.is_checked()
    card.locator('.st-check').first.uncheck()
    assert c.val('#qty__P110') == ''
    hu1.check()
    assert c.p.evaluate("document.querySelector('.sl-card[data-sl=\"P110\"] .bin-check').indeterminate")


def test_hu_ticks_restored_without_changing_count(counting):
    c, _ = counting
    card = c.p.locator('.sl-card[data-sl="P110"]')
    card.locator('details.type-tree > summary').click()
    card.locator('details.st-row').first.locator('summary span').first.click()
    card.locator('details.bin-row summary span').first.click()
    card.locator('li[data-hu="HU-2"] .hu-check').check()
    c.p.fill('#qty__P110', '99')                       # counter overrides by hand
    save_progress(c)
    assert c.val('#qty__P110') == '99'
    card = c.p.locator('.sl-card[data-sl="P110"]')
    assert card.locator('li[data-hu="HU-2"] .hu-check').is_checked()
    # the same HU listed under P200 (stock type valid for every SL) is separate
    assert not c.p.locator('.sl-card[data-sl="P200"] li[data-hu="HU-2"] .hu-check').is_checked()


def test_pou_slots_add_to_p105(counting):
    c, _ = counting
    rows = c.p.locator('.sl-card[data-sl="P105"] .pou-slot-row')
    rows.nth(0).locator('.pou-confirm').check()          # fifo qty 7
    assert c.val('#qty__P105') == '7'
    rows.nth(0).locator('.pou-fifoqty').fill('9')
    assert c.val('#qty__P105') == '9'
    c.p.locator('tr.usage-data-row .usage-qty').nth(0).fill('1')
    assert c.val('#qty__P105') == '10'
    save_progress(c)
    assert c.p.locator('.sl-card[data-sl="P105"] .pou-slot-row').nth(0).locator('.pou-confirm').is_checked()
    assert c.val('#qty__P105') == '10'


def open_groups(c, part):
    c.p.wait_for_selector(f'.expl-row[data-part="{part}"]', state='attached')
    c.p.evaluate("document.querySelectorAll('details.expl-group').forEach(d => d.open = true)")
    return c.p.locator(f'.expl-row[data-part="{part}"]')


def test_explosion_counts_survive_filter_and_reload(counting, sql):
    c, rid = counting
    c.p.click('#explosion-load-btn')
    sub1 = open_groups(c, 'SUB-1')
    assert sub1.count() == 2                          # TOP-A has routes R1 and R2
    assert 'R1' in c.text('#part-routes') and 'R2' in c.text('#part-routes')
    sub1.first.locator('.expl-qty').fill('3')         # 3 × 2, counted once
    assert sub1.nth(1).locator('.expl-qty').input_value() == '3'
    assert c.val('#qty__P105') == '6' and c.text('#expl-total') == '6'
    assert sub1.first.locator('[data-subtotal]').inner_text() == '6'
    # filter to TOP-B only: the SUB-1 count is kept and still counted
    c.p.fill('#expl-ref-input', 'mod-c')
    c.p.press('#expl-ref-input', 'Enter')
    c.p.wait_for_selector('#explosion-host :text("Filtered by")')
    sub2 = open_groups(c, 'SUB-2')
    assert c.p.locator('.expl-row[data-part="SUB-1"]').count() == 0
    sub2.first.locator('.expl-qty').fill('1')         # 1 × 3
    assert c.val('#qty__P105') == '9' and c.text('#expl-total') == '9'
    c.p.click('#expl-ref-clear')
    c.p.wait_for_selector('#explosion-host :text("Filtered by")', state='detached')
    sub1 = open_groups(c, 'SUB-1')
    assert sub1.first.locator('.expl-qty').input_value() == '3'
    save_progress(c)
    sub1 = open_groups(c, 'SUB-1')                    # auto-loaded after reload
    assert sub1.first.locator('.expl-qty').input_value() == '3'
    assert open_groups(c, 'SUB-2').first.locator('.expl-qty').input_value() == '1'
    assert c.val('#qty__P105') == '9'
    assert entry(sql, rid, 'P105')['counted_qty'] == 9


def test_explosion_hide_toggle_is_remembered(counting):
    c, _ = counting
    assert c.text('#explosion-toggle-btn') == 'Hide explosion'
    c.p.click('#explosion-toggle-btn')
    assert not c.p.is_visible('#explosion-body')
    c.p.reload()
    assert not c.p.is_visible('#explosion-body') and c.text('#explosion-toggle-btn') == 'Show explosion'


def test_explosion_load_error_is_shown(counting, externals, sql):
    c, _ = counting
    externals.explosion = {'groups': [], 'error': 'Azure connect failed: timeout'}
    sql.execute('UPDATE cc_parts SET explosion_json = NULL')
    c.p.click('#explosion-load-btn')
    c.p.wait_for_selector('#explosion-host :text("Azure connect failed")')
    c.p.fill('#expl-ref-input', 'x')
    c.p.click('#expl-ref-add')
    assert c.p.locator('#expl-ref-chips .chip').count() == 1


def test_add_and_remove_location_keeps_typed_counts(counting, sql):
    c, rid = counting
    c.p.fill('#qty__P110', '12')
    c.p.select_option('#add_sl', 'P400')
    c.p.click('button:has-text("+ Add location")')
    c.p.wait_for_selector('.sl-card[data-sl="P400"]')
    assert c.val('#qty__P110') == '12'
    c.p.fill('#qty__P400', '2')
    c.p.click('.sl-card[data-sl="P400"] [data-delete-sl]')
    c.p.wait_for_selector('.flashes li:has-text("Removed manual location P400.")')
    assert c.p.locator('.sl-card[data-sl="P400"]').count() == 0
    assert c.val('#qty__P110') == '12'
    assert entry(sql, rid, 'P400') is None


def test_finish_and_readonly_view(counting, pages, sql):
    c, rid = counting
    c.p.locator('tr.usage-data-row .usage-qty').nth(0).fill('4')
    c.p.click('#add-usage-line')
    c.p.locator('tr.usage-manual input[type=text]').fill('Trolley')
    c.p.locator('tr.usage-manual .usage-qty').fill('6')
    assert c.val('#qty__P105') == '10'
    c.p.fill('#count_notes', 'All good')
    c.p.click('#finish-count-btn')
    c.p.wait_for_selector('.flashes li:has-text("finished")')
    assert any('Finish this count?' in d for d in c.dialogs)
    assert q1(sql, 'SELECT status FROM cc_count_requests WHERE id = ?', rid)['status'] == 'done'

    admin = pages('admin1')
    admin.go(f'/counts/{rid}/')
    # the old page recomputed P105 without the manual line (showed 4)
    assert admin.val('#qty__P105') == '10'
    assert admin.p.is_disabled('#qty__P105')
    assert admin.p.locator('tr.usage-manual .usage-qty').input_value() == '6'
    assert admin.p.locator('tr.usage-manual .usage-qty').is_disabled()
    assert admin.p.locator('#finish-count-btn').count() == 0
    assert admin.text('#t-cnt') == '10'


def test_html_in_feeder_names_is_not_executed(counting, externals, sql):
    c, rid = counting
    externals.usage_rows = [{'line_name': '<img src=x onerror="window.__xss=1">', 'machine_name': 'M"1',
                             'feed': 1, 'bank': 'A', 'track': 1, 'models': '<b>MOD</b>',
                             'last_used': None}]
    c.p.reload()
    c.p.locator('tr.usage-data-row .usage-qty').first.click()
    assert c.p.evaluate('window.__xss') is None
    assert '<img src=x' in c.text('#feeder-resume')
    c.p.locator('tr.usage-data-row .usage-qty').first.fill('2')
    save_progress(c)
    c.p.locator('tr.usage-data-row .usage-qty').first.click()
    assert c.p.evaluate('window.__xss') is None
    assert c.p.locator('tr.usage-data-row .usage-qty').first.input_value() == '2'


def test_unmapped_pou_mapping(counting, sql):
    c, rid = counting
    c.p.fill('#qty__P110', '13')                       # typed but not saved yet
    c.p.select_option('.pou-map-row select', 'P110')
    c.p.click('.pou-map-row button')
    c.p.wait_for_selector('.flashes li:has-text("POU POU-9 mapped to P110.")')
    assert c.p.locator('.unmapped-pou-card').count() == 0
    assert c.p.locator('.sl-card[data-sl="P110"] .pou-slot-row').count() == 1
    assert c.val('#qty__P110') == '13'
    assert entry(sql, rid, 'P110')['counted_qty'] == 13


def test_save_works_with_unmapped_pou(counting, sql):
    """The POU-map <form>s used to be nested inside the count form; their
    required <select> blocked every Save / Finish while a POU was unmapped."""
    c, rid = counting
    assert c.p.locator('.unmapped-pou-card').count() == 1
    c.p.fill('#qty__P110', '8')
    save_progress(c)
    assert entry(sql, rid, 'P110')['counted_qty'] == 8


def test_no_p105_warning(counting, sql, pages):
    c, rid = counting
    sql.execute('DELETE FROM cc_mars_pou')
    c.p.click('.sl-card[data-sl="P105"] [data-delete-sl]')
    c.p.wait_for_selector('#no-p105-note')


def test_review_from_requests_page(counting, pages, sql):
    c, rid = counting
    c.p.fill('#qty__P110', '17')
    c.p.click('#finish-count-btn')
    c.p.wait_for_selector('.flashes')
    admin = pages('admin1')
    admin.go('/requests/')
    assert admin.text('.stage-done .count') == '1'
    admin.p.fill('#pn-filter', 'nothing-matches')
    assert admin.p.locator('tr.filter-empty').count() == 1
    assert admin.text('.stage-done .count') == '0/1'
    admin.p.fill('#pn-filter', 'pn-1')
    admin.p.click(f'form.review-form[data-rid="{rid}"] button[value="adjust"]')
    admin.p.wait_for_selector(f'tr[data-rid="{rid}"]', state='detached')
    assert admin.text('.stage-done .count') == '0/0'
    assert admin.p.locator('.stage-done tr.empty-row').count() >= 1
    row = q1(sql, 'SELECT status, review_outcome FROM cc_count_requests WHERE id = ?', rid)
    assert row == {'status': 'reviewed', 'review_outcome': 'adjust'}
    admin.go('/requests/?status=reviewed')
    assert f'#{rid}' in admin.text('.stage-reviewed')


def test_recount_from_requests_page(counting, pages, sql):
    c, rid = counting
    c.p.fill('#qty__P110', '1')
    c.p.click('#finish-count-btn')
    c.p.wait_for_selector('.flashes')
    admin = pages('admin1')
    admin.go('/requests/')
    admin.p.click(f'form.review-form[data-rid="{rid}"] button[value="recount"]')
    admin.p.wait_for_selector(f'tr[data-rid="{rid}"]', state='detached')
    assert any('recount' in d for d in admin.dialogs)
    assert q1(sql, 'SELECT status FROM cc_count_requests WHERE id = ?', rid)['status'] == 'pending'
    assert q(sql, 'SELECT * FROM cc_count_entries WHERE request_id = ?', rid) == []


def test_every_page_loads_without_js_errors(counting, pages, sql):
    c, rid = counting
    for path in ['/', '/requests/', '/requests/?status=reviewed', '/requests/?status=cancelled',
                 f'/lookup/?material={PART}', f'/counts/{rid}/']:
        c.go(path)
    admin = pages('admin1')
    for path in ['/', '/admin/users/', '/admin/users/new/', f'/counts/{rid}/', '/requests/',
                 '/requests/new/']:
        admin.go(path)
    # dashboard polling endpoint answers and does not trigger a reload loop
    admin.go('/')
    data = admin.p.evaluate("fetch('/api/dashboard.json').then(r => r.json())")
    assert data['mine'] == [rid]


def test_user_management_ui(pages, users, sql):
    admin = pages('admin1')
    admin.go('/admin/users/new/')
    admin.p.fill('#username', 'newcounter')
    admin.p.fill('#full_name', 'New Counter')
    admin.p.fill('#email', 'nc@example.com')
    admin.p.select_option('#role', 'cycle_counter')
    admin.p.fill('#password', 'pass1234')
    admin.p.click('button:has-text("Create user")')
    admin.p.wait_for_selector('.flashes li:has-text("created")')
    row = admin.p.locator('tr:has-text("newcounter")')
    row.locator('summary').click()
    row.locator('button:has-text("Deactivate")').click()
    admin.p.wait_for_selector('.flashes li:has-text("deactivated")')
    assert q1(sql, "SELECT active FROM cc_users WHERE username = 'newcounter'")['active'] is False
    row = admin.p.locator('tr:has-text("newcounter")')
    row.locator('summary').click()
    row.locator('button:has-text("Delete user")').click()
    admin.p.wait_for_selector('.flashes li:has-text("deleted")')
    assert any('Permanently delete user "newcounter"' in d for d in admin.dialogs)


def test_logout_button(pages, users):
    a = pages('analyst1')
    a.p.click('nav button:has-text("Log out")')
    a.p.wait_for_url(a.base + '/login/')
    a.go('/')
    assert a.p.url.endswith('/login/')


def test_cancel_from_dashboard(pages, users, sql):
    a = pages('analyst1')
    a.go('/requests/new/')
    a.p.fill('#part_number', 'PN-X')
    a.p.fill('#reason', 'r')
    a.p.select_option('#priority', '3')
    a.p.click('button:has-text("Create request")')
    a.p.wait_for_selector('.flashes')
    a.p.click('button:has-text("Cancel request")')
    a.p.wait_for_selector('.flashes li:has-text("cancelled")')
    assert q1(sql, 'SELECT status FROM cc_count_requests')['status'] == 'cancelled'
