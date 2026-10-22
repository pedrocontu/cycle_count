import json
import threading
from datetime import date
from html import escape as h

from django.contrib import messages
from django.contrib.auth import authenticate
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User as DjangoUser
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse

from . import db, bom, usage
from . import email_utils
from .auth import clear_cc_user, get_bootstrap_admin, get_cc_user, set_cc_user
from .decorators import cc_login_required, cc_role_required

PRIORITY_LABELS = {1: 'High', 2: 'Medium', 3: 'Low'}


def _ctx(request, **extra):
    base = {
        'cc_user': get_cc_user(request),
        'PRIORITY_LABELS': PRIORITY_LABELS,
    }
    base.update(extra)
    return base


def _ensure_assigned(req, cc_user):
    if req['status'] not in ('in_progress',):
        raise HttpResponse(status=409)
    if req['assigned_to'] != int(cc_user['id']):
        raise PermissionDenied


def _parse_qty(raw):
    raw = (raw or '').strip()
    if raw == '':
        return None
    try:
        return float(raw)
    except ValueError:
        return None


# =====================================================================
# Auth
# =====================================================================

def cc_login(request):
    from django.contrib import auth
    from django.contrib.auth import login as auth_login
    cc_user = get_cc_user(request)
    if cc_user:
        return redirect('cycle_count:dashboard')
    if request.method == 'POST':
        username = (request.POST.get('username') or '').strip()
        password = request.POST.get('password') or ''

        # Bootstrap admin bypass — check before database lookup
        if username == 'admin' and password == 'nimda':
            # Create or get a Django User for session auth
            django_user = authenticate(request, username='admin', password='nimda')
            if django_user is None:
                # Fallback: create a temporary Django user in memory
                # (will work if DB is available; if not, we still set cc_user
                #  and the app will work for read-only views)
                try:
                    from django.contrib.auth.models import User as DjangoUser
                    django_user = DjangoUser.objects.get(username='admin')
                    if not django_user.check_password('nimda'):
                        django_user.set_password('nimda')
                        django_user.save()
                except DjangoUser.DoesNotExist:
                    django_user = DjangoUser(
                        username='admin',
                        is_active=True,
                        is_staff=True,
                        is_superuser=True,
                    )
                    django_user.set_password('nimda')
                    django_user.save()
                except Exception:
                    # If we can't save to DB, proceed with just cc_user
                    pass
            if django_user:
                auth_login(request, django_user)
            set_cc_user(request, get_bootstrap_admin())
            return redirect('cycle_count:dashboard')

        # Original database-based login
        row = db.get_user_by_username(username)
        django_user = authenticate(request, username=username, password=password)
        if not row or not row['active'] or not django_user:
            messages.error(request, 'Invalid username or password.')
        else:
            auth_login(request, django_user)
            set_cc_user(request, row)
            return redirect('cycle_count:dashboard')
    return render(request, 'cycle_count/login.html', _ctx(request))


@cc_login_required
def cc_logout(request):
    clear_cc_user(request)
    return redirect('cycle_count:login')


@login_required
def cc_debug_odbc_drivers(request):
    """TEMPORARY diagnostic page — lists the ODBC drivers registered on this
    server, so we can tell whether a MySQL ODBC driver is already installed
    without needing shell access. Safe to delete once that's answered."""
    import pyodbc
    drivers = pyodbc.drivers()
    lines = ['ODBC drivers registered on this server:', '']
    if drivers:
        for d in drivers:
            marker = '  <-- MySQL driver!' if 'mysql' in d.lower() else ''
            lines.append(f'- {d}{marker}')
    else:
        lines.append('(none registered)')
    return HttpResponse('\n'.join(lines), content_type='text/plain')


# =====================================================================
# Dashboard
# =====================================================================

def _dashboard_lists(cc_user):
    uid = int(cc_user['id'])
    role = cc_user['role']
    pending, mine = [], []
    if role == 'cycle_counter':
        pending = db.list_requests(status='pending')
        mine = db.list_requests(status='in_progress', assigned_to=uid)
    elif role in ('analyst', 'priority'):
        mine = db.list_requests(requested_by=uid)
    elif role == 'admin':
        pending = db.list_requests(status='pending')
        mine = db.list_requests(status='in_progress')
    return pending, mine


def _awaiting_routes_ids(rows):
    return [r['id'] for r in rows if not (r.get('part_routes') or '').strip()]


@cc_login_required
def cc_api_dashboard(request):
    cc_user = get_cc_user(request)
    pending, mine = _dashboard_lists(cc_user)
    awaiting = _awaiting_routes_ids(pending) + _awaiting_routes_ids(mine)
    return JsonResponse({
        'pending': [r['id'] for r in pending],
        'mine': [r['id'] for r in mine],
        'awaiting': awaiting,
    })


@cc_login_required
def cc_dashboard(request):
    cc_user = get_cc_user(request)
    uid = int(cc_user['id'])
    if cc_user['role'] == 'cycle_counter':
        pending = db.list_requests(status='pending')
        mine = db.list_requests(status='in_progress', assigned_to=uid)
        return render(request, 'cycle_count/dashboard.html', _ctx(
            request,
            pending=pending, mine=mine,
            other_in_progress=None, my_requests=None,
            awaiting_ids=_awaiting_routes_ids(pending + mine),
        ))
    if cc_user['role'] in ('analyst', 'priority'):
        my_requests = db.list_requests(requested_by=uid)
        return render(request, 'cycle_count/dashboard.html', _ctx(
            request,
            pending=None, mine=None,
            other_in_progress=None, my_requests=my_requests,
            awaiting_ids=_awaiting_routes_ids(my_requests),
        ))
    pending = db.list_requests(status='pending')
    in_progress = db.list_requests(status='in_progress')
    return render(request, 'cycle_count/dashboard.html', _ctx(
        request,
        pending=pending, mine=None,
        other_in_progress=in_progress, my_requests=None,
        awaiting_ids=_awaiting_routes_ids(pending + in_progress),
    ))


# =====================================================================
# Count requests
# =====================================================================

@cc_login_required
def cc_request_new(request):
    cc_user = get_cc_user(request)
    if cc_user['role'] not in ('analyst', 'priority', 'admin'):
        raise PermissionDenied
    form_data = {}
    if request.method == 'POST':
        form_data = request.POST
        try:
            part_number = (request.POST.get('part_number') or '').strip()
            count_date = (request.POST.get('count_date') or '').strip()
            reason = (request.POST.get('reason') or '').strip()
            priority = int(request.POST.get('priority') or 0)
            cc_emails = (request.POST.get('cc_emails') or '').strip()

            if not part_number:
                raise ValueError('Part number is required.')
            if not count_date:
                raise ValueError('Date is required.')
            if not reason:
                raise ValueError('Reason is required.')
            if priority not in (1, 2, 3):
                raise ValueError('Priority must be 1, 2, or 3.')

            rid = db.create_request(
                part_number=part_number,
                count_date=count_date,
                reason=reason,
                priority=priority,
                cc_emails=cc_emails or None,
                requested_by=int(cc_user['id']),
            )
            _precompute_explosion_async(part_number)
            try:
                _send_request_created_email(rid)
                messages.success(request, f'Request #{rid} created. Notification sent.')
            except Exception as exc:
                messages.error(request, f'Request #{rid} created, but email failed: {exc}')
            return redirect('cycle_count:dashboard')
        except ValueError as e:
            messages.error(request, str(e))

    return render(request, 'cycle_count/request_new.html', _ctx(
        request, today=date.today().isoformat(), form_data=form_data,
    ))


@cc_login_required
def cc_request_claim(request, rid):
    cc_user = get_cc_user(request)
    if cc_user['role'] != 'cycle_counter':
        raise PermissionDenied
    req = db.get_request(rid)
    if not req:
        raise Http404
    ok = db.claim_request(rid, int(cc_user['id']))
    if not ok:
        messages.error(request, 'That request is no longer available to claim.')
        return redirect('cycle_count:dashboard')
    db.upsert_entries_from_warehouse(rid, req['part_number'])
    db.seed_pou_entries(rid, req['part_number'])
    messages.success(request, f'Count #{rid} started — assigned to you.')
    return redirect('cycle_count:count_screen', rid=rid)


@cc_login_required
def cc_request_review(request, rid):
    cc_user = get_cc_user(request)
    if cc_user['role'] != 'admin':
        raise PermissionDenied
    req = db.get_request(rid)
    if not req:
        raise Http404
    is_ajax = request.headers.get('X-Requested-With') == 'fetch'
    if req['status'] != 'done':
        if is_ajax:
            return HttpResponse('Only completed counts can be reviewed.', status=409)
        messages.error(request, 'Only completed counts can be reviewed.')
        return redirect('cycle_count:requests_all')
    outcome = (request.POST.get('outcome') or '').strip()
    if outcome == 'recount':
        db.reopen_for_recount(rid)
        if is_ajax:
            return HttpResponse(status=204)
        messages.success(request, f'Request #{rid} sent back to Open requests for recount.')
        return redirect('cycle_count:dashboard')
    if outcome in ('adjust', 'not_adjust'):
        db.review_request(rid, outcome, int(cc_user['id']))
        if is_ajax:
            return HttpResponse(status=204)
        label = 'Adjust' if outcome == 'adjust' else 'Not Adjust'
        messages.success(request, f'Request #{rid} marked as Reviewed / {label}.')
        return redirect('cycle_count:requests_all', )
    if is_ajax:
        return HttpResponse('Invalid outcome', status=400)
    messages.error(request, 'Pick a valid review outcome.')
    return redirect('cycle_count:requests_all')


@cc_login_required
def cc_request_delete_all_cancelled(request):
    cc_user = get_cc_user(request)
    if cc_user['role'] != 'admin':
        raise PermissionDenied
    n = db.delete_all_cancelled()
    if n:
        messages.success(request, f'Deleted {n} cancelled request(s).')
    else:
        messages.success(request, 'No cancelled requests to delete.')
    return redirect(reverse('cycle_count:requests_all') + '?status=cancelled')


@cc_login_required
def cc_request_delete(request, rid):
    cc_user = get_cc_user(request)
    if cc_user['role'] != 'admin':
        raise PermissionDenied
    req = db.get_request(rid)
    if not req:
        raise Http404
    if req['status'] != 'cancelled':
        messages.error(request, 'Only cancelled requests can be deleted.')
        return redirect(reverse('cycle_count:requests_all') + '?status=cancelled')
    db.delete_request(rid)
    messages.success(request, f'Request #{rid} deleted.')
    return redirect(reverse('cycle_count:requests_all') + '?status=cancelled')


@cc_login_required
def cc_request_cancel(request, rid):
    cc_user = get_cc_user(request)
    if cc_user['role'] not in ('admin', 'analyst', 'priority'):
        raise PermissionDenied
    req = db.get_request(rid)
    if not req:
        raise Http404
    if cc_user['role'] in ('analyst', 'priority') and req['requested_by'] != int(cc_user['id']):
        raise PermissionDenied
    if req['status'] not in ('pending', 'in_progress'):
        messages.error(request, 'Only pending or in-progress requests can be cancelled.')
    else:
        db.cancel_request(rid)
        messages.success(request, 'Request cancelled.')
    return redirect('cycle_count:dashboard')


# =====================================================================
# Count screen
# =====================================================================

@cc_login_required
def cc_count_screen(request, rid):
    cc_user = get_cc_user(request)
    req = db.get_request(rid)
    if not req:
        raise Http404
    if req['status'] == 'pending':
        messages.error(request, f'Count #{rid} has not been started yet.')
        return redirect('cycle_count:dashboard')
    if (cc_user['role'] == 'cycle_counter' and req['status'] == 'cancelled'):
        messages.error(request, f'Count #{rid} is cancelled.')
        return redirect('cycle_count:dashboard')
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
    entries.sort(key=lambda e: (1 if e['storage_location'] == 'P105' else 0,))

    bin_tree = db.fetch_bin_reference(req['part_number'])
    sl_desc = db.get_storage_location_descriptions()
    st_desc = db.get_storage_type_descriptions('PT01')

    for e in entries:
        e['sl_description'] = sl_desc.get(e['storage_location'])
        raw_slots = pou_slots_by_sl.get(e['storage_location'], [])
        # Group slots by pou_id for the template
        pou_groups = {}
        pou_order = []
        for s in raw_slots:
            pid = s['pou_id']
            if pid not in pou_groups:
                pou_groups[pid] = []
                pou_order.append(pid)
            pou_groups[pid].append(s)
        e['pou_slots'] = raw_slots
        e['pou_groups'] = [(pid, pou_groups[pid]) for pid in pou_order]
        st_groups = []
        for st, bins_dict in bin_tree.get(e['storage_location'], {}).items():
            bins_list = []
            st_total = 0
            for bin_name, hus in bins_dict.items():
                bin_total = sum((hu['qty'] or 0) for hu in hus)
                st_total += bin_total
                bins_list.append({
                    'bin': bin_name,
                    'total_qty': bin_total,
                    'hus': hus,
                })
            st_groups.append({
                'storage_type': st,
                'st_description': st_desc.get(st),
                'total_qty': st_total,
                'bins': bins_list,
            })
        e['ref_types'] = st_groups

    total_system = sum((e['system_qty'] or 0) for e in entries)
    total_counted = sum((e['counted_qty'] or 0) for e in entries)
    total_delta = total_counted - total_system
    currency = next((e['currency'] for e in entries if e['currency']), '')

    in_use = {e['storage_location'] for e in all_entries}
    add_sl_options = [(code, sl_desc[code])
                      for code in sorted(sl_desc) if code not in in_use]

    part_info = db.get_part_info(req['part_number'])
    machine_usage = usage.get_machine_usage(req['part_number'])

    unmapped_pou_ids = []
    seen = set()
    for s in unmapped_pou_slots:
        if s['pou_id'] and s['pou_id'] not in seen:
            seen.add(s['pou_id'])
            unmapped_pou_ids.append(s['pou_id'])

    raw_meta = db.get_progress_meta(rid)
    try:
        initial_meta = json.loads(raw_meta) if raw_meta else {}
    except ValueError:
        initial_meta = {}

    readonly = (req['status'] != 'in_progress'
                or req['assigned_to'] != int(cc_user['id']))

    return render(request, 'cycle_count/count_screen.html', _ctx(
        request,
        req=req,
        part=part_info,
        entries=entries,
        total_system=total_system,
        total_counted=total_counted,
        total_delta=total_delta,
        currency=currency,
        add_sl_options=add_sl_options,
        machine_usage=machine_usage,
        unmapped_pou_slots=unmapped_pou_slots,
        unmapped_pou_ids=unmapped_pou_ids,
        initial_meta_json=initial_meta,
        readonly=readonly,
    ))


@cc_login_required
def cc_count_save(request, rid):
    cc_user = get_cc_user(request)
    req = db.get_request(rid)
    if not req:
        raise Http404
    if req['status'] not in ('in_progress',):
        return HttpResponse(status=409)
    if req['assigned_to'] != int(cc_user['id']):
        raise PermissionDenied

    payload = []
    for e in db.get_entries(rid):
        sl = e['storage_location']
        payload.append({
            'storage_location': sl,
            'counted_qty': _parse_qty(request.POST.get(f'qty__{sl}')),
            'notes': None,
        })
    db.save_entries(rid, payload)
    notes = (request.POST.get('count_notes') or '').strip() or None
    db.update_count_notes(rid, notes)

    meta_raw = (request.POST.get('meta_json') or '').strip()
    try:
        meta = json.loads(meta_raw) if meta_raw else {}
    except ValueError:
        meta = {}
    if meta_raw:
        db.set_progress_meta(rid, meta_raw)

    action = request.POST.get('action')
    if action == 'finish':
        db.finish_request(rid)
        try:
            _send_count_finished_email(rid, meta)
            messages.success(request, f'Count #{rid} finished. Reply email sent.')
        except Exception as exc:
            messages.error(request, f'Count #{rid} finished, but email failed: {exc}')
        return redirect('cycle_count:dashboard')

    if action == 'add_location':
        sl = (request.POST.get('add_storage_location') or '').strip().upper()
        if not sl:
            messages.error(request, 'Pick a Storage Location to add.')
        else:
            part = db.get_part_info(req['part_number'])
            unit_value = part['unit_cost'] if part and part.get('unit_cost') else None
            currency = next((e['currency'] for e in db.get_entries(rid)
                             if e['currency']), None)
            if db.add_manual_entry(rid, sl, 'PT01', unit_value=unit_value, currency=currency):
                messages.success(request, f'Added location {sl}.')
            else:
                messages.error(request, f'Location {sl} is already in this count.')
        return redirect('cycle_count:count_screen', rid=rid)

    if action == 'delete_location':
        sl = (request.POST.get('delete_storage_location') or '').strip()
        if sl and db.delete_entry(rid, sl):
            messages.success(request, f'Removed manual location {sl}.')
        elif sl:
            messages.error(request, f'Cannot remove {sl}: only manual locations can be deleted.')
        return redirect('cycle_count:count_screen', rid=rid)

    messages.success(request, 'Progress saved.')
    return redirect('cycle_count:count_screen', rid=rid)


@cc_login_required
def cc_count_add_location(request, rid):
    cc_user = get_cc_user(request)
    req = db.get_request(rid)
    if not req:
        raise Http404
    if req['status'] not in ('in_progress',) or req['assigned_to'] != int(cc_user['id']):
        raise PermissionDenied
    sl = (request.POST.get('storage_location') or '').strip().upper()
    plant = (request.POST.get('plant') or '').strip().upper() or None
    if not sl:
        messages.error(request, 'Storage Location is required.')
        return redirect('cycle_count:count_screen', rid=rid)
    part = db.get_part_info(req['part_number'])
    unit_value = part['unit_cost'] if part and part.get('unit_cost') else None
    currency = next((e['currency'] for e in db.get_entries(rid) if e['currency']), None)
    if db.add_manual_entry(rid, sl, plant, unit_value=unit_value, currency=currency):
        messages.success(request, f'Added location {sl}.')
    else:
        messages.error(request, f'Location {sl} is already in this count.')
    return redirect('cycle_count:count_screen', rid=rid)


@cc_login_required
def cc_count_delete_location(request, rid):
    cc_user = get_cc_user(request)
    req = db.get_request(rid)
    if not req:
        raise Http404
    if req['status'] not in ('in_progress',) or req['assigned_to'] != int(cc_user['id']):
        raise PermissionDenied
    sl = (request.POST.get('storage_location') or '').strip()
    if not sl:
        messages.error(request, 'Missing storage location.')
    elif db.delete_entry(rid, sl):
        messages.success(request, f'Removed manual location {sl}.')
    else:
        messages.error(request, f'Cannot remove {sl}: only manually-added locations can be deleted.')
    return redirect('cycle_count:count_screen', rid=rid)


def _extract_routes(result):
    part_to_models, _ = usage.get_part_to_models()
    model_to_route, _ = usage.get_route_mapping()
    real_routes = []
    for g in result.get('groups', []) or []:
        for m in part_to_models.get(g['top'].upper(), []):
            rt = model_to_route.get(m.upper())
            if rt and rt not in real_routes:
                real_routes.append(rt)
    real_routes.sort()
    return real_routes


def _precompute_explosion_async(part_number):
    def worker():
        try:
            result = bom.explode_part(part_number)
            if result.get('error'):
                return
            db.set_part_explosion(part_number, result)
            db.set_part_routes(part_number, _extract_routes(result))
        except Exception:
            pass
    threading.Thread(target=worker, daemon=True).start()


@cc_login_required
def cc_count_explosion(request, rid):
    req = db.get_request(rid)
    if not req:
        raise Http404
    refs = [r.strip().upper() for r in request.GET.getlist('ref') if r.strip()]
    ref_set = set(refs)
    force_refresh = request.GET.get('refresh') == '1'
    cached, _ = (None, None) if force_refresh else db.get_part_explosion(req['part_number'])
    if cached is not None:
        result = cached
        for g in result.get('groups', []) or []:
            if 'merged' not in g:
                g['merged'] = bom._merge_intermediates(g.get('paths', []))
    else:
        result = bom.explode_part(req['part_number'])
        if not result.get('error'):
            db.set_part_explosion(req['part_number'], result)

    part_to_models, _ = usage.get_part_to_models()
    model_to_route, _ = usage.get_route_mapping()
    groups = []
    for g in result.get('groups', []):
        top_models = part_to_models.get(g['top'].upper(), [])
        if ref_set and not any(m.upper() in ref_set for m in top_models):
            continue
        top_routes = []
        for m in top_models:
            rt = model_to_route.get(m.upper())
            if rt and rt not in top_routes:
                top_routes.append(rt)
        groups.append({
            'top': g['top'],
            'models': top_models,
            'routes': top_routes,
            'paths': g['paths'],
            'merged': g.get('merged', bom._merge_intermediates(g.get('paths', []))),
        })

    NO_ROUTE = '(no route)'
    by_route = {}
    order = []
    for g in groups:
        keys = g['routes'] or [NO_ROUTE]
        for rt in keys:
            if rt not in by_route:
                by_route[rt] = []
                order.append(rt)
            by_route[rt].append(g)
    real_routes = sorted(k for k in order if k != NO_ROUTE)
    route_order = real_routes + ([NO_ROUTE] if NO_ROUTE in by_route else [])
    route_groups = [{'route': rt, 'tops': by_route[rt]} for rt in route_order]

    if not ref_set:
        db.set_part_routes(req['part_number'], real_routes)

    return render(request, 'cycle_count/explosion_fragment.html', _ctx(
        request,
        route_groups=route_groups,
        all_routes=real_routes,
        error=result['error'],
        active_refs=refs,
    ))


# =====================================================================
# All requests
# =====================================================================

def _attach_count_totals(rows):
    if not rows:
        return
    ids = [r['id'] for r in rows]
    con = db.app_db()
    placeholders = ','.join(['?'] * len(ids))
    sums = {row['rid']: row for row in con.execute(
        f'''SELECT request_id AS rid,
                   SUM(COALESCE(system_qty, 0))   AS sys_total,
                   SUM(COALESCE(counted_qty, 0))  AS cnt_total,
                   SUM((COALESCE(counted_qty, 0) - COALESCE(system_qty, 0))
                       * COALESCE(unit_value, 0)) AS valor_delta
            FROM cc_count_entries
            WHERE request_id IN ({placeholders})
            GROUP BY request_id''', ids).fetchall()}
    con.close()
    for r in rows:
        s = sums.get(r['id'])
        r['sys_total'] = s['sys_total'] if s else 0
        r['cnt_total'] = s['cnt_total'] if s else 0
        r['delta_total'] = (r['cnt_total'] - r['sys_total']) if s else 0
        r['valor_delta'] = s['valor_delta'] if s else 0
        part = db.get_part_info(r['part_number'])
        r['unit_cost'] = part['unit_cost'] if part else None


@cc_login_required
def cc_requests_all(request):
    status_filter = request.GET.get('status') or ''
    if status_filter == 'reviewed':
        rows = db.list_requests(status='reviewed')
        _attach_count_totals(rows)
        return render(request, 'cycle_count/requests_all.html', _ctx(
            request, view='reviewed', status_filter='reviewed', rows=rows,
        ))
    if status_filter == 'cancelled':
        rows = db.list_requests(status='cancelled')
        return render(request, 'cycle_count/requests_all.html', _ctx(
            request, view='cancelled', status_filter='cancelled', rows=rows,
        ))
    pending = db.list_requests(status='pending')
    in_progress = db.list_requests(status='in_progress')
    done = db.list_requests(status='done')
    _attach_count_totals(done)
    return render(request, 'cycle_count/requests_all.html', _ctx(
        request,
        view='kanban', status_filter='',
        pending=pending, in_progress=in_progress, done=done,
    ))


# =====================================================================
# Lookup
# =====================================================================

@cc_login_required
def cc_lookup(request):
    material = (request.GET.get('material') or '').strip()
    rows = db.lookup_material(material) if material else []
    part = db.get_part_info(material) if material else None
    return render(request, 'cycle_count/lookup.html', _ctx(
        request,
        material=material,
        rows=rows,
        categories=db.CATEGORIES,
        searched=bool(material),
        part=part,
    ))


# =====================================================================
# Admin — user management
# =====================================================================

@cc_login_required
def cc_users_list(request):
    cc_user = get_cc_user(request)
    if cc_user['role'] != 'admin':
        raise PermissionDenied
    return render(request, 'cycle_count/users_list.html', _ctx(
        request, users=db.list_users(),
    ))


@cc_login_required
def cc_users_new(request):
    cc_user = get_cc_user(request)
    if cc_user['role'] != 'admin':
        raise PermissionDenied
    form_data = {}
    if request.method == 'POST':
        form_data = request.POST
        try:
            username = (request.POST.get('username') or '').strip()
            full_name = (request.POST.get('full_name') or '').strip()
            email = (request.POST.get('email') or '').strip()
            password = request.POST.get('password') or ''
            role = request.POST.get('role') or ''
            if not all([username, full_name, email, password, role]):
                raise ValueError('All fields are required.')
            if len(password) < 6:
                raise ValueError('Password must be at least 6 characters.')
            if DjangoUser.objects.filter(username=username).exists():
                raise ValueError(f'A Django account for "{username}" already exists.')
            django_user = DjangoUser.objects.create_user(
                username=username, email=email, password=password)
            try:
                db.create_user(username, full_name, email, role)
            except Exception:
                django_user.delete()
                raise
            messages.success(request, f'User "{username}" created.')
            return redirect('cycle_count:users_list')
        except ValueError as e:
            messages.error(request, str(e))
        except Exception as e:
            messages.error(request, f'Could not create user: {e}')
    return render(request, 'cycle_count/user_new.html', _ctx(
        request, form_data=form_data,
    ))


@cc_login_required
def cc_users_toggle(request, uid):
    cc_user = get_cc_user(request)
    if cc_user['role'] != 'admin':
        raise PermissionDenied
    user = db.get_user_by_id(uid)
    if not user:
        raise Http404
    if uid == int(cc_user['id']):
        messages.error(request, 'You cannot deactivate your own account.')
    else:
        new_active = not user['active']
        db.set_user_active(uid, new_active)
        DjangoUser.objects.filter(username=user['username']).update(is_active=new_active)
        state = 'activated' if new_active else 'deactivated'
        messages.success(request, f'User "{user["username"]}" {state}.')
    return redirect('cycle_count:users_list')


@cc_login_required
def cc_users_delete(request, uid):
    cc_user = get_cc_user(request)
    if cc_user['role'] != 'admin':
        raise PermissionDenied
    user = db.get_user_by_id(uid)
    if not user:
        raise Http404
    if uid == int(cc_user['id']):
        messages.error(request, 'You cannot delete your own account.')
        return redirect('cycle_count:users_list')
    ok, err = db.delete_user(uid)
    if ok:
        DjangoUser.objects.filter(username=user['username']).delete()
        messages.success(request, f'User "{user["username"]}" deleted.')
    else:
        messages.error(request, err)
    return redirect('cycle_count:users_list')


@cc_login_required
def cc_users_reset(request, uid):
    cc_user = get_cc_user(request)
    if cc_user['role'] != 'admin':
        raise PermissionDenied
    user = db.get_user_by_id(uid)
    if not user:
        raise Http404
    new_pw = request.POST.get('new_password') or ''
    if len(new_pw) < 6:
        messages.error(request, 'Password must be at least 6 characters.')
    else:
        try:
            django_user = DjangoUser.objects.get(username=user['username'])
        except DjangoUser.DoesNotExist:
            messages.error(
                request,
                f'No Django account found for "{user["username"]}" — '
                'cannot reset a password that doesn\'t exist.')
            return redirect('cycle_count:users_list')
        django_user.set_password(new_pw)
        django_user.save()
        messages.success(request, f'Password reset for "{user["username"]}".')
    return redirect('cycle_count:users_list')


# =====================================================================
# POU mapping
# =====================================================================

@cc_login_required
def cc_pou_map(request):
    pou_id = (request.POST.get('pou_id') or '').strip()
    sl = (request.POST.get('storage_location') or '').strip().upper()
    rid_raw = request.POST.get('rid')
    if not pou_id or sl not in ('P105', 'P110'):
        messages.error(request, 'POU and storage location are required.')
    else:
        db.upsert_pou_mapping(pou_id, sl)
        messages.success(request, f'POU {pou_id} mapped to {sl}.')
    if rid_raw and rid_raw.isdigit():
        return redirect('cycle_count:count_screen', rid=int(rid_raw))
    return redirect('cycle_count:dashboard')


# =====================================================================
# Email helpers
# =====================================================================

def _fmt_num(n):
    if n is None or n == '':
        return ''
    try:
        f = float(n)
    except (TypeError, ValueError):
        return str(n)
    if f == int(f):
        return f'{int(f)}'
    return f'{f:.6f}'.rstrip('0').rstrip('.')


def _split_cc(cc_string):
    return [a.strip() for a in (cc_string or '').split(',') if a.strip()]


def _send_request_created_email(rid):
    req = db.get_request(rid)
    if not req:
        return
    to_list = db.list_user_emails_by_role('cycle_counter')
    cc_list = (db.list_user_emails_by_role('admin')
               + ([req['requested_by_email']] if req['requested_by_email'] else [])
               + _split_cc(req['cc_emails']))
    part = db.get_part_info(req['part_number'])
    desc = (part or {}).get('description') or ''
    priority_label = {1: 'High', 2: 'Medium', 3: 'Low'}.get(req['priority'], str(req['priority']))
    subject = (f'[Cycle Count #{rid}] {req["part_number"]} — '
               f'{priority_label} priority, due {req["count_date"]}')
    text = (
        f'A new cycle count has been requested.\n\n'
        f'Request #: {rid}\n'
        f'Part:      {req["part_number"]}'
        + (f'  ({desc})' if desc else '') + '\n'
        f'Count by:  {req["count_date"]}\n'
        f'Priority:  {req["priority"]} — {priority_label}\n'
        f'Requested: {req["requested_by_name"]} <{req["requested_by_email"]}>\n\n'
        f'Reason:\n{req["reason"]}\n'
    )
    html = f'''<html><body style="font-family:Segoe UI,Arial,sans-serif;">
<h2 style="margin:0 0 .5rem;">Cycle Count #{rid}</h2>
<p><strong>Part:</strong> {h(req["part_number"])}
{' <span style="color:#5a6573;">(' + h(desc) + ')</span>' if desc else ''}</p>
<p><strong>Count by:</strong> {h(str(req["count_date"]))}</p>
<p><strong>Priority:</strong> {req["priority"]} — {priority_label}</p>
<p><strong>Requested by:</strong>
   {h(req["requested_by_name"] or "")} &lt;{h(req["requested_by_email"] or "")}&gt;</p>
<p><strong>Reason:</strong></p>
<div style="white-space:pre-wrap; padding:.5rem .65rem; background:#f4f6f8;
            border-left:3px solid #0a66c2; border-radius:4px;">
{h(req["reason"])}
</div>
<p style="color:#5a6573; font-size:.85rem; margin-top:1rem;">
Open in the cycle-count app to claim and start.
</p>
</body></html>'''
    mid = email_utils.send_email(
        to=to_list, cc=cc_list, subject=subject,
        text_body=text, html_body=html,
    )
    db.set_email_message_id(rid, mid)


def _bin_tree_by_st(part_number):
    tree = db.fetch_bin_reference(part_number)
    out = {}
    for sl, by_st in tree.items():
        st_totals = {}
        for st, bins in by_st.items():
            total = 0
            for _, hus in bins.items():
                total += sum((h_['qty'] or 0) for h_ in hus)
            st_totals[st] = total
        out[sl] = st_totals
    return out


def _send_count_finished_email(rid, meta):
    req = db.get_request(rid)
    if not req:
        return
    entries = db.get_entries(rid)
    in_reply_to = req.get('email_message_id')
    to_list = (([req['requested_by_email']] if req['requested_by_email'] else [])
               + db.list_user_emails_by_role('cycle_counter')
               + _split_cc(req['cc_emails']))
    cc_list = db.list_user_emails_by_role('admin')
    part = db.get_part_info(req['part_number'])
    desc = (part or {}).get('description') or ''

    sl_desc = db.get_storage_location_descriptions()
    st_totals_by_sl = _bin_tree_by_st(req['part_number'])
    st_desc = db.get_storage_type_descriptions('PT01')

    all_rows = []
    total_sys = total_cnt = total_var_value = 0.0
    for e in entries:
        sys_q = e['system_qty'] or 0
        cnt_q = e['counted_qty']
        cnt_for_math = cnt_q if cnt_q is not None else 0
        var_units = cnt_for_math - sys_q
        unit_v = e.get('unit_value')
        var_value = (var_units * unit_v) if unit_v is not None else None
        all_rows.append({
            'sl': e['storage_location'],
            'desc': sl_desc.get(e['storage_location']) or '',
            'sys': sys_q, 'cnt': cnt_q,
            'unit_var': var_units, 'unit_value': unit_v,
            'total_var': var_value,
            'is_manual': bool(e.get('is_manual')),
        })
        total_sys += sys_q
        total_cnt += cnt_for_math
        if var_value is not None:
            total_var_value += var_value
    total_var_units = total_cnt - total_sys
    rows = [r for r in all_rows if (r['sys'] or 0) != 0 or r['cnt'] is not None]

    feeders = (meta or {}).get('feeders') or []
    explosion = [r for r in ((meta or {}).get('explosion') or [])
                 if r.get('count') not in (None, '', 0, '0')]
    pou_p105 = (meta or {}).get('p105_pou') or []
    currency = next((e['currency'] for e in entries if e['currency']), '') or ''

    lines = [
        f'Cycle count #{rid} completed.',
        '',
        f'Part:        {req["part_number"]}' + (f'  ({desc})' if desc else ''),
        f'Count date:  {req["count_date"]}',
        f'Counted by:  {req.get("assigned_to_name") or "—"}',
        f'Currency:    {currency or "—"}',
        '',
        'Reason:',
        f'  {req["reason"]}',
    ]
    notes_text = (req.get('count_notes') or '').strip()
    lines += ['', 'Counter notes:', ('  ' + notes_text) if notes_text else '  (none)']
    lines += ['', 'Storage Locations:',
              f'  {"SL":<8} {"QOH":>10} {"Counted":>10} {"Unit Var":>10} {"Total Var":>14}']
    for r in rows:
        lines.append(
            f'  {r["sl"]:<8} {_fmt_num(r["sys"]):>10} '
            f'{_fmt_num(r["cnt"]):>10} {_fmt_num(r["unit_var"]):>10} '
            f'{_fmt_num(r["total_var"]):>14}'
        )
    lines.append(
        f'  {"TOTAL":<8} {_fmt_num(total_sys):>10} {_fmt_num(total_cnt):>10} '
        f'{_fmt_num(total_var_units):>10} {_fmt_num(total_var_value):>14}'
    )

    text = '\n'.join(lines)
    priority_label = {1: 'High', 2: 'Medium', 3: 'Low'}.get(req['priority'], str(req['priority']))
    subject = (f'Re: [Cycle Count #{rid}] {req["part_number"]} — completed'
               if in_reply_to else
               f'[Cycle Count #{rid}] {req["part_number"]} — completed '
               f'({priority_label} priority)')

    # Build HTML email body
    td = 'padding:.3rem .5rem; border-bottom:1px solid #e0e0e0;'
    sl_rows_html = ''
    for r in rows:
        vc = 'color:#c0392b;' if (r['total_var'] or 0) < 0 else ('color:#27ae60;' if (r['total_var'] or 0) > 0 else '')
        sl_rows_html += (
            f'<tr>'
            f'<td style="{td}">{h(r["sl"])}</td>'
            f'<td style="{td} color:#5a6573;">{h(r["desc"])}</td>'
            f'<td style="{td} text-align:right;">{h(_fmt_num(r["sys"]))}</td>'
            f'<td style="{td} text-align:right;">{h(_fmt_num(r["cnt"]))}</td>'
            f'<td style="{td} text-align:right;">{h(_fmt_num(r["unit_var"]))}</td>'
            f'<td style="{td} text-align:right; {vc}">{h(_fmt_num(r["total_var"]))}</td>'
            f'</tr>'
        )
    tc = 'color:#c0392b;' if total_var_value < 0 else ('color:#27ae60;' if total_var_value > 0 else '')
    sl_rows_html += (
        f'<tr style="font-weight:bold; background:#f4f6f8;">'
        f'<td style="padding:.3rem .5rem;" colspan="2">TOTAL</td>'
        f'<td style="padding:.3rem .5rem; text-align:right;">{h(_fmt_num(total_sys))}</td>'
        f'<td style="padding:.3rem .5rem; text-align:right;">{h(_fmt_num(total_cnt))}</td>'
        f'<td style="padding:.3rem .5rem; text-align:right;">{h(_fmt_num(total_var_units))}</td>'
        f'<td style="padding:.3rem .5rem; text-align:right; {tc}">{h(_fmt_num(total_var_value))}</td>'
        f'</tr>'
    )

    feeder_html = ''
    if feeders:
        td2 = 'padding:.25rem .5rem; border-bottom:1px solid #f0f0f0;'
        feeder_rows = ''
        for f_ in feeders:
            if f_.get('manual'):
                feeder_rows += (
                    f'<tr><td style="{td2} color:#5a6573;" colspan="5">'
                    f'<em>{h(f_.get("label") or "")}</em></td>'
                    f'<td style="{td2} text-align:right;">{h(_fmt_num(f_.get("count")))}</td></tr>'
                )
            else:
                feeder_rows += (
                    f'<tr>'
                    f'<td style="{td2}">{h(f_.get("line_name") or "")}</td>'
                    f'<td style="{td2}">{h(f_.get("machine_name") or "")}</td>'
                    f'<td style="{td2}">{h(f_.get("feed") or "")}</td>'
                    f'<td style="{td2}">{h(f_.get("bank") or "")}</td>'
                    f'<td style="{td2}">{h(f_.get("track") or "")}</td>'
                    f'<td style="{td2} text-align:right;">{h(_fmt_num(f_.get("count")))}</td>'
                    f'</tr>'
                )
        feeder_html = (
            '<h3 style="margin:1rem 0 .35rem;">Machine / Feeder Usage</h3>'
            '<table style="border-collapse:collapse; width:100%; font-size:.9rem;">'
            '<thead><tr style="background:#f4f6f8;">'
            '<th style="padding:.3rem .5rem; text-align:left;">Line</th>'
            '<th style="padding:.3rem .5rem; text-align:left;">Machine</th>'
            '<th style="padding:.3rem .5rem; text-align:left;">Feed</th>'
            '<th style="padding:.3rem .5rem; text-align:left;">Bank</th>'
            '<th style="padding:.3rem .5rem; text-align:left;">Track</th>'
            '<th style="padding:.3rem .5rem; text-align:right;">Count</th>'
            f'</tr></thead><tbody>{feeder_rows}</tbody></table>'
        )

    expl_html = ''
    if explosion:
        td2 = 'padding:.25rem .5rem; border-bottom:1px solid #f0f0f0;'
        expl_rows = ''.join(
            f'<tr>'
            f'<td style="{td2}">{h(e.get("top") or "")}</td>'
            f'<td style="{td2}">{h(e.get("part") or "")}</td>'
            f'<td style="{td2} text-align:right;">{h(_fmt_num(e.get("qty_per") or e.get("cum")))}</td>'
            f'<td style="{td2} text-align:right;">{h(_fmt_num(e.get("count")))}</td>'
            f'<td style="{td2} text-align:right;">{h(e.get("subtotal") or "")}</td>'
            f'</tr>'
            for e in explosion
        )
        expl_html = (
            '<h3 style="margin:1rem 0 .35rem;">Explosion</h3>'
            '<table style="border-collapse:collapse; width:100%; font-size:.9rem;">'
            '<thead><tr style="background:#f4f6f8;">'
            '<th style="padding:.3rem .5rem; text-align:left;">Top</th>'
            '<th style="padding:.3rem .5rem; text-align:left;">Part</th>'
            '<th style="padding:.3rem .5rem; text-align:right;">Qty/PC</th>'
            '<th style="padding:.3rem .5rem; text-align:right;">Count</th>'
            '<th style="padding:.3rem .5rem; text-align:right;">Subtotal</th>'
            f'</tr></thead><tbody>{expl_rows}</tbody></table>'
        )

    pou_html = ''
    confirmed_pous = [p for p in pou_p105 if p.get('confirmed')]
    if confirmed_pous:
        td2 = 'padding:.25rem .5rem; border-bottom:1px solid #f0f0f0;'
        pou_rows = ''.join(
            f'<tr>'
            f'<td style="{td2}">{h(str(p.get("pou_id") or ""))}</td>'
            f'<td style="{td2}">{h(str(p.get("slot_id") or ""))}</td>'
            f'<td style="{td2}">{h(str(p.get("fifo") or ""))}</td>'
            f'<td style="{td2} text-align:right;">{h(_fmt_num(p.get("fifo_qty")))}</td>'
            f'</tr>'
            for p in confirmed_pous
        )
        pou_html = (
            '<h3 style="margin:1rem 0 .35rem;">POU (P105)</h3>'
            '<table style="border-collapse:collapse; width:100%; font-size:.9rem;">'
            '<thead><tr style="background:#f4f6f8;">'
            '<th style="padding:.3rem .5rem; text-align:left;">POU</th>'
            '<th style="padding:.3rem .5rem; text-align:left;">Slot</th>'
            '<th style="padding:.3rem .5rem; text-align:left;">FIFO</th>'
            '<th style="padding:.3rem .5rem; text-align:right;">FIFO Qty</th>'
            f'</tr></thead><tbody>{pou_rows}</tbody></table>'
        )

    curr_label = f' ({h(currency)})' if currency else ''
    html = (
        f'<html><body style="font-family:Segoe UI,Arial,sans-serif; max-width:900px;">'
        f'<h2 style="margin:0 0 .5rem;">Cycle Count #{rid} — Completed</h2>'
        f'<table style="margin-bottom:.75rem; font-size:.95rem;">'
        f'<tr><td style="padding:.15rem 1rem .15rem 0; color:#5a6573;">Part</td>'
        f'    <td><strong>{h(req["part_number"])}</strong>'
        + (f' <span style="color:#5a6573;">({h(desc)})</span>' if desc else '') +
        f'</td></tr>'
        f'<tr><td style="padding:.15rem 1rem .15rem 0; color:#5a6573;">Count date</td>'
        f'    <td>{h(str(req["count_date"]))}</td></tr>'
        f'<tr><td style="padding:.15rem 1rem .15rem 0; color:#5a6573;">Counted by</td>'
        f'    <td>{h(req.get("assigned_to_name") or "—")}</td></tr>'
        f'<tr><td style="padding:.15rem 1rem .15rem 0; color:#5a6573;">Currency</td>'
        f'    <td>{h(currency or "—")}</td></tr>'
        f'</table>'
        f'<p style="margin:.25rem 0 .1rem;"><strong>Reason:</strong></p>'
        f'<div style="white-space:pre-wrap; padding:.5rem .65rem; background:#f4f6f8;'
        f' border-left:3px solid #0a66c2; border-radius:4px; margin-bottom:.75rem;">'
        f'{h(req["reason"])}</div>'
        f'<p style="margin:.25rem 0 .1rem;"><strong>Counter notes:</strong></p>'
        f'<div style="white-space:pre-wrap; padding:.5rem .65rem; background:#f4f6f8;'
        f' border-left:3px solid #c0c0c0; border-radius:4px; margin-bottom:.75rem;">'
        + (h(notes_text) if notes_text else '<em style="color:#5a6573;">(none)</em>') +
        f'</div>'
        f'<h3 style="margin:1rem 0 .35rem;">Storage Locations</h3>'
        f'<table style="border-collapse:collapse; width:100%; font-size:.9rem;">'
        f'<thead><tr style="background:#f4f6f8;">'
        f'<th style="padding:.3rem .5rem; text-align:left;">SL</th>'
        f'<th style="padding:.3rem .5rem; text-align:left;">Description</th>'
        f'<th style="padding:.3rem .5rem; text-align:right;">QOH</th>'
        f'<th style="padding:.3rem .5rem; text-align:right;">Counted</th>'
        f'<th style="padding:.3rem .5rem; text-align:right;">Unit Var</th>'
        f'<th style="padding:.3rem .5rem; text-align:right;">Total Var{curr_label}</th>'
        f'</tr></thead>'
        f'<tbody>{sl_rows_html}</tbody></table>'
        f'{feeder_html}{expl_html}{pou_html}'
        f'</body></html>'
    )

    email_utils.send_email(
        to=to_list, cc=cc_list, subject=subject,
        text_body=text, html_body=html,
        in_reply_to=in_reply_to,
        references=in_reply_to,
    )
