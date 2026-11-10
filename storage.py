"""PostgreSQL support for the recovered provider's parameterized SQL.

SQLite is used only by isolated local tests. Production always honours DATABASE_URL.
"""
import os
import re
import sqlite3
from pathlib import Path

DATABASE_URL = os.environ.get('SALONMAX_DATABASE_URL') or os.environ.get('DATABASE_URL', '')


def postgres():
    return DATABASE_URL.startswith(('postgres://', 'postgresql://'))


class Cursor:
    def __init__(self, cursor, lastrowid=None):
        self.cursor = cursor
        self.lastrowid = lastrowid
        self.rowcount = cursor.rowcount

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

    def __iter__(self):
        return iter(self.cursor)


class Connection:
    def __init__(self, schema):
        import psycopg
        from psycopg.rows import dict_row
        self.raw = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        self.schema = schema
        if schema != 'public':
            from psycopg import sql
            self.raw.execute(sql.SQL('create schema if not exists {}').format(sql.Identifier(schema)))
            self.raw.execute(sql.SQL('set search_path to {}').format(sql.Identifier(schema)))
            self.raw.commit()

    def execute(self, statement, params=()):
        # Translate metadata queries explicitly; PostgreSQL has no PRAGMA interface.
        match = re.search(r"(?:pragma_table_info\(|pragma table_info\()'([a-z_]+)'\)", statement, re.I)
        if match:
            return Cursor(self.raw.execute(
                'select column_name as name from information_schema.columns where table_schema=%s and table_name=%s order by ordinal_position',
                (self.schema, match.group(1))))
        statement = re.sub(r'integer primary key autoincrement', 'bigserial primary key', statement, flags=re.I)
        statement = re.sub(r'default current_timestamp', "default (to_char(current_timestamp at time zone 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'))", statement, flags=re.I)
        # All recovered timestamps are stored as UTC text, including newly written values.
        if not re.match(r'\s*create\s', statement, re.I):
            statement = re.sub(r'\bcurrent_timestamp\b', "to_char(current_timestamp at time zone 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')", statement, flags=re.I)
        statement = statement.replace("datetime('now', '-1 day')", "(current_timestamp - interval '1 day')")
        statement = statement.replace('datetime(coalesce(customers.updated_at, customers.created_at))', 'cast(coalesce(nullif(customers.updated_at, \'\'), customers.created_at) as timestamptz)')
        statement = re.sub(r'datetime\(([^()]+)\)', r'cast(\1 as timestamptz)', statement)
        # psycopg treats percent signs specially only when parameters are supplied.
        if params:
            statement = statement.replace('%', '%%').replace('?', '%s')
        else:
            statement = statement.replace('?', '%s')
        match = re.match(r'\s*insert\s+into\s+([a-z_]+)', statement, re.I)
        returning = False
        if match and 'returning' not in statement.lower():
            columns = self.raw.execute(
                'select column_name from information_schema.columns where table_schema=%s and table_name=%s and column_name=\'id\'',
                (self.schema, match.group(1))).fetchone()
            if columns:
                statement = statement.rstrip().rstrip(';') + ' returning id'
                returning = True
        cursor = self.raw.execute(statement, params or None)
        row = cursor.fetchone() if returning else None
        return Cursor(cursor, row['id'] if row else None)

    def executescript(self, script):
        for statement in script.split(';'):
            if statement.strip():
                self.execute(statement)

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()


def connect(path, schema='public'):
    if postgres():
        return Connection(schema)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('pragma foreign_keys=on')
    return conn
