# Cycle Count

Aplicação Flask para pedidos e execução de contagens cíclicas (SQL Server `cc_*`,
explosão BOM no Azure SAP_QADPRD, utilização de feeders no MySQL, REF CODES em Excel).

## Instalação

Requer Python 3.10+ e o *ODBC Driver 17 (ou 18) for SQL Server*.

```bash
python -m venv venv
venv\Scripts\activate            # Linux: source venv/bin/activate
pip install -r requirements.txt
copy .env.example .env           # Linux: cp .env.example .env
```

Preencher o `.env` (nunca fazer commit dele). A `CC_SECRET_KEY` gera-se com:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

## Arrancar

```bash
waitress-serve --listen=0.0.0.0:8000 wsgi:app
```

Desenvolvimento: `flask --app wsgi run --debug`

## Migração a partir da versão Django (uma vez)

1. Correr `sql/001_password_hash.sql` (alarga `cc_users.password_hash`, que passa a guardar a password).
2. Copiar as passwords dos utilizadores existentes a partir do `db.sqlite3` antigo:
   `flask --app wsgi import-django-passwords caminho\para\db.sqlite3`
   (quem não for importado tem de receber uma password nova em *Users → Reset password*).
3. Se ainda não houver nenhum admin: `flask --app wsgi create-admin <username> --email <email>`

O login fixo `admin` / `nimda` deixou de existir.

## Testes

Os testes correm contra um SQL Server verdadeiro (cria a base `cc_pytest`) e um Chromium:

```bash
pip install -r requirements-dev.txt
docker run -d -e ACCEPT_EULA=Y -e "MSSQL_SA_PASSWORD=Test_Passw0rd!" -p 1433:1433 mcr.microsoft.com/mssql/server:2022-latest
export CC_TEST_SQL_SERVER="DRIVER={ODBC Driver 18 for SQL Server};SERVER=127.0.0.1,1433;UID=sa;PWD=Test_Passw0rd!;TrustServerCertificate=yes"
playwright install chromium        # ou CC_TEST_CHROMIUM=/caminho/para/chrome
pytest
```
