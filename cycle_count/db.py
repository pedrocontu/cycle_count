"""Raw SQL access layer for the cc_* tables (SQL Server, via pyodbc).

Every public function opens its own connection through `_connect()`, which
always closes it and rolls back on error.  pyodbc enables ODBC connection
pooling by default, so this is cheap.
"""
import json
from contextlib import contextmanager
from decimal import Decimal

import pyodbc

IntegrityError = pyodbc.IntegrityError
Error = pyodbc.Error


class DatabaseUnavailable(Exception):
    """The SQL Server could not be reached (as opposed to a failing query)."""

ROLES = ('admin', 'analyst', 'priority', 'cycle_counter')
REVIEW_OUTCOMES = ('adjust', 'not_adjust')
CATEGORIES = ['Unrestricted', 'Quality Inspection', 'Blocked', 'Returns']

_conn_str = None


def configure(conn_str):
    global _conn_str
    _conn_str = conn_str


# ---------- pyodbc → dict-row facade ----------

def _to_dict(cols, row):
    return {col: (float(v) if isinstance(v, Decimal) else v)
            for col, v in zip(cols, row)}


class _Cursor:
    def __init__(self, cur):
        self._cur = cur
        self._cols = [c[0] for c in cur.description] if cur.description else None

    def fetchone(self):
        r = self._cur.fetchone()
        return _to_dict(self._cols, r) if r is not None else None

    def fetchall(self):
        return [_to_dict(self._cols, r) for r in self._cur.fetchall()]

    @property
    def rowcount(self):
        return self._cur.rowcount


class _Conn:
    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, params=()):
        cur = self._raw.cursor()
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


@contextmanager
def _connect():
    if not _conn_str:
        raise RuntimeError('CC_SQL_CONN is not configured.')
    try:
        raw = pyodbc.connect(_conn_str)
    except pyodbc.Error as e:
        raise DatabaseUnavailable(str(e)) from e
    con = _Conn(raw)
    try:
        yield con
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        raise
    finally:
        con.close()


def _plant_matches(plant_match, storage_location):
    """cc_stock_types.plant is either NULL (any SL), one SL, or 'A/B/C'."""
    if plant_match is None or plant_match == storage_location:
        return True
    if '/' in plant_match:
        return storage_location in plant_match.split('/')
    return False


# ---------- users ----------

def get_user_by_id(uid):
    with _connect() as con:
        return con.execute('SELECT * FROM cc_users WHERE id = ?', (uid,)).fetchone()


def get_user_by_username(username):
    with _connect() as con:
        return con.execute('SELECT * FROM cc_users WHERE username = ?',
                           (username,)).fetchone()


def list_users():
    with _connect() as con:
        return con.execute('SELECT * FROM cc_users ORDER BY username').fetchall()


def list_user_emails_by_role(role):
    with _connect() as con:
        rows = con.execute(
            '''SELECT email FROM cc_users
               WHERE role = ? AND active = 1
                 AND email IS NOT NULL AND email <> ''
               ORDER BY username''',
            (role,),
        ).fetchall()
    return [r['email'] for r in rows]


def create_user(username, full_name, email, role, password_hash):
    if role not in ROLES:
        raise ValueError(f'invalid role: {role}')
    with _connect() as con:
        con.execute(
            '''INSERT INTO cc_users
                   (username, full_name, email, password_hash, role,
                    active, created_at)
               VALUES (?, ?, ?, ?, ?, 1, SYSUTCDATETIME())''',
            (username, full_name, email, password_hash, role),
        )
        con.commit()


def set_user_active(uid, active):
    with _connect() as con:
        con.execute('UPDATE cc_users SET active = ? WHERE id = ?',
                    (1 if active else 0, uid))
        con.commit()


def set_user_password(uid, password_hash):
    with _connect() as con:
        con.execute('UPDATE cc_users SET password_hash = ? WHERE id = ?',
                    (password_hash, uid))
        con.commit()


def delete_user(uid):
    with _connect() as con:
        refs = con.execute(
            '''SELECT COUNT(*) AS n FROM cc_count_requests
               WHERE requested_by = ? OR assigned_to = ? OR reviewed_by = ?''',
            (uid, uid, uid),
        ).fetchone()['n']
        if refs > 0:
            return False, (f'User is referenced by {refs} count request(s). '
                           'Deactivate instead of deleting.')
        con.execute('DELETE FROM cc_users WHERE id = ?', (uid,))
        con.commit()
    return True, None


# ---------- count requests ----------

def create_request(part_number, count_date, reason, priority, cc_emails, requested_by):
    with _connect() as con:
        row = con.execute(
            '''INSERT INTO cc_count_requests
                   (part_number, count_date, reason, priority,
                    cc_emails, requested_by, status, created_at)
               OUTPUT INSERTED.id AS id
               VALUES (?, ?, ?, ?, ?, ?, 'pending', SYSUTCDATETIME())''',
            (part_number, count_date, reason, int(priority), cc_emails, requested_by),
        ).fetchone()
        con.commit()
    return int(row['id'])


def get_request(rid):
    with _connect() as con:
        return con.execute(
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


_TOTALS_JOIN = '''
    LEFT JOIN (
        SELECT request_id,
               SUM(COALESCE(system_qty, 0))  AS sys_total,
               SUM(COALESCE(counted_qty, 0)) AS cnt_total,
               SUM((COALESCE(counted_qty, 0) - COALESCE(system_qty, 0))
                   * COALESCE(unit_value, 0)) AS valor_delta,
               COALESCE(
                   SUM(COALESCE(system_qty, 0) * unit_value)
                     / NULLIF(SUM(CASE WHEN unit_value IS NOT NULL
                                       THEN COALESCE(system_qty, 0) END), 0),
                   AVG(unit_value)) AS avg_unit_value
        FROM cc_count_entries
        GROUP BY request_id
    ) t ON t.request_id = r.id'''


def list_requests(status=None, assigned_to=None, requested_by=None, with_totals=False):
    """List requests (priority users first). `with_totals` adds the count
    totals in the same query — no per-row queries and no IN (...) list, so it
    works no matter how many requests exist."""
    totals_cols = ''
    totals_join = ''
    if with_totals:
        totals_cols = ''',
                    COALESCE(t.sys_total, 0)   AS sys_total,
                    COALESCE(t.cnt_total, 0)   AS cnt_total,
                    COALESCE(t.cnt_total, 0) - COALESCE(t.sys_total, 0) AS delta_total,
                    COALESCE(t.valor_delta, 0) AS valor_delta,
                    t.avg_unit_value           AS avg_unit_value'''
        totals_join = _TOTALS_JOIN
    sql = f'''SELECT r.*,
                    ru.full_name AS requested_by_name,
                    ru.role      AS requested_by_role,
                    au.full_name AS assigned_to_name,
                    p.routes     AS part_routes{totals_cols}
             FROM cc_count_requests r
             LEFT JOIN cc_users ru ON ru.id = r.requested_by
             LEFT JOIN cc_users au ON au.id = r.assigned_to
             LEFT JOIN cc_parts p  ON p.part_number = r.part_number{totals_join}
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
    with _connect() as con:
        return con.execute(sql, args).fetchall()


def _populate_entries(con, rid, part_number):
    """Copy the SAP warehouse stock of the part into cc_count_entries.
    TRY_CAST: a non-numeric value in the stock table must not abort the claim."""
    rows = con.execute(
        '''SELECT storage_location AS sl,
                  MAX(plant)       AS plant,
                  MAX(currency)    AS currency,
                  SUM(COALESCE(TRY_CAST(unrestricted       AS FLOAT), 0)
                    + COALESCE(TRY_CAST(quality_inspection AS FLOAT), 0)
                    + COALESCE(TRY_CAST(blocked            AS FLOAT), 0)
                    + COALESCE(TRY_CAST(returns            AS FLOAT), 0)) AS sys_qty,
                  SUM(COALESCE(TRY_CAST(value_unrestricted AS FLOAT), 0)
                    + COALESCE(TRY_CAST(Value_in_QualInsp  AS FLOAT), 0)
                    + COALESCE(TRY_CAST(Value_BlockedStock AS FLOAT), 0)
                    + COALESCE(TRY_CAST(value_rets_blocked AS FLOAT), 0)) AS sys_value
           FROM cc_warehouse_stock
           WHERE material = ?
           GROUP BY storage_location''',
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


def claim_request(rid, user_id):
    """Assign a pending request and snapshot the system stock — atomically:
    if the snapshot fails, the request stays pending."""
    with _connect() as con:
        req = con.execute('SELECT part_number FROM cc_count_requests WHERE id = ?',
                          (rid,)).fetchone()
        if not req:
            return False
        cur = con.execute(
            '''UPDATE cc_count_requests
                  SET assigned_to = ?,
                      status = 'in_progress',
                      started_at = SYSUTCDATETIME()
                WHERE id = ? AND status = 'pending' AND assigned_to IS NULL''',
            (user_id, rid),
        )
        if cur.rowcount != 1:
            con.rollback()
            return False
        _populate_entries(con, rid, req['part_number'])
        con.commit()
    return True


def cancel_request(rid):
    with _connect() as con:
        cur = con.execute(
            '''UPDATE cc_count_requests SET status = 'cancelled'
                WHERE id = ? AND status IN ('pending', 'in_progress')''', (rid,))
        ok = cur.rowcount == 1
        con.commit()
    return ok


def finish_request(rid, user_id):
    with _connect() as con:
        cur = con.execute(
            '''UPDATE cc_count_requests
                  SET status = 'done', completed_at = SYSUTCDATETIME()
                WHERE id = ? AND status = 'in_progress' AND assigned_to = ?''',
            (rid, user_id))
        ok = cur.rowcount == 1
        con.commit()
    return ok


def review_request(rid, outcome, user_id):
    if outcome not in REVIEW_OUTCOMES:
        raise ValueError(f'invalid review outcome: {outcome}')
    with _connect() as con:
        cur = con.execute(
            '''UPDATE cc_count_requests
                  SET status = 'reviewed',
                      review_outcome = ?,
                      reviewed_by = ?,
                      reviewed_at = SYSUTCDATETIME()
                WHERE id = ? AND status = 'done' ''',
            (outcome, user_id, rid),
        )
        ok = cur.rowcount == 1
        con.commit()
    return ok


def reopen_for_recount(rid):
    """Send a finished count back to pending. Clears every trace of the
    previous count, including the saved screen state (progress_meta)."""
    with _connect() as con:
        cur = con.execute(
            '''UPDATE cc_count_requests
                  SET status = 'pending',
                      assigned_to = NULL,
                      started_at = NULL,
                      completed_at = NULL,
                      count_notes = NULL,
                      progress_meta = NULL,
                      review_outcome = NULL,
                      reviewed_by = NULL,
                      reviewed_at = NULL
                WHERE id = ? AND status = 'done' ''',
            (rid,),
        )
        if cur.rowcount != 1:
            con.rollback()
            return False
        con.execute('DELETE FROM cc_count_entries WHERE request_id = ?', (rid,))
        con.commit()
    return True


def delete_request(rid):
    """Only cancelled requests can be deleted."""
    with _connect() as con:
        con.execute(
            '''DELETE e FROM cc_count_entries e
                 JOIN cc_count_requests r ON r.id = e.request_id
                WHERE r.id = ? AND r.status = 'cancelled' ''', (rid,))
        cur = con.execute(
            "DELETE FROM cc_count_requests WHERE id = ? AND status = 'cancelled'", (rid,))
        ok = cur.rowcount == 1
        con.commit()
    return ok


def delete_all_cancelled():
    with _connect() as con:
        con.execute(
            '''DELETE e FROM cc_count_entries e
                 JOIN cc_count_requests r ON r.id = e.request_id
                WHERE r.status = 'cancelled' ''')
        cur = con.execute("DELETE FROM cc_count_requests WHERE status = 'cancelled'")
        n = cur.rowcount
        con.commit()
    return n


# ---------- count entries ----------

def get_entries(rid):
    with _connect() as con:
        return con.execute(
            'SELECT * FROM cc_count_entries WHERE request_id = ? ORDER BY storage_location',
            (rid,),
        ).fetchall()


def save_count(rid, entries, notes, meta_json=None):
    """Save counted quantities, notes and (if given) the screen state in one
    transaction."""
    with _connect() as con:
        for e in entries:
            con.execute(
                '''UPDATE cc_count_entries
                      SET counted_qty = ?, updated_at = SYSUTCDATETIME()
                    WHERE request_id = ? AND storage_location = ?''',
                (e['counted_qty'], rid, e['storage_location']),
            )
        con.execute('UPDATE cc_count_requests SET count_notes = ? WHERE id = ?',
                    (notes, rid))
        if meta_json is not None:
            con.execute('UPDATE cc_count_requests SET progress_meta = ? WHERE id = ?',
                        (meta_json, rid))
        con.commit()


def delete_entry(rid, storage_location):
    with _connect() as con:
        cur = con.execute(
            '''DELETE FROM cc_count_entries
                WHERE request_id = ? AND storage_location = ? AND is_manual = 1''',
            (rid, storage_location),
        )
        ok = cur.rowcount == 1
        con.commit()
    return ok


def add_manual_entry(rid, storage_location, plant=None, unit_value=None, currency=None):
    with _connect() as con:
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


def fetch_bin_reference(part_number):
    """{storage_location: {storage_type: {bin: [hu rows]}}} from EWM stock."""
    with _connect() as con:
        details = con.execute(
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
        ).fetchall()
        sls = [r['sl'] for r in con.execute(
            'SELECT DISTINCT storage_location AS sl FROM cc_warehouse_stock WHERE material = ?',
            (part_number,),
        ).fetchall()]

    tree = {}
    for sl in sls:
        per_type = {}
        for d in details:
            if not _plant_matches(d['plant_match'], sl):
                continue
            per_type.setdefault(d['storage_type'] or '—', {}) \
                    .setdefault(d['bin'] or '—', []).append(d)
        tree[sl] = {
            st: {b: per_type[st][b] for b in sorted(per_type[st])}
            for st in sorted(per_type)
        }
    return tree


# ---------- parts ----------

def get_part_info(part_number):
    with _connect() as con:
        return con.execute('SELECT * FROM cc_parts WHERE part_number = ?',
                           (part_number,)).fetchone()


def set_part_routes(part_number, routes):
    """Store the computed routes. '' means "computed, none found";
    NULL (never written) means "not computed yet"."""
    if not part_number:
        return
    joined = '|'.join(routes or [])
    with _connect() as con:
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


def get_part_explosion(part_number):
    if not part_number:
        return None, None
    with _connect() as con:
        row = con.execute(
            'SELECT explosion_json, explosion_synced_at FROM cc_parts WHERE part_number = ?',
            (part_number,),
        ).fetchone()
    if not row or not row['explosion_json']:
        return None, None
    try:
        return json.loads(row['explosion_json']), row['explosion_synced_at']
    except ValueError:
        return None, None


def set_part_explosion(part_number, payload):
    if not part_number:
        return
    blob = json.dumps(payload) if payload is not None else None
    with _connect() as con:
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


# ---------- POU ----------

def get_pou_slots_for_part(part_number):
    """{storage_location or None (unmapped): [slot dicts]}"""
    with _connect() as con:
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
    with _connect() as con:
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


def seed_pou_entries(rid, part_number):
    """Make sure every SL that has a POU slot for this part has an entry."""
    pou_by_sl = get_pou_slots_for_part(part_number)
    if not pou_by_sl:
        return
    entries = get_entries(rid)
    existing = {e['storage_location'] for e in entries}
    missing = [sl for sl in pou_by_sl if sl and sl not in existing]
    if not missing:
        return
    part = get_part_info(part_number)
    unit_value = part['unit_cost'] if part and part.get('unit_cost') else None
    currency = next((e['currency'] for e in entries if e['currency']), None)
    for sl in missing:
        add_manual_entry(rid, sl, plant=None, unit_value=unit_value, currency=currency)


# ---------- descriptions ----------

def get_storage_location_descriptions():
    with _connect() as con:
        rows = con.execute('SELECT code, description FROM cc_storage_location_desc').fetchall()
    return {r['code']: r['description'] for r in rows}


def get_storage_type_descriptions(warehouse='PT01'):
    with _connect() as con:
        rows = con.execute(
            'SELECT storage_type, description FROM cc_storage_type_desc WHERE warehouse = ?',
            (warehouse,),
        ).fetchall()
    return {r['storage_type']: r['description'] for r in rows}


# ---------- misc request fields ----------

def set_email_message_id(rid, message_id):
    with _connect() as con:
        con.execute('UPDATE cc_count_requests SET email_message_id = ? WHERE id = ?',
                    (message_id, rid))
        con.commit()


# ---------- lookup ----------

def lookup_material(material):
    with _connect() as con:
        rows = con.execute(
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
                      OR COALESCE(TRY_CAST(returns            AS FLOAT), 0) <> 0)
               ORDER BY storage_location''',
            (material,),
        ).fetchall()
        details = con.execute(
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
        ).fetchall() if rows else []

    for r in rows:
        sl = r['storage_location']
        per_cat = {c: {} for c in CATEGORIES}
        for d in details:
            if d['category'] in per_cat and _plant_matches(d['plant_match'], sl):
                per_cat[d['category']].setdefault(d['storage_type'] or '—', []).append(d)
        cats = []
        for c in CATEGORIES:
            try:
                qty = float(r.get(c.lower().replace(' ', '_')) or 0)
            except (ValueError, TypeError):
                qty = 0.0
            groups = [
                {'storage_type': st,
                 'total_qty': sum((e['qty'] or 0) for e in entries),
                 'entries': entries}
                for st, entries in sorted(per_cat[c].items())
            ]
            cats.append({'name': c, 'qty': qty, 'groups': groups})
        r['categories'] = cats
    return rows
