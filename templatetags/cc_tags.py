from django import template

register = template.Library()


@register.filter
def plain_num(value):
    """Format a number without trailing zeros (like Python's %g but cleaner)."""
    if value is None or value == '':
        return ''
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if f == int(f):
        return str(int(f))
    return f'{f:.6f}'.rstrip('0').rstrip('.')


@register.filter
def abs_val(value):
    try:
        return abs(float(value))
    except (TypeError, ValueError):
        return value


@register.filter
def get_item(dictionary, key):
    """Allow {{ dict|get_item:key }} in templates."""
    if dictionary is None:
        return None
    return dictionary.get(key)


@register.filter
def replace_underscores(value):
    return str(value).replace('_', ' ')


@register.filter
def priority_label(value):
    return {1: 'High', 2: 'Medium', 3: 'Low'}.get(int(value or 0), str(value))


@register.filter
def is_none(value):
    return value is None


@register.filter
def none_to_empty(value):
    """Return empty string for None, otherwise the value (safe for 0 and 0.0)."""
    if value is None:
        return ''
    return value


@register.filter
def split(value, sep):
    if not value:
        return []
    return str(value).split(sep)
