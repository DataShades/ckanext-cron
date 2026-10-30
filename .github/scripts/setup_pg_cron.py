"""Prepare the pg_cron-enabled PostgreSQL service for the tests.

Creates the CKAN role and databases, and the pg_cron extension in the database
named by cron.database_name. Safe to run more than once.

    python setup_pg_cron.py postgresql://supabase_admin:pass@postgres/postgres
"""

import sys

import psycopg2

ROLE = "ckan_default"
PASSWORD = "pass"
DATABASES = ["ckan_test", "datastore_test"]

conn = psycopg2.connect(sys.argv[1])
conn.autocommit = True
cur = conn.cursor()

cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (ROLE,))
if not cur.fetchone():
    # a superuser, so that the tests can also create a limited role
    cur.execute(f"CREATE ROLE {ROLE} LOGIN SUPERUSER PASSWORD %s", (PASSWORD,))

for database in DATABASES:
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,))
    if not cur.fetchone():
        cur.execute(f"CREATE DATABASE {database} OWNER {ROLE}")

cur.execute("CREATE EXTENSION IF NOT EXISTS pg_cron")
cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'pg_cron'")
print("pg_cron", cur.fetchone()[0])
