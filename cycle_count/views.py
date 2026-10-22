import json
import logging
import re
from datetime import date
from threading import Thread

from flask import (Blueprint, abort, flash, g, jsonify, redirect, render_template,
                   request, url_for)
from flask_wtf.csrf import CSRFError
from werkzeug.exceptions import HTTPException

from . import auth, bom, db, emails, usage
from .auth import login_required, role_required

log = logging.getLogger(__name__)
bp = Blueprint('cc', __name__)

EMAIL_RE = re.compile(r'^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$')
USERNAME_RE = re.compile(r'^[A-Za-z0-9._-]{1,50}$')
SL_RE = re.compile(r'^[A-Z0-9_-]{1,10}$')
POU_STORAGE_LOCATIONS = ('P105', 'P110')
NO_ROUTE = '(no route)'


@bp.before_app_request
def _load_user():
    auth.load_current_user()


def _uid():
    return int(g.user['id'])


def _parse_qty(raw):
    raw = (raw or '').strip().replace(',', '.')
    if raw == '':
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _get_request_or_404(rid):
    req = db.get_request(rid)
    if not req:
        abort(404)
    return req


def _require_active_counter(req):
    """Only the counter assigned to an in-progress count may change it."""
    if req['status'] != 'in_progress':
        abort(409, 'This count is not in progress any more.')
    if req['assigned_to'] != _uid():
        abort(403)


# =====================================================================
# Auth
# =====================================================================

@bp.route('/login/', methods=['GET', 'POST'])
def login():
    if g.user:
        return redirect(url_for('cc.dashboard'))
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        user = auth.authenticate(username, password)
        if user:
            auth.login_user(user)
            return redirect(url_for('cc.dashboard'))
        flash('Invalid username or password.', 'error')
    return render_template('login.html')


@bp.route('/logout/', methods=['POST'])
def logout():
    auth.logout_user()
    return redirect(url_for('cc.login'))


# =====================================================================
# Dashboard
# =====================================================================

def _dashboard_lists(user):
    """Returns (pending, mine, other_in_progress, my_requests); None = section
    not shown for this role."""
    uid = int(user['id'])
    role = user['role']
    if role == 'cycle_counter':
        return (db.list_requests(status='pending'),
                db.list_requests(status='in_progress', assigned_to=uid), None, None)
    if role in ('analyst', 'priority'):
        return None, None, None, db.list_requests(requested_by=uid)
    return (db.list_requests(status='pending'), None,
            db.list_requests(status='in_progress'), None)


def _ids(rows):
    return [r['id'] for r in rows or []]


def _awaiting_routes_ids(*lists):
    # routes IS NULL → not computed yet ('' means computed, none found)
    return [r['id'] for rows in lists for r in rows or [] if r.get('part_routes') is None]


@bp.route('/api/dashboard.json')
@login_required
def api_dashboard():
    pending, mine, other, my_requests = _dashboard_lists(g.user)
    return jsonify({
        'pending': _ids(pending),
        'mine': _ids(mine if mine is not None else (other if other is not None else my_requests)),
        'awaiting': _awaiting_routes_ids(pending, mine, other, my_requests),
    })


@bp.route('/')
@login_required
def dashboard():
    pending, mine, other, my_requests = _dashboard_lists(g.user)
    return render_template(
        'dashboard.html',
        pending=pending, mine=mine, other_in_progress=other, my_requests=my_requests,
        pending_ids=_ids(pending),
        mine_ids=_ids(mine if mine is not None else (other if other is not None else my_requests)),
        awaiting_ids=_awaiting_routes_ids(pending, mine, other, my_requests),
    )


# =====================================================================
# Count requests
# =====================================================================

def _clean_cc_emails(raw):
    addrs = [a.strip() for a in re.split(r'[,;\s]+', raw or '') if a.strip()]
    bad = [a for a in addrs if not EMAIL_RE.match(a)]
    if bad:
        raise ValueError('Invalid email address(es) in CC: ' + ', '.join(bad))
    return ', '.join(addrs) or None


@bp.route('/requests/new/', methods=['GET', 'POST'])
@role_required('analyst', 'priority', 'admin')
def request_new():
    form = request.form if request.method == 'POST' else {}
    if request.method == 'POST':
        try:
            part_number = (form.get('part_number') or '').strip()
            raw_date = (form.get('count_date') or '').strip()
            reason = (form.get('reason') or '').strip()
            if not part_number:
                raise ValueError('Part number is required.')
            if len(part_number) > 40:
                raise ValueError('Part number is too long.')
            if not raw_date:
                raise ValueError('Date is required.')
            try:
                count_date = date.fromisoformat(raw_date)
            except ValueError:
                raise ValueError('Date must be a valid date (YYYY-MM-DD).')
            if not reason:
                raise ValueError('Reason is required.')
            try:
                priority = int(form.get('priority') or 0)
            except ValueError:
                priority = 0
            if priority not in (1, 2, 3):
                raise ValueError('Priority must be 1, 2, or 3.')
            cc_emails = _clean_cc_emails(form.get('cc_emails'))
        except ValueError as e:
            flash(str(e), 'error')
        else:
            rid = db.create_request(
                part_number=part_number, count_date=count_date, reason=reason,
                priority=priority, cc_emails=cc_emails, requested_by=_uid(),
            )
            _precompute_explosion_async(part_number)
            try:
                emails.send_request_created(rid)
                flash(f'Request #{rid} created. Notification sent.', 'success')
            except Exception as exc:
                log.exception('request-created email failed')
                flash(f'Request #{rid} created, but email failed: {exc}', 'error')
            return redirect(url_for('cc.dashboard'))
    return render_template('request_new.html', today=date.today().isoformat(), form=form)


@bp.route('/requests/<int:rid>/claim/', methods=['POST'])
@role_required('cycle_counter')
def request_claim(rid):
    _get_request_or_404(rid)
    if not db.claim_request(rid, _uid()):
        flash('That request is no longer available to claim.', 'error')
        return redirect(url_for('cc.dashboard'))
    req = db.get_request(rid)
    try:
        db.seed_pou_entries(rid, req['part_number'])
    except Exception:
        log.exception('seed_pou_entries failed for #%s (retried on screen load)', rid)
    flash(f'Count #{rid} started — assigned to you.', 'success')
    return redirect(url_for('cc.count_screen', rid=rid))


def _wants_json():
    return request.headers.get('X-Requested-With') == 'fetch'


@bp.route('/requests/<int:rid>/review/', methods=['POST'])
@role_required('admin')
def request_review(rid):
    req = _get_request_or_404(rid)
    outcome = (request.form.get('outcome') or '').strip()
    if req['status'] != 'done':
        msg = 'Only completed counts can be reviewed.'
        if _wants_json():
            return msg, 409
        flash(msg, 'error')
        return redirect(url_for('cc.requests_all'))
    if outcome == 'recount':
        ok = db.reopen_for_recount(rid)
        success = f'Request #{rid} sent back to Open requests for recount.'
    elif outcome in db.REVIEW_OUTCOMES:
        ok = db.review_request(rid, outcome, _uid())
        label = 'Adjust' if outcome == 'adjust' else 'Not Adjust'
        success = f'Request #{rid} marked as Reviewed / {label}.'
    else:
        if _wants_json():
            return 'Invalid outcome', 400
        flash('Pick a valid review outcome.', 'error')
        return redirect(url_for('cc.requests_all'))
    if not ok:
        msg = 'This count changed in the meantime — reload the page.'
        if _wants_json():
            return msg, 409
        flash(msg, 'error')
        return redirect(url_for('cc.requests_all'))
    if _wants_json():
        return '', 204
    flash(success, 'success')
    return redirect(url_for('cc.requests_all'))


@bp.route('/requests/delete_all_cancelled/', methods=['POST'])
@role_required('admin')
def request_delete_all_cancelled():
    n = db.delete_all_cancelled()
    flash(f'Deleted {n} cancelled request(s).' if n else 'No cancelled requests to delete.',
          'success')
    return redirect(url_for('cc.requests_all', status='cancelled'))


@bp.route('/requests/<int:rid>/delete/', methods=['POST'])
@role_required('admin')
def request_delete(rid):
    _get_request_or_404(rid)
    if db.delete_request(rid):
        flash(f'Request #{rid} deleted.', 'success')
    else:
        flash('Only cancelled requests can be deleted.', 'error')
    return redirect(url_for('cc.requests_all', status='cancelled'))


@bp.route('/requests/<int:rid>/cancel/', methods=['POST'])
@role_required('admin', 'analyst', 'priority')
def request_cancel(rid):
    req = _get_request_or_404(rid)
    if g.user['role'] != 'admin' and req['requested_by'] != _uid():
        abort(403)
    if db.cancel_request(rid):
        flash(f'Request #{rid} cancelled.', 'success')
    else:
        flash('Only pending or in-progress requests can be cancelled.', 'error')
    return redirect(url_for('cc.dashboard'))


# =====================================================================
# Count screen
# =====================================================================

def _build_ref_types(bins_by_type, st_desc):
    out = []
    for st, bins_dict in bins_by_type.items():
        bins_list = []
        st_total = 0
        for bin_name, hus in bins_dict.items():
            bin_total = sum((hu['qty'] or 0) for hu in hus)
            st_total += bin_total
            bins_list.append({'bin': bin_name, 'total_qty': bin_total, 'hus': hus})
        out.append({'storage_type': st, 'st_description': st_desc.get(st),
                    'total_qty': st_total, 'bins': bins_list})
    return out


@bp.route('/counts/<int:rid>/')
@login_required
def count_screen(rid):
    req = _get_request_or_404(rid)
    if req['status'] == 'pending':
        flash(f'Count #{rid} has not been started yet.', 'error')
        return redirect(url_for('cc.dashboard'))
    if g.user['role'] == 'cycle_counter' and req['status'] == 'cancelled':
        flash(f'Count #{rid} is cancelled.', 'error')
        return redirect(url_for('cc.dashboard'))
    if req['status'] == 'in_progress':
        db.seed_pou_entries(rid, req['part_number'])

    all_entries = db.get_entries(rid)
    pou_slots_by_sl = db.get_pou_slots_for_part(req['part_number'])
    unmapped_pou_slots = pou_slots_by_sl.pop(None, [])

    entries = [e for e in all_entries
               if (e['system_qty'] or 0) != 0
               or e['counted_qty'] is not None
               or e.get('is_manual')
               or e['storage_location'] in pou_slots_by_sl]
    entries.sort(key=lambda e: 1 if e['storage_location'] == 'P105' else 0)

    bin_tree = db.fetch_bin_reference(req['part_number'])
    sl_desc = db.get_storage_location_descriptions()
    st_desc = db.get_storage_type_descriptions('PT01')

    for e in entries:
        e['sl_description'] = sl_desc.get(e['storage_location'])
        raw_slots = pou_slots_by_sl.get(e['storage_location'], [])
        groups = {}
        for s in raw_slots:
            groups.setdefault(s['pou_id'], []).append(s)
        e['pou_slots'] = raw_slots
        e['pou_groups'] = list(groups.items())
        e['ref_types'] = _build_ref_types(bin_tree.get(e['storage_location'], {}), st_desc)

    total_system = sum((e['system_qty'] or 0) for e in entries)
    total_counted = sum((e['counted_qty'] or 0) for e in entries)
    currency = next((e['currency'] for e in entries if e['currency']), '')

    in_use = {e['storage_location'] for e in all_entries}
    add_sl_options = [(code, sl_desc[code]) for code in sorted(sl_desc) if code not in in_use]

    unmapped_pou_ids = list(dict.fromkeys(s['pou_id'] for s in unmapped_pou_slots if s['pou_id']))

    try:
        initial_meta = json.loads(req.get('progress_meta') or '{}')
        if not isinstance(initial_meta, dict):
            initial_meta = {}
    except ValueError:
        initial_meta = {}

    readonly = req['status'] != 'in_progress' or req['assigned_to'] != _uid()

    return render_template(
        'count_screen.html',
        req=req,
        part=db.get_part_info(req['part_number']),
        entries=entries,
        total_system=total_system,
        total_counted=total_counted,
        currency=currency,
        add_sl_options=add_sl_options,
        machine_usage=usage.get_machine_usage(req['part_number']),
        unmapped_pou_slots=unmapped_pou_slots,
        unmapped_pou_ids=unmapped_pou_ids,
        initial_meta=initial_meta,
        readonly=readonly,
    )


def _current_currency(rid):
    return next((e['currency'] for e in db.get_entries(rid) if e['currency']), None)


def _manual_unit_value(part_number):
    part = db.get_part_info(part_number)
    return part['unit_cost'] if part and part.get('unit_cost') else None


@bp.route('/counts/<int:rid>/save/', methods=['POST'])
@login_required
def count_save(rid):
    req = _get_request_or_404(rid)
    _require_active_counter(req)

    payload = [{'storage_location': e['storage_location'],
                'counted_qty': _parse_qty(request.form.get(f'qty__{e["storage_location"]}'))}
               for e in db.get_entries(rid)]
    notes = (request.form.get('count_notes') or '').strip() or None

    meta = None
    meta_raw = (request.form.get('meta_json') or '').strip()
    if meta_raw:
        try:
            meta = json.loads(meta_raw)
        except ValueError:
            meta = None
        if not isinstance(meta, dict):
            meta = None
    db.save_count(rid, payload, notes, json.dumps(meta) if meta is not None else None)

    action = request.form.get('action') or 'save'
    if action == 'finish':
        if not db.finish_request(rid, _uid()):
            abort(409, 'This count is not in progress any more.')
        if meta is None:
            try:
                meta = json.loads(req.get('progress_meta') or '{}')
            except ValueError:
                meta = {}
        try:
            emails.send_count_finished(rid, meta)
            flash(f'Count #{rid} finished. Reply email sent.', 'success')
        except Exception as exc:
            log.exception('count-finished email failed')
            flash(f'Count #{rid} finished, but email failed: {exc}', 'error')
        return redirect(url_for('cc.dashboard'))

    if action == 'add_location':
        sl = (request.form.get('add_storage_location') or '').strip().upper()
        if not sl:
            flash('Pick a Storage Location to add.', 'error')
        elif not SL_RE.match(sl):
            flash('Invalid Storage Location code.', 'error')
        elif db.add_manual_entry(rid, sl, 'PT01',
                                 unit_value=_manual_unit_value(req['part_number']),
                                 currency=_current_currency(rid)):
            flash(f'Added location {sl}.', 'success')
        else:
            flash(f'Location {sl} is already in this count.', 'error')
        return redirect(url_for('cc.count_screen', rid=rid))

    if action == 'delete_location':
        sl = (request.form.get('delete_storage_location') or '').strip()
        if sl and db.delete_entry(rid, sl):
            flash(f'Removed manual location {sl}.', 'success')
        elif sl:
            flash(f'Cannot remove {sl}: only manual locations can be deleted.', 'error')
        return redirect(url_for('cc.count_screen', rid=rid))

    flash('Progress saved.', 'success')
    return redirect(url_for('cc.count_screen', rid=rid))


def _routes_for_tops(tops):
    part_to_models, _ = usage.get_part_to_models()
    model_to_route, _ = usage.get_route_mapping()
    out = {}
    for top in tops:
        models = part_to_models.get(top.upper(), [])
        routes = []
        for m in models:
            rt = model_to_route.get(m.upper())
            if rt and rt not in routes:
                routes.append(rt)
        out[top] = (models, routes)
    return out


def _all_routes(result):
    tops = [g_['top'] for g_ in result.get('groups') or []]
    routes = set()
    for _models, rts in _routes_for_tops(tops).values():
        routes.update(rts)
    return sorted(routes)


def _precompute_explosion_async(part_number):
    def worker():
        try:
            result = bom.explode_part(part_number)
            if result.get('error'):
                log.warning('explosion precompute for %s failed: %s',
                            part_number, result['error'])
                return
            db.set_part_explosion(part_number, result)
            db.set_part_routes(part_number, _all_routes(result))
        except Exception:
            log.exception('explosion precompute for %s failed', part_number)
    Thread(target=worker, daemon=True).start()


@bp.route('/counts/<int:rid>/explosion/')
@login_required
def count_explosion(rid):
    req = _get_request_or_404(rid)
    refs = [r.strip().upper() for r in request.args.getlist('ref') if r.strip()]
    ref_set = set(refs)
    cached = None
    if request.args.get('refresh') != '1':
        cached, _ = db.get_part_explosion(req['part_number'])
    if cached is not None:
        result = cached
    else:
        result = bom.explode_part(req['part_number'])
        if not result.get('error'):
            db.set_part_explosion(req['part_number'], result)

    tops = [g_['top'] for g_ in result.get('groups') or []]
    routes_by_top = _routes_for_tops(tops)
    groups = []
    for g_ in result.get('groups') or []:
        models, routes = routes_by_top[g_['top']]
        if ref_set and not any(m.upper() in ref_set for m in models):
            continue
        groups.append({
            'top': g_['top'],
            'models': models,
            'routes': routes,
            'paths': g_.get('paths', []),
            'merged': g_.get('merged') or bom._merge_intermediates(g_.get('paths', [])),
        })

    by_route = {}
    for grp in groups:
        for rt in grp['routes'] or [NO_ROUTE]:
            by_route.setdefault(rt, []).append(grp)
    real_routes = sorted(k for k in by_route if k != NO_ROUTE)
    route_order = real_routes + ([NO_ROUTE] if NO_ROUTE in by_route else [])
    route_groups = [{'route': rt, 'tops': by_route[rt]} for rt in route_order]

    if not ref_set and not result.get('error'):
        db.set_part_routes(req['part_number'], real_routes)

    return render_template(
        'explosion_fragment.html',
        route_groups=route_groups,
        all_routes=real_routes,
        error=result.get('error'),
        active_refs=refs,
    )


# =====================================================================
# All requests
# =====================================================================

@bp.route('/requests/')
@login_required
def requests_all():
    status_filter = request.args.get('status') or ''
    if status_filter == 'reviewed':
        return render_template('requests_all.html', view='reviewed', status_filter='reviewed',
                               rows=db.list_requests(status='reviewed', with_totals=True))
    if status_filter == 'cancelled':
        return render_template('requests_all.html', view='cancelled', status_filter='cancelled',
                               rows=db.list_requests(status='cancelled'))
    return render_template(
        'requests_all.html', view='kanban', status_filter='',
        pending=db.list_requests(status='pending'),
        in_progress=db.list_requests(status='in_progress'),
        done=db.list_requests(status='done', with_totals=True),
    )


# =====================================================================
# Lookup
# =====================================================================

@bp.route('/lookup/')
@login_required
def lookup():
    material = (request.args.get('material') or '').strip()
    return render_template(
        'lookup.html',
        material=material,
        rows=db.lookup_material(material) if material else [],
        part=db.get_part_info(material) if material else None,
        searched=bool(material),
    )


# =====================================================================
# Admin — user management
# =====================================================================

@bp.route('/admin/users/')
@role_required('admin')
def users_list():
    return render_template('users_list.html', users=db.list_users())


@bp.route('/admin/users/new/', methods=['GET', 'POST'])
@role_required('admin')
def users_new():
    form = request.form if request.method == 'POST' else {}
    if request.method == 'POST':
        username = (form.get('username') or '').strip()
        full_name = (form.get('full_name') or '').strip()
        email = (form.get('email') or '').strip()
        password = form.get('password') or ''
        role = form.get('role') or ''
        error = None
        if not all([username, full_name, email, password, role]):
            error = 'All fields are required.'
        elif not USERNAME_RE.match(username):
            error = 'Username may only contain letters, digits, ".", "_" and "-".'
        elif not EMAIL_RE.match(email):
            error = 'Invalid email address.'
        elif role not in db.ROLES:
            error = 'Invalid role.'
        elif len(password) < auth.MIN_PASSWORD_LENGTH:
            error = f'Password must be at least {auth.MIN_PASSWORD_LENGTH} characters.'
        elif db.get_user_by_username(username):
            error = f'User "{username}" already exists.'
        if error:
            flash(error, 'error')
        else:
            try:
                db.create_user(username, full_name, email, role, auth.hash_password(password))
            except db.IntegrityError:
                flash(f'User "{username}" already exists.', 'error')
            else:
                flash(f'User "{username}" created.', 'success')
                return redirect(url_for('cc.users_list'))
    return render_template('user_new.html', form=form)


@bp.route('/admin/users/<int:uid>/toggle/', methods=['POST'])
@role_required('admin')
def users_toggle(uid):
    user = db.get_user_by_id(uid) or abort(404)
    if uid == _uid():
        flash('You cannot deactivate your own account.', 'error')
    else:
        new_active = not user['active']
        db.set_user_active(uid, new_active)
        flash(f'User "{user["username"]}" {"activated" if new_active else "deactivated"}.',
              'success')
    return redirect(url_for('cc.users_list'))


@bp.route('/admin/users/<int:uid>/delete/', methods=['POST'])
@role_required('admin')
def users_delete(uid):
    user = db.get_user_by_id(uid) or abort(404)
    if uid == _uid():
        flash('You cannot delete your own account.', 'error')
        return redirect(url_for('cc.users_list'))
    ok, err = db.delete_user(uid)
    if ok:
        flash(f'User "{user["username"]}" deleted.', 'success')
    else:
        flash(err, 'error')
    return redirect(url_for('cc.users_list'))


@bp.route('/admin/users/<int:uid>/reset/', methods=['POST'])
@role_required('admin')
def users_reset(uid):
    user = db.get_user_by_id(uid) or abort(404)
    new_pw = request.form.get('new_password') or ''
    if len(new_pw) < auth.MIN_PASSWORD_LENGTH:
        flash(f'Password must be at least {auth.MIN_PASSWORD_LENGTH} characters.', 'error')
    else:
        db.set_user_password(uid, auth.hash_password(new_pw))
        flash(f'Password reset for "{user["username"]}".', 'success')
    return redirect(url_for('cc.users_list'))


# =====================================================================
# POU mapping
# =====================================================================

@bp.route('/pou/map/', methods=['POST'])
@role_required('cycle_counter', 'admin')
def pou_map():
    pou_id = (request.form.get('pou_id') or '').strip()
    sl = (request.form.get('storage_location') or '').strip().upper()
    rid_raw = request.form.get('rid') or ''
    if not pou_id or sl not in POU_STORAGE_LOCATIONS:
        flash('POU and storage location are required.', 'error')
    else:
        db.upsert_pou_mapping(pou_id, sl)
        flash(f'POU {pou_id} mapped to {sl}.', 'success')
    if rid_raw.isdigit():
        return redirect(url_for('cc.count_screen', rid=int(rid_raw)))
    return redirect(url_for('cc.dashboard'))


# =====================================================================
# Errors
# =====================================================================

def register_error_handlers(app):
    def render_error(code, message):
        if _wants_json() or request.path.startswith('/api/'):
            return jsonify({'error': message}), code
        return render_template('error.html', code=code, message=message), code

    @app.errorhandler(CSRFError)
    def _csrf(e):
        return render_error(400, 'Your session expired or the form is stale. '
                                 'Go back, reload the page and try again.')

    @app.errorhandler(HTTPException)
    def _http(e):
        messages = {
            403: 'You do not have permission to do that.',
            404: 'Page not found.',
            405: 'That action is not allowed this way.',
        }
        desc = e.description if e.code == 409 else messages.get(e.code, e.description)
        return render_error(e.code, desc)

    @app.errorhandler(db.DatabaseUnavailable)
    def _db_down(e):
        log.error('database unavailable: %s', e)
        return render_error(503, 'The database is not reachable right now. Try again shortly.')

    @app.errorhandler(Exception)
    def _unexpected(e):
        log.exception('unhandled error')
        return render_error(500, 'Unexpected error. The details were written to the log.')
