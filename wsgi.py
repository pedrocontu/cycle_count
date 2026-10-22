"""WSGI entry point:  waitress-serve --listen=0.0.0.0:8000 wsgi:app"""
from cycle_count import create_app

app = create_app()
