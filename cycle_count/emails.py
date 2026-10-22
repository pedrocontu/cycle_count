"""Notification emails (request created / count finished)."""
from html import escape as h

from . import db, email_utils
from .filters import PRIORITY_LABELS, fixed, plain_num as _fmt_num

_TH = 'padding:.3rem .5rem; text-align:{align};'
_TD = 'padding:.3rem .5rem; border-bottom:1px solid #e0e0e0;'
_TD2 = 'padding:.25rem .5rem; border-bottom:1px solid #f0f0f0;'


def _money(value):
    """Values in currency: 2 decimals, like the count screen."""
    if value is None:
        return ''
    return '0.00' if abs(value) < 0.005 else fixed(value, 2)


def split_cc(cc_string):
    return [a.strip() for a in (cc_string or '').split(',') if a.strip()]


def _priority_label(req):
    return PRIORITY_LABELS.get(req['priority'], str(req['priority']))


def _table(headers, rows_html):
    head = ''.join(
        f'<th style="{_TH.format(align=align)}">{h(label)}</th>' for label, align in headers)
    return ('<table style="border-collapse:collapse; width:100%; font-size:.9rem;">'
            f'<thead><tr style="background:#f4f6f8;">{head}</tr></thead>'
            f'<tbody>{rows_html}</tbody></table>')


def send_request_created(rid):
    req = db.get_request(rid)
    if not req:
        return
    to_list = db.list_user_emails_by_role('cycle_counter')
    cc_list = (db.list_user_emails_by_role('admin')
               + ([req['requested_by_email']] if req['requested_by_email'] else [])
               + split_cc(req['cc_emails']))
    part = db.get_part_info(req['part_number'])
    desc = (part or {}).get('description') or ''
    label = _priority_label(req)
    subject = (f'[Cycle Count #{rid}] {req["part_number"]} — '
               f'{label} priority, due {req["count_date"]}')
    text = (
        f'A new cycle count has been requested.\n\n'
        f'Request #: {rid}\n'
        f'Part:      {req["part_number"]}' + (f'  ({desc})' if desc else '') + '\n'
        f'Count by:  {req["count_date"]}\n'
        f'Priority:  {req["priority"]} — {label}\n'
        f'Requested: {req["requested_by_name"]} <{req["requested_by_email"]}>\n\n'
        f'Reason:\n{req["reason"]}\n'
    )
    html = f'''<html><body style="font-family:Segoe UI,Arial,sans-serif;">
<h2 style="margin:0 0 .5rem;">Cycle Count #{rid}</h2>
<p><strong>Part:</strong> {h(req["part_number"])}
{' <span style="color:#5a6573;">(' + h(desc) + ')</span>' if desc else ''}</p>
<p><strong>Count by:</strong> {h(str(req["count_date"]))}</p>
<p><strong>Priority:</strong> {h(str(req["priority"]))} — {h(label)}</p>
<p><strong>Requested by:</strong>
   {h(req["requested_by_name"] or "")} &lt;{h(req["requested_by_email"] or "")}&gt;</p>
<p><strong>Reason:</strong></p>
<div style="white-space:pre-wrap; padding:.5rem .65rem; background:#f4f6f8;
            border-left:3px solid #0a66c2; border-radius:4px;">{h(req["reason"])}</div>
<p style="color:#5a6573; font-size:.85rem; margin-top:1rem;">
Open in the cycle-count app to claim and start.
</p>
</body></html>'''
    mid = email_utils.send_email(to=to_list, cc=cc_list, subject=subject,
                                 text_body=text, html_body=html)
    db.set_email_message_id(rid, mid)


def _color(v):
    v = v or 0
    if v < 0:
        return 'color:#c0392b;'
    if v > 0:
        return 'color:#27ae60;'
    return ''


def send_count_finished(rid, meta):
    req = db.get_request(rid)
    if not req:
        return
    meta = meta if isinstance(meta, dict) else {}
    entries = db.get_entries(rid)
    in_reply_to = req.get('email_message_id')
    to_list = (([req['requested_by_email']] if req['requested_by_email'] else [])
               + db.list_user_emails_by_role('cycle_counter')
               + split_cc(req['cc_emails']))
    cc_list = db.list_user_emails_by_role('admin')
    part = db.get_part_info(req['part_number'])
    desc = (part or {}).get('description') or ''
    sl_desc = db.get_storage_location_descriptions()

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
            'unit_var': var_units, 'total_var': var_value,
        })
        total_sys += sys_q
        total_cnt += cnt_for_math
        if var_value is not None:
            total_var_value += var_value
    total_var_units = total_cnt - total_sys
    rows = [r for r in all_rows if (r['sys'] or 0) != 0 or r['cnt'] is not None]

    feeders = [f for f in (meta.get('feeders') or []) if isinstance(f, dict)]
    explosion = [r for r in (meta.get('explosion') or [])
                 if isinstance(r, dict) and r.get('count') not in (None, '', 0, '0')]
    pou_p105 = [p for p in (meta.get('p105_pou') or []) if isinstance(p, dict)]
    currency = next((e['currency'] for e in entries if e['currency']), '') or ''
    notes_text = (req.get('count_notes') or '').strip()

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
        '', 'Counter notes:', ('  ' + notes_text) if notes_text else '  (none)',
        '', 'Storage Locations:',
        f'  {"SL":<8} {"QOH":>10} {"Counted":>10} {"Unit Var":>10} {"Total Var":>14}',
    ]
    for r in rows:
        lines.append(
            f'  {r["sl"]:<8} {_fmt_num(r["sys"]):>10} '
            f'{_fmt_num(r["cnt"]):>10} {_fmt_num(r["unit_var"]):>10} '
            f'{_money(r["total_var"]):>14}')
    lines.append(
        f'  {"TOTAL":<8} {_fmt_num(total_sys):>10} {_fmt_num(total_cnt):>10} '
        f'{_fmt_num(total_var_units):>10} {_money(total_var_value):>14}')
    text = '\n'.join(lines)

    subject = (f'Re: [Cycle Count #{rid}] {req["part_number"]} — completed'
               if in_reply_to else
               f'[Cycle Count #{rid}] {req["part_number"]} — completed '
               f'({_priority_label(req)} priority)')

    sl_rows_html = ''.join(
        f'<tr>'
        f'<td style="{_TD}">{h(r["sl"])}</td>'
        f'<td style="{_TD} color:#5a6573;">{h(r["desc"])}</td>'
        f'<td style="{_TD} text-align:right;">{h(_fmt_num(r["sys"]))}</td>'
        f'<td style="{_TD} text-align:right;">{h(_fmt_num(r["cnt"]))}</td>'
        f'<td style="{_TD} text-align:right;">{h(_fmt_num(r["unit_var"]))}</td>'
        f'<td style="{_TD} text-align:right; {_color(r["total_var"])}">'
        f'{h(_money(r["total_var"]))}</td>'
        f'</tr>'
        for r in rows)
    sl_rows_html += (
        f'<tr style="font-weight:bold; background:#f4f6f8;">'
        f'<td style="padding:.3rem .5rem;" colspan="2">TOTAL</td>'
        f'<td style="padding:.3rem .5rem; text-align:right;">{h(_fmt_num(total_sys))}</td>'
        f'<td style="padding:.3rem .5rem; text-align:right;">{h(_fmt_num(total_cnt))}</td>'
        f'<td style="padding:.3rem .5rem; text-align:right;">{h(_fmt_num(total_var_units))}</td>'
        f'<td style="padding:.3rem .5rem; text-align:right; {_color(total_var_value)}">'
        f'{h(_money(total_var_value))}</td>'
        f'</tr>')

    feeder_html = ''
    if feeders:
        feeder_rows = ''
        for f_ in feeders:
            if f_.get('manual'):
                feeder_rows += (
                    f'<tr><td style="{_TD2} color:#5a6573;" colspan="5">'
                    f'<em>{h(str(f_.get("label") or ""))}</em></td>'
                    f'<td style="{_TD2} text-align:right;">{h(_fmt_num(f_.get("count")))}</td></tr>')
            else:
                cells = ''.join(f'<td style="{_TD2}">{h(str(f_.get(k) or ""))}</td>'
                                for k in ('line_name', 'machine_name', 'feed', 'bank', 'track'))
                feeder_rows += (f'<tr>{cells}<td style="{_TD2} text-align:right;">'
                                f'{h(_fmt_num(f_.get("count")))}</td></tr>')
        feeder_html = ('<h3 style="margin:1rem 0 .35rem;">Machine / Feeder Usage</h3>'
                       + _table([('Line', 'left'), ('Machine', 'left'), ('Feed', 'left'),
                                 ('Bank', 'left'), ('Track', 'left'), ('Count', 'right')],
                                feeder_rows))

    expl_html = ''
    if explosion:
        expl_rows = ''.join(
            f'<tr>'
            f'<td style="{_TD2}">{h(str(e.get("top") or ""))}</td>'
            f'<td style="{_TD2}">{h(str(e.get("part") or ""))}</td>'
            f'<td style="{_TD2} text-align:right;">{h(_fmt_num(e.get("qty_per") or e.get("cum")))}</td>'
            f'<td style="{_TD2} text-align:right;">{h(_fmt_num(e.get("count")))}</td>'
            f'<td style="{_TD2} text-align:right;">{h(str(e.get("subtotal") or ""))}</td>'
            f'</tr>'
            for e in explosion)
        expl_html = ('<h3 style="margin:1rem 0 .35rem;">Explosion</h3>'
                     + _table([('Top', 'left'), ('Part', 'left'), ('Qty/PC', 'right'),
                               ('Count', 'right'), ('Subtotal', 'right')], expl_rows))

    pou_html = ''
    confirmed = [p for p in pou_p105 if p.get('confirmed')]
    if confirmed:
        pou_rows = ''.join(
            f'<tr>'
            f'<td style="{_TD2}">{h(str(p.get("pou_id") or ""))}</td>'
            f'<td style="{_TD2}">{h(str(p.get("slot_id") or ""))}</td>'
            f'<td style="{_TD2}">{h(str(p.get("fifo") or ""))}</td>'
            f'<td style="{_TD2} text-align:right;">{h(_fmt_num(p.get("fifo_qty")))}</td>'
            f'</tr>'
            for p in confirmed)
        pou_html = ('<h3 style="margin:1rem 0 .35rem;">POU (P105)</h3>'
                    + _table([('POU', 'left'), ('Slot', 'left'), ('FIFO', 'left'),
                              ('FIFO Qty', 'right')], pou_rows))

    label_td = 'padding:.15rem 1rem .15rem 0; color:#5a6573;'
    box = 'white-space:pre-wrap; padding:.5rem .65rem; background:#f4f6f8; border-radius:4px; margin-bottom:.75rem;'
    html = (
        f'<html><body style="font-family:Segoe UI,Arial,sans-serif; max-width:900px;">'
        f'<h2 style="margin:0 0 .5rem;">Cycle Count #{rid} — Completed</h2>'
        f'<table style="margin-bottom:.75rem; font-size:.95rem;">'
        f'<tr><td style="{label_td}">Part</td><td><strong>{h(req["part_number"])}</strong>'
        + (f' <span style="color:#5a6573;">({h(desc)})</span>' if desc else '') +
        f'</td></tr>'
        f'<tr><td style="{label_td}">Count date</td><td>{h(str(req["count_date"]))}</td></tr>'
        f'<tr><td style="{label_td}">Counted by</td><td>{h(req.get("assigned_to_name") or "—")}</td></tr>'
        f'<tr><td style="{label_td}">Currency</td><td>{h(currency or "—")}</td></tr>'
        f'</table>'
        f'<p style="margin:.25rem 0 .1rem;"><strong>Reason:</strong></p>'
        f'<div style="{box} border-left:3px solid #0a66c2;">{h(req["reason"])}</div>'
        f'<p style="margin:.25rem 0 .1rem;"><strong>Counter notes:</strong></p>'
        f'<div style="{box} border-left:3px solid #c0c0c0;">'
        + (h(notes_text) if notes_text else '<em style="color:#5a6573;">(none)</em>') +
        '</div>'
        '<h3 style="margin:1rem 0 .35rem;">Storage Locations</h3>'
        + _table([('SL', 'left'), ('Description', 'left'), ('QOH', 'right'),
                  ('Counted', 'right'), ('Unit Var', 'right'),
                  ('Total Var' + (f' ({currency})' if currency else ''), 'right')],
                 sl_rows_html)
        + f'{feeder_html}{expl_html}{pou_html}'
        '</body></html>'
    )

    email_utils.send_email(
        to=to_list, cc=cc_list, subject=subject,
        text_body=text, html_body=html,
        in_reply_to=in_reply_to, references=in_reply_to,
    )
