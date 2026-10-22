"""Raw SQL access layer for the cc_* tables in visteon_test (SQL Server).

Uses pyodbc directly — no Django ORM — so the tables stay unmanaged and
no migrations touch them.  The connection string is read from settings.
"""
import json as _json
import os
from decimal import Decimal

import pyodbc
from django.conf import settings

# Password verification is delegated to django.contrib.auth (see views.cc_login /
# cc_users_new / cc_users_reset) — cc_users.password_hash is kept only because the
# column is NOT NULL in SQL Server; it's never read back for verification.
_UNUSED_PASSWORD_PLACEHOLDER = 'managed-by-django'

APP_SQL_CONN = getattr(
    settings, 'CC_SQL_CONN',
    os.environ.get(
        'CC_SQL_CONN',
        (r'DRIVER={ODBC Driver 17 for SQL Server};'
         r'SERVER=10.216.180.197\VWMT;DATABASE=visteon_test;'
         r'UID=vwmt;PWD=vwmt4palmela;'
         r'TrustServerCertificate=yes;Connection Timeout=10;'),
    ),
)


# ---------- pyodbc → dict-row facade ----------

class _DictRow:
    __slots__ = ('_cols', '_data')

    def __init__(self, cols, data):
        self._cols = cols
        self._data = tuple(
            float(v) if isinstance(v, Decimal) else v for v in data
        )

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._data[key]
        try:
            return self._data[self._cols.index(key)]
        except ValueError:
            raise KeyError(key)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def keys(self):
        return list(self._cols)

    def __iter__(self):
        return iter(self._cols)

    def __contains__(self, key):
        return key in self._cols

    def __repr__(self):
        return f'DictRow({dict(zip(self._cols, self._data))!r})'


def _to_dict(cols, row):
    return {col: (float(v) if isinstance(v, Decimal) else v)
            for col, v in zip(cols, row)}


class _Cursor:
    def __init__(self, cur):
        self._cur = cur
        self._cols = ([c[0] for c in cur.description]
                      if cur.description else None)

    def fetchone(self):
        r = self._cur.fetchone()
        return _to_dict(self._cols, r) if r is not None else None

    def fetchall(self):
        return [_to_dict(self._cols, r) for r in self._cur.fetchall()]

    @property
    def rowcount(self):
        return self._cur.rowcount

    def __iter__(self):
        for r in self._cur:
            yield _to_dict(self._cols, r)


class _Conn:
    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, params=()):
        cur = self._raw.cursor()
        if params is None:
            params = ()
        if not isinstance(params, (list, tuple)):
            params = (params,)
        cur.execute(sql, params)
        return _Cursor(cur)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def app_db():
    return _Conn(pyodbc.connect(APP_SQL_CONN))


warehouse_db = app_db
IntegrityError = pyodbc.IntegrityError


# ---------- users ----------

def get_user_by_id(uid):
    con = app_db()
    row = con.execute('SELECT * FROM cc_users WHERE id = ?', (uid,)).fetchone()
    con.close()
    return dict(row) if row else None


def get_user_by_username(username):
    con = app_db()
    row = con.execute('SELECT * FROM cc_users WHERE username = ?',
                      (username,)).fetchone()
    con.close()
    return dict(row) if row else None


def list_users():
    con = app_db()
    rows = con.execute('SELECT * FROM cc_users ORDER BY username').fetchall()
    con.close()
    return [dict(r) for r in rows]


def list_user_emails_by_role(role):
    con = app_db()
    rows = con.execute(
        '''SELECT email FROM cc_users
           WHERE role = ? AND active = 1
             AND email IS NOT NULL AND email <> ''
           ORDER BY username''',
        (role,),
    ).fetchall()
    con.close()
    return [r['email'] for r in rows]


def create_user(username, full_name, email, role):
    """Creates the cc_users profile row. The Django account (which owns the
    actual password) must be created separately — see views.cc_users_new."""
    if role not in ('admin', 'analyst', 'priority', 'cycle_counter'):
        raise ValueError(f'invalid role: {role}')
    con = app_db()
    con.execute(
        '''INSERT INTO cc_users
               (username, full_name, email, password_hash, role,
                active, created_at)
           VALUES (?, ?, ?, ?, ?, 1, SYSUTCDATETIME())''',
        (username, full_name, email, _UNUSED_PASSWORD_PLACEHOLDER, role),
    )
    con.commit()
    con.close()


def set_user_active(uid, active):
    con = app_db()
    con.execute('UPDATE cc_users SET active = ? WHERE id = ?',
                (1 if active else 0, uid))
    con.commit()
    con.close()


def delete_user(uid):
    con = app_db()
    refs = con.execute(
        'SELECT COUNT(*) AS n FROM cc_count_requests '
        'WHERE requested_by = ? OR assigned_to = ?',
        (uid, uid),
    ).fetchone()['n']
    if refs > 0:
        con.close()
        return False, (f'User has {refs} count request(s). '
                       'Deactivate instead of deleting.')
    con.execute('DELETE FROM cc_users WHERE id = ?', (uid,))
    con.commit()
    con.close()
    return True, None


# ---------- count requests ----------

def create_request(part_number, count_date, reason, priority, cc_emails, requested_by):
    con = app_db()
    row = con.execute(
        '''INSERT INTO cc_count_requests
               (part_number, count_date, reason, priority,
                cc_emails, requested_by, status, created_at)
           OUTPUT INSERTED.id AS id
           VALUES (?, ?, ?, ?, ?, ?, 'pending', SYSUTCDATETIME())''',
        (part_number, count_date, reason, int(priority), cc_emails, requested_by),
    ).fetchone()
    con.commit()
    con.close()
    return int(row['id'])


def get_request(rid):
    con = app_db()
    row = con.execute(
        '''SELECT r.*,
                  ru.full_name AS requested_by_name,
                  ru.email     AS requested_by_email,
                  au.full_name AS assigned_to_name,
                  au.username  AS assigned_to_username,
                  rv.full_name AS reviewed_by_name
           FROM cc_count_requests r
           LEFT JOIN cc_users ru ON ru.id = r.requested_by
           LEFT JOIN cc_users au ON au.id = r.assigned_to
           LEFT JOIN cc_users rv ON rv.id = r.reviewed_by
           WHERE r.id = ?''',
        (rid,),
    ).fetchone()
    con.close()
    return dict(row) if row else None


def list_requests(status=None, assigned_to=None, requested_by=None):
    sql = '''SELECT r.*,
                    ru.full_name AS requested_by_name,
                    ru.role      AS requested_by_role,
                    au.full_name AS assigned_to_name,
                    p.routes     AS part_routes
             FROM cc_count_requests r
             LEFT JOIN cc_users ru ON ru.id = r.requested_by
             LEFT JOIN cc_users au ON au.id = r.assigned_to
             LEFT JOIN cc_parts p  ON p.part_number = r.part_number
             WHERE 1=1'''
    args = []
    if status:
        sql += ' AND r.status = ?'
        args.append(status)
    if assigned_to is not None:
        sql += ' AND r.assigned_to = ?'
        args.append(assigned_to)
    if requested_by is not None:
        sql += ' AND r.requested_by = ?'
        args.append(requested_by)
    sql += ''' ORDER BY CASE WHEN ru.role = 'priority' THEN 0 ELSE 1 END,
                        r.priority ASC, r.count_date ASC, r.created_at ASC'''
    con = app_db()
    rows = con.execute(sql, args).fetchall()
    con.close()
    return [dict(r) for r in rows]


def claim_request(rid, user_id):
    con = app_db()
    cur = con.execute(
        '''UPDATE cc_count_requests
              SET assigned_to = ?,
                  status = 'in_progress',
                  started_at = SYSUTCDATETIME()
            WHERE id = ? AND status = 'pending' AND assigned_to IS NULL''',
        (user_id, rid),
    )
    affected = cur.rowcount
    con.commit()
    con.close()
    return affected == 1


def cancel_request(rid):
    con = app_db()
    con.execute("UPDATE cc_count_requests SET status = 'cancelled' WHERE id = ?", (rid,))
    con.commit()
    con.close()


def finish_request(rid):
    con = app_db()
    con.execute(
        "UPDATE cc_count_requests "
        "SET status = 'done', completed_at = SYSUTCDATETIME() "
        "WHERE id = ?", (rid,)
    )
    con.commit()
    con.close()


def review_request(rid, outcome, user_id):
    if outcome not in ('adjust', 'not_adjust'):
        raise ValueError(f'invalid review outcome: {outcome}')
    con = app_db()
    con.execute(
        '''UPDATE cc_count_requests
              SET status = 'reviewed',
                  review_outcome = ?,
                  reviewed_by = ?,
                  reviewed_at = SYSUTCDATETIME()
            WHERE id = ?''',
        (outcome, user_id, rid),
    )
    con.commit()
    con.close()


def reopen_for_recount(rid):
    con = app_db()
    con.execute(
        '''UPDATE cc_count_requests
              SET status = 'pending',
                  assigned_to = NULL,
                  started_at = NULL,
                  completed_at = NULL,
                  count_notes = NULL,
                  review_outcome = NULL,
                  reviewed_by = NULL,
                  reviewed_at = NULL
            WHERE id = ?''',
        (rid,),
    )
    con.execute('DELETE FROM cc_count_entries WHERE request_id = ?', (rid,))
    con.commit()
    con.close()


def delete_request(rid):
    con = app_db()
    con.execute('DELETE FROM cc_count_entries WHERE request_id = ?', (rid,))
    con.execute('DELETE FROM cc_count_requests WHERE id = ?', (rid,))
    con.commit()
    con.close()


def delete_all_cancelled():
    con = app_db()
    con.execute(
        '''DELETE FROM cc_count_entries
            WHERE request_id IN (SELECT id FROM cc_count_requests
                                  WHERE status = 'cancelled')''')
    cur = con.execute("DELETE FROM cc_count_requests WHERE status = 'cancelled'")
    n = cur.rowcount
    con.commit()
    con.close()
    return n


# ---------- count entries ----------

def get_entries(rid):
    con = app_db()
    rows = con.execute(
        'SELECT * FROM cc_count_entries WHERE request_id = ? ORDER BY storage_location',
        (rid,),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def upsert_entries_from_warehouse(rid, part_number):
    con = app_db()
    rows = con.execute(
        '''SELECT storage_location AS sl,
                  plant            AS plant,
                  currency         AS currency,
                  COALESCE(CAST(unrestricted        AS FLOAT), 0)
                + COALESCE(CAST(quality_inspection AS FLOAT), 0)
                + COALESCE(CAST(blocked            AS FLOAT), 0)
                + COALESCE(CAST(returns            AS FLOAT), 0) AS sys_qty,
                  COALESCE(value_unrestricted, 0)
                + COALESCE(Value_in_QualInsp, 0)
                + COALESCE(Value_BlockedStock, 0)
                + COALESCE(value_rets_blocked, 0)   AS sys_value
           FROM cc_warehouse_stock
           WHERE material = ?''',
        (part_number,),
    ).fetchall()

    for r in rows:
        qty = r['sys_qty'] or 0
        value = r['sys_value'] or 0
        unit_value = (value / qty) if qty else None
        con.execute(
            '''MERGE cc_count_entries AS t
               USING (VALUES (?, ?, ?, ?, ?, ?)) AS s
                     (request_id, storage_location, plant,
                      system_qty, unit_value, currency)
                  ON t.request_id = s.request_id
                 AND t.storage_location = s.storage_location
               WHEN MATCHED THEN UPDATE SET
                    plant       = s.plant,
                    system_qty  = s.system_qty,
                    unit_value  = s.unit_value,
                    currency    = s.currency,
                    updated_at  = SYSUTCDATETIME()
               WHEN NOT MATCHED THEN INSERT
                    (request_id, storage_location, plant, system_qty,
                     unit_value, currency, is_manual, updated_at)
                    VALUES (s.request_id, s.storage_location, s.plant,
                            s.system_qty, s.unit_value, s.currency,
                            0, SYSUTCDATETIME());''',
            (rid, r['sl'], r['plant'], qty, unit_value, r['currency']),
        )
    con.commit()
    con.close()


def fetch_bin_reference(part_number):
    con = app_db()
    try:
        details = [dict(r) for r in con.execute(
            '''SELECT e.stock_type    AS code,
                      e.storage_type  AS storage_type,
                      e.storage_bin   AS bin,
                      e.handling_unit AS hu,
                      e.quantity      AS qty,
                      e.Batch         AS batch,
                      st.plant        AS plant_match
               FROM cc_ewm_stock e
               LEFT JOIN cc_stock_types st ON st.code = e.stock_type
               WHERE e.product = ?''',
            (part_number,),
        ).fetchall()]
        sls = [r['sl'] for r in con.execute(
            'SELECT DISTINCT storage_location AS sl FROM cc_warehouse_stock WHERE material = ?',
            (part_number,),
        ).fetchall()]
    finally:
        con.close()

    def matches(plant_match, sl):
        if plant_match is None:
            return True
        if plant_match == sl:
            return True
        if '/' in plant_match:
            return sl in plant_match.split('/')
        return False

    tree = {}
    for sl in sls:
        per_type = {}
        for d in details:
            if not matches(d['plant_match'], sl):
                continue
            st_key = d['storage_type'] or '—'
            bin_key = d['bin'] or '—'
            per_type.setdefault(st_key, {}).setdefault(bin_key, []).append(d)
        tree[sl] = {
            st: {b: per_type[st][b] for b in sorted(per_type[st])}
            for st in sorted(per_type)
        }
    return tree


def get_part_info(part_number):
    con = app_db()
    row = con.execute('SELECT * FROM cc_parts WHERE part_number = ?',
                      (part_number,)).fetchone()
    con.close()
    return dict(row) if row else None


def set_part_routes(part_number, routes):
    if not part_number:
        return
    joined = '|'.join(routes) if routes else ''
    con = app_db()
    con.execute(
        '''MERGE cc_parts AS t
           USING (VALUES (?, ?)) AS s (part_number, routes)
              ON t.part_number = s.part_number
           WHEN MATCHED THEN UPDATE SET routes = s.routes
           WHEN NOT MATCHED THEN INSERT (part_number, routes, unit_cost, synced_at)
                VALUES (s.part_number, s.routes, 0, SYSUTCDATETIME());''',
        (part_number, joined),
    )
    con.commit()
    con.close()


def get_part_explosion(part_number):
    if not part_number:
        return None, None
    con = app_db()
    row = con.execute(
        'SELECT explosion_json, explosion_synced_at FROM cc_parts WHERE part_number = ?',
        (part_number,),
    ).fetchone()
    con.close()
    if not row or not row['explosion_json']:
        return None, None
    try:
        return _json.loads(row['explosion_json']), row['explosion_synced_at']
    except ValueError:
        return None, None


def set_part_explosion(part_number, payload):
    if not part_number:
        return
    blob = _json.dumps(payload) if payload is not None else None
    con = app_db()
    con.execute(
        '''MERGE cc_parts AS t
           USING (VALUES (?, ?, SYSUTCDATETIME())) AS s
                 (part_number, explosion_json, explosion_synced_at)
              ON t.part_number = s.part_number
           WHEN MATCHED THEN UPDATE SET
                explosion_json      = s.explosion_json,
                explosion_synced_at = s.explosion_synced_at
           WHEN NOT MATCHED THEN INSERT
                (part_number, explosion_json, explosion_synced_at, unit_cost, synced_at)
                VALUES (s.part_number, s.explosion_json,
                        s.explosion_synced_at, 0, SYSUTCDATETIME());''',
        (part_number, blob),
    )
    con.commit()
    con.close()


def upsert_part(part_number, description, part_type, status, unit_cost):
    con = app_db()
    con.execute(
        '''MERGE cc_parts AS t
           USING (VALUES (?, ?, ?, ?, ?)) AS s
                 (part_number, description, part_type, status, unit_cost)
              ON t.part_number = s.part_number
           WHEN MATCHED THEN UPDATE SET
                description = s.description,
                part_type   = s.part_type,
                status      = s.status,
                unit_cost   = s.unit_cost,
                synced_at   = SYSUTCDATETIME()
           WHEN NOT MATCHED THEN INSERT
                (part_number, description, part_type, status, unit_cost, synced_at)
                VALUES (s.part_number, s.description, s.part_type, s.status,
                        s.unit_cost, SYSUTCDATETIME());''',
        (part_number, description, part_type, status, unit_cost or 0),
    )
    con.commit()
    con.close()


def get_pou_slots_for_part(part_number):
    con = app_db()
    rows = con.execute(
        '''SELECT m.POU_ID             AS pou_id,
                  m.Slot_ID           AS slot_id,
                  m.FIFO              AS fifo,
                  m.FIFO_Qty          AS fifo_qty,
                  ps.storage_location AS sl
             FROM cc_mars_pou m
             LEFT JOIN cc_pou_storage ps ON ps.pou_id = m.POU_ID
            WHERE m.PartNumber = ?
            ORDER BY ps.storage_location, m.POU_ID, m.Slot_ID''',
        (part_number,),
    ).fetchall()
    con.close()
    by_sl = {}
    for r in rows:
        by_sl.setdefault(r['sl'], []).append({
            'pou_id': r['pou_id'],
            'slot_id': r['slot_id'],
            'fifo': r['fifo'],
            'fifo_qty': r['fifo_qty'],
        })
    return by_sl


def upsert_pou_mapping(pou_id, storage_location):
    con = app_db()
    con.execute(
        '''MERGE cc_pou_storage AS t
           USING (VALUES (?, ?)) AS s (pou_id, storage_location)
              ON t.pou_id = s.pou_id
           WHEN MATCHED THEN UPDATE SET storage_location = s.storage_location
           WHEN NOT MATCHED THEN INSERT (pou_id, storage_location)
                VALUES (s.pou_id, s.storage_location);''',
        (pou_id, storage_location),
    )
    con.commit()
    con.close()


def seed_pou_entries(rid, part_number):
    pou_by_sl = get_pou_slots_for_part(part_number)
    if not pou_by_sl:
        return
    existing = {e['storage_location'] for e in get_entries(rid)}
    part = get_part_info(part_number)
    unit_value = part['unit_cost'] if part and part.get('unit_cost') else None
    con = app_db()
    row = con.execute(
        '''SELECT TOP 1 currency FROM cc_count_entries
            WHERE request_id = ? AND currency IS NOT NULL''',
        (rid,),
    ).fetchone()
    con.close()
    currency = row['currency'] if row else None
    for sl in pou_by_sl:
        if not sl or sl in existing:
            continue
        add_manual_entry(rid, sl, plant=None, unit_value=unit_value, currency=currency)


def get_storage_location_descriptions():
    con = app_db()
    rows = con.execute('SELECT code, description FROM cc_storage_location_desc').fetchall()
    con.close()
    return {r['code']: r['description'] for r in rows}


def get_storage_type_descriptions(warehouse='PT01'):
    con = app_db()
    rows = con.execute(
        'SELECT storage_type, description FROM cc_storage_type_desc WHERE warehouse = ?',
        (warehouse,),
    ).fetchall()
    con.close()
    return {r['storage_type']: r['description'] for r in rows}


def set_progress_meta(rid, meta_json):
    con = app_db()
    con.execute('UPDATE cc_count_requests SET progress_meta = ? WHERE id = ?',
                (meta_json or None, rid))
    con.commit()
    con.close()


def get_progress_meta(rid):
    con = app_db()
    row = con.execute('SELECT progress_meta FROM cc_count_requests WHERE id = ?',
                      (rid,)).fetchone()
    con.close()
    return row['progress_meta'] if row else None


def set_email_message_id(rid, message_id):
    con = app_db()
    con.execute('UPDATE cc_count_requests SET email_message_id = ? WHERE id = ?',
                (message_id, rid))
    con.commit()
    con.close()


def update_count_notes(rid, notes):
    con = app_db()
    con.execute('UPDATE cc_count_requests SET count_notes = ? WHERE id = ?',
                (notes, rid))
    con.commit()
    con.close()


def delete_entry(rid, storage_location):
    con = app_db()
    cur = con.execute(
        '''DELETE FROM cc_count_entries
            WHERE request_id = ? AND storage_location = ? AND is_manual = 1''',
        (rid, storage_location),
    )
    affected = cur.rowcount
    con.commit()
    con.close()
    return affected == 1


def add_manual_entry(rid, storage_location, plant=None, unit_value=None, currency=None):
    con = app_db()
    try:
        con.execute(
            '''INSERT INTO cc_count_entries
                   (request_id, storage_location, plant, system_qty,
                    is_manual, unit_value, currency, updated_at)
               VALUES (?, ?, ?, 0, 1, ?, ?, SYSUTCDATETIME())''',
            (rid, storage_location, plant, unit_value, currency),
        )
        con.commit()
        return True
    except IntegrityError:
        con.rollback()
        return False
    finally:
        con.close()


def save_entries(rid, entries):
    con = app_db()
    for e in entries:
        con.execute(
            '''UPDATE cc_count_entries
                  SET counted_qty = ?, notes = ?,
                      updated_at  = SYSUTCDATETIME()
                WHERE request_id = ? AND storage_location = ?''',
            (e.get('counted_qty'), e.get('notes'), rid, e['storage_location']),
        )
    con.commit()
    con.close()


# ---------- lookup ----------

CATEGORIES = ['Unrestricted', 'Quality Inspection', 'Blocked', 'Returns']

SUFFIX_PLANT_MATCH_SQL = (
    "(st.plant IS NULL "
    "OR st.plant = w.\"Storage Location\" "
    "OR ('/' || st.plant || '/') LIKE ('%/' || w.\"Storage Location\" || '/%'))"
)


def _plant_matches(plant_match, storage_location):
    if plant_match is None:
        return True
    if plant_match == storage_location:
        return True
    if '/' in plant_match:
        return storage_location in plant_match.split('/')
    return False


def lookup_material(material):
    con = warehouse_db()
    try:
        rows = [dict(r) for r in con.execute(
            '''SELECT material           AS material,
                      storage_location   AS storage_location,
                      plant              AS plant,
                      unrestricted       AS unrestricted,
                      quality_inspection AS quality_inspection,
                      blocked            AS blocked,
                      returns            AS returns
               FROM cc_warehouse_stock
               WHERE material = ?
                 AND (COALESCE(TRY_CAST(unrestricted        AS FLOAT), 0) <> 0
                      OR COALESCE(TRY_CAST(quality_inspection AS FLOAT), 0) <> 0
                      OR COALESCE(TRY_CAST(blocked            AS FLOAT), 0) <> 0
                      OR COALESCE(TRY_CAST(returns            AS FLOAT), 0) <> 0)''',
            (material,),
        ).fetchall()]
        details = [dict(r) for r in con.execute(
            '''SELECT e.stock_type     AS code,
                      e.storage_type   AS storage_type,
                      e.storage_bin    AS bin,
                      e.handling_unit  AS hu,
                      e.quantity       AS qty,
                      st.plant          AS plant_match,
                      st.stock_category AS category
               FROM cc_ewm_stock e
               LEFT JOIN cc_stock_types st ON st.code = e.stock_type
               WHERE e.product = ?''',
            (material,),
        ).fetchall()] if rows else []
    finally:
        con.close()

    for r in rows:
        sl = r['storage_location']
        per_cat = {c: {} for c in CATEGORIES}
        for d in details:
            cat = d['category']
            if cat not in per_cat:
                continue
            if not _plant_matches(d['plant_match'], sl):
                continue
            per_cat[cat].setdefault(d['storage_type'], []).append(d)
        cats_list = []
        for c in CATEGORIES:
            key = c.lower().replace(' ', '_')
            try:
                qty = float(r.get(key) or 0)
            except (ValueError, TypeError):
                qty = 0.0
            groups = [
                {'storage_type': st,
                 'total_qty': sum((e['qty'] or 0) for e in entries),
                 'entries': entries}
                for st, entries in sorted(per_cat[c].items())
            ]
            r[f'{c}__groups'] = groups
            cats_list.append({'name': c, 'qty': qty, 'groups': groups})
        r['_categories'] = cats_list
    return rows
