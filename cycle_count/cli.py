"""Admin commands:  flask --app wsgi <command>"""
import getpass
import sqlite3

import click

from . import auth, db


def register(app):
    @app.cli.command('create-admin')
    @click.argument('username')
    @click.option('--full-name', default='Administrator')
    @click.option('--email', default='')
    def create_admin(username, full_name, email):
        """Create an admin user (asks for the password)."""
        if db.get_user_by_username(username):
            raise click.ClickException(f'User "{username}" already exists.')
        password = getpass.getpass('Password: ')
        if len(password) < auth.MIN_PASSWORD_LENGTH:
            raise click.ClickException(
                f'Password must be at least {auth.MIN_PASSWORD_LENGTH} characters.')
        if password != getpass.getpass('Repeat password: '):
            raise click.ClickException('Passwords do not match.')
        db.create_user(username, full_name, email, 'admin', auth.hash_password(password))
        click.echo(f'Admin "{username}" created.')

    @app.cli.command('import-django-passwords')
    @click.argument('sqlite_path', type=click.Path(exists=True, dir_okay=False))
    def import_django_passwords(sqlite_path):
        """Copy password hashes from the old Django db.sqlite3 (auth_user)
        into cc_users, so existing users keep their passwords."""
        con = sqlite3.connect(sqlite_path)
        try:
            rows = con.execute('SELECT username, password FROM auth_user').fetchall()
        finally:
            con.close()
        imported = skipped = 0
        for username, pw_hash in rows:
            user = db.get_user_by_username(username)
            if not user or not auth.is_legacy_hash(pw_hash):
                skipped += 1
                continue
            db.set_user_password(user['id'], pw_hash)
            imported += 1
        click.echo(f'Imported {imported} password(s); skipped {skipped}.')
