from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from flask import current_app, g
from flask_wtf.csrf import generate_csrf
from markupsafe import Markup

PRIORITY_LABELS = {1: 'High', 2: 'Medium', 3: 'Low'}


def plain_num(value):
    """Number without trailing zeros; '' for None."""
    if value is None or value == '':
        return ''
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if f == int(f):
        return str(int(f))
    return f'{f:.6f}'.rstrip('0').rstrip('.')


def fixed(value, decimals=2):
    """Fixed number of decimals (like Django's floatformat:N); '' for None."""
    if value is None or value == '':
        return ''
    try:
        return f'{float(value):.{int(decimals)}f}'
    except (TypeError, ValueError):
        return str(value)


def priority_label(value):
    try:
        return PRIORITY_LABELS.get(int(value), str(value))
    except (TypeError, ValueError):
        return str(value)


def localdt(value, fmt='%Y-%m-%d %H:%M'):
    """SQL Server timestamps are written with SYSUTCDATETIME() (naive UTC):
    show them in the plant's local time."""
    if not value:
        return '—'
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.astimezone(ZoneInfo(current_app.config['CC_DISPLAY_TZ']))
        return value.strftime(fmt)
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def dt(value, fmt='%Y-%m-%d %H:%M'):
    """Format a datetime as-is (no timezone conversion)."""
    if not value:
        return '—'
    if isinstance(value, (datetime, date)):
        return value.strftime(fmt)
    return str(value)


def split_routes(value):
    return [r for r in (value or '').split('|') if r.strip()]


def register(app):
    app.jinja_env.filters.update(
        plain_num=plain_num, fixed=fixed, priority_label=priority_label,
        localdt=localdt, dt=dt, split_routes=split_routes,
    )

    @app.context_processor
    def _globals():
        return {
            'cc_user': g.get('user'),
            'csrf_field': lambda: Markup(
                f'<input type="hidden" name="csrf_token" value="{generate_csrf()}">'),
        }
