import glob
import os
import threading

import openpyxl

MYSQL_CONFIG = dict(
    host='10.136.186.101', port=3306,
    user='select', password='select',
    database='vdb',
    connect_timeout=8, read_timeout=15,
)

REF_CODES_DIR = r'W:\sharedir\MP&L\Inventory\Utils\REF CODES'
REF_CODES_PATTERN = 'REF CODES-CIMPLE_*'

USAGE_SQL = """
SELECT l.name AS line_name,
       m.name AS machine_name,
       rmf.feed,
       rmf.bank,
       rmf.track,
       GROUP_CONCAT(DISTINCT rmpm.model ORDER BY rmpm.model SEPARATOR ', ') AS models,
       MAX(rmf.dateonupdate) AS last_used
FROM registermachinefeed rmf
JOIN partnumber pn ON pn.id = rmf.partnumber_id
JOIN machineprogram mp ON mp.id = rmf.machineprogram_id
JOIN machine m ON m.id = mp.machine_id
JOIN line l ON l.id = m.line_id
JOIN registermachineprogrammodel rmpm ON rmpm.machineprogram_id = mp.id
WHERE pn.partnumber = %s
  AND rmf.active = 1
  AND rmf.dateonupdate >= DATE_SUB(CURDATE(), INTERVAL 90 DAY)
GROUP BY l.name, m.name, rmf.bank, rmf.feed, rmf.track
ORDER BY line_name, last_used DESC, m.name, rmf.bank, rmf.feed, rmf.track
"""

_route_cache = {'mtime': None, 'path': None, 'data': None}
_route_lock = threading.Lock()


def _latest_ref_codes_file():
    pattern = os.path.join(REF_CODES_DIR, REF_CODES_PATTERN)
    files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    return files[0] if files else None


def _load_ref_codes(path):
    model_to_route = {}
    part_to_models = {}
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.active
        first = True
        for row in ws.iter_rows(min_col=1, max_col=4, values_only=True):
            if first:
                first = False
                continue
            model, _desc, pn, route = row
            if model is None:
                continue
            mkey = str(model).strip().upper()
            if not mkey:
                continue
            if route is not None:
                rval = str(route).strip()
                if rval and mkey not in model_to_route:
                    model_to_route[mkey] = rval
            if pn is not None:
                pkey = str(pn).strip().upper()
                if pkey:
                    bucket = part_to_models.setdefault(pkey, [])
                    if mkey not in bucket:
                        bucket.append(mkey)
    finally:
        wb.close()
    return {'model_to_route': model_to_route, 'part_to_models': part_to_models}


def _get_ref_data():
    with _route_lock:
        path = _latest_ref_codes_file()
        if not path:
            return None, None
        mtime = os.path.getmtime(path)
        if (_route_cache['path'] != path or _route_cache['mtime'] != mtime):
            _route_cache['data'] = _load_ref_codes(path)
            _route_cache['path'] = path
            _route_cache['mtime'] = mtime
        return _route_cache['data'], path


def get_route_mapping():
    data, path = _get_ref_data()
    return (data['model_to_route'] if data else {}), path


def get_part_to_models():
    data, path = _get_ref_data()
    return (data['part_to_models'] if data else {}), path


def fetch_usage_rows(part_number_no_dash):
    import pymysql  # lazy: keeps the whole site from breaking if pymysql isn't
                     # installed — get_machine_usage() already catches this and
                     # degrades gracefully (see its try/except below).
    con = pymysql.connect(**MYSQL_CONFIG)
    try:
        cur = con.cursor()
        cur.execute(USAGE_SQL, (part_number_no_dash,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        con.close()


def get_machine_usage(part_number):
    queried = (part_number or '').replace('-', '')
    out = {'rows': [], 'queried_pn': queried, 'route_file': None, 'error': None}
    if not queried:
        return out
    try:
        raw_rows = fetch_usage_rows(queried)
    except Exception as e:
        out['error'] = f'MySQL lookup failed: {type(e).__name__}: {e}'
        return out

    mapping, path = get_route_mapping()
    out['route_file'] = os.path.basename(path) if path else None

    for r in raw_rows:
        models_str = r.get('models') or ''
        models = [m.strip() for m in models_str.split(',') if m.strip()]
        routes = []
        for m in models:
            rt = mapping.get(m.upper())
            if rt and rt not in routes:
                routes.append(rt)
        out['rows'].append({
            'line_name': r.get('line_name'),
            'machine_name': r.get('machine_name'),
            'feed': r.get('feed'),
            'bank': r.get('bank'),
            'track': r.get('track'),
            'models': models,
            'routes': routes,
            'last_used': r.get('last_used'),
        })
    return out
