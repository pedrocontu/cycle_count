from functools import wraps

from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect

from .auth import get_cc_user


def cc_login_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not get_cc_user(request):
            return redirect('cycle_count:login')
        return view_func(request, *args, **kwargs)
    return wrapper


def cc_role_required(*roles):
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            user = get_cc_user(request)
            if not user:
                return redirect('cycle_count:login')
            if user['role'] not in roles:
                raise PermissionDenied
            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator
