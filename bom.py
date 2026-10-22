import pyodbc
from .sync_sap import AZURE_CONN

EXPLOSION_SQL = """
WITH BOM_Path AS (
    SELECT
        ps_comp, ps_par, ps_qty_per,
        0 AS LEVEL,
        CAST(ps_par AS VARCHAR(MAX)) AS PATH,
        CAST(ps_qty_per AS DECIMAL(18,4)) AS CUMULATIVE_QTY,
        CAST(ps_qty_per AS VARCHAR(MAX)) AS QTY_PATH
    FROM ps_mstr
    WHERE ps_comp = ?
      AND ps_start <= CAST(GETDATE() AS DATE)
      AND ps_end   >= CAST(GETDATE() AS DATE)

    UNION ALL

    SELECT
        m.ps_comp, m.ps_par, m.ps_qty_per,
        p.LEVEL + 1,
        CAST(p.PATH + ',' + CAST(m.ps_par AS VARCHAR(MAX)) AS VARCHAR(MAX)),
        CAST(p.CUMULATIVE_QTY * m.ps_qty_per AS DECIMAL(18,4)),
        CAST(p.QTY_PATH + ',' + CAST(m.ps_qty_per AS VARCHAR(MAX)) AS VARCHAR(MAX))
    FROM ps_mstr m
    INNER JOIN BOM_Path p ON m.ps_comp = p.ps_par
    WHERE p.ps_par IS NOT NULL
      AND m.ps_comp NOT LIKE '%14B115%'
)
SELECT
    LEVEL, ps_par AS PS_PAR, CUMULATIVE_QTY, PATH, QTY_PATH
FROM BOM_Path bp
WHERE bp.ps_par NOT IN (SELECT ps_comp FROM BOM_Path)
ORDER BY LEVEL
"""


def _parse_path(path_str, qty_path_str):
    parts = [p.strip() for p in (path_str or '').split(',') if p.strip()]
    qtys_raw = [q.strip() for q in (qty_path_str or '').split(',') if q.strip()]
    qtys = []
    for q in qtys_raw:
        try:
            qtys.append(float(q))
        except ValueError:
            qtys.append(0.0)
    steps = []
    cum = 1.0
    for i, part in enumerate(parts):
        qty_per = qtys[i] if i < len(qtys) else 0.0
        cum *= qty_per
        steps.append({'part': part, 'qty_per': qty_per, 'cum_to_here': cum})
    return steps


def explode_part(part_number):
    out = {'groups': [], 'error': None}
    if not part_number:
        return out
    try:
        con = pyodbc.connect(AZURE_CONN, timeout=20)
    except Exception as e:
        out['error'] = f'Azure connect failed: {type(e).__name__}: {e}'
        return out
    try:
        cur = con.cursor()
        cur.execute(EXPLOSION_SQL, part_number)
        rows = cur.fetchall()
    except Exception as e:
        out['error'] = f'Explosion query failed: {type(e).__name__}: {e}'
        con.close()
        return out
    con.close()

    groups = {}
    order = []
    for r in rows:
        steps = _parse_path(r.PATH, r.QTY_PATH)
        if not steps:
            continue
        top = steps[-1]['part']
        if top not in groups:
            groups[top] = {'top': top, 'paths': []}
            order.append(top)
        groups[top]['paths'].append({
            'level': int(r.LEVEL),
            'cumulative': float(r.CUMULATIVE_QTY) if r.CUMULATIVE_QTY is not None else 0.0,
            'steps': steps,
        })
    for g in groups.values():
        g['merged'] = _merge_intermediates(g['paths'])
    out['groups'] = [groups[t] for t in order]
    return out


def _merge_intermediates(paths):
    merged = {}
    order = []
    for p in paths:
        intermediates = p['steps'][:-1]
        chain_parts = [s['part'] for s in p['steps']]
        chain_label = ' → '.join(reversed(chain_parts))
        for s in intermediates:
            key = s['part']
            if key not in merged:
                merged[key] = {
                    'part': key,
                    'total_cum': 0.0,
                    'chains': [],
                }
                order.append(key)
            merged[key]['total_cum'] += float(s['cum_to_here'] or 0)
            merged[key]['chains'].append({
                'path_str': chain_label,
                'qty_per': float(s['qty_per'] or 0),
                'cum': float(s['cum_to_here'] or 0),
                'level': int(p['level']),
            })
    return [merged[k] for k in order]
