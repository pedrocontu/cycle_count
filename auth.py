import datetime

SESSION_KEY = '_cc_user'

# Bootstrap admin credentials — hardcoded for testing without database access
# username: admin  password: nimda
_BOOTSTRAP_ADMIN = {
    'id': 0,
    'username': 'admin',
    'full_name': 'System Administrator',
    'email': 'admin@visteon.com',
    'role': 'admin',
    'active': 1,
}


def get_cc_user(request):
    return request.session.get(SESSION_KEY)


def set_cc_user(request, user_dict):
    safe = {}
    for k, v in user_dict.items():
        if isinstance(v, (datetime.datetime, datetime.date)):
            safe[k] = v.isoformat()
        else:
            safe[k] = v
    request.session[SESSION_KEY] = safe


def clear_cc_user(request):
    request.session.pop(SESSION_KEY, None)


def get_bootstrap_admin():
    """Return a copy of the hardcoded bootstrap admin user dict."""
    return dict(_BOOTSTRAP_ADMIN)
