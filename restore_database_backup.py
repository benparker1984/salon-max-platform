"""Restore an exported application snapshot into a NEW, EMPTY PostgreSQL database.

Never points at a default database. Refuses existing application tables/sequences.
"""
import argparse
import base64
from datetime import date, datetime
from decimal import Decimal
import json
import os
from pathlib import Path


def decode(value):
    if isinstance(value, dict) and set(value) == {'__backup_type__', 'value'}:
        kind, data = value['__backup_type__'], value['value']
        return {'bytes': base64.b64decode, 'decimal': Decimal,
                'datetime': datetime.fromisoformat, 'date': date.fromisoformat}[kind](data)
    return value


def restore(snapshot, url):
    import psycopg
    from psycopg import sql
    if snapshot.get('format') != 'salonmax-application-database' or snapshot.get('version') != 1:
        raise ValueError('Unsupported backup')
    schemas = snapshot['schemas']
    if set(schemas) != {'public', 'salon_config'}:
        raise ValueError('Unexpected application schemas')
    if any(t['schema'] not in schemas or t['row_count'] != len(t['rows']) for t in snapshot['tables']):
        raise ValueError('Invalid backup table metadata')
    count = 0
    with psycopg.connect(url) as db:
        # Everything, including DDL and sequence creation, rolls back on failure.
        existing = db.execute('''select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace
            where n.nspname=any(%s) and c.relkind in ('r','p','S','v','m','f')''', (schemas,)).fetchone()[0]
        if existing:
            raise ValueError('Target database is not empty; refusing to overwrite it')
        for schema in schemas:
            db.execute(sql.SQL('create schema if not exists {}').format(sql.Identifier(schema)))
        for seq in snapshot['sequences']:
            if seq['schema'] not in schemas or seq['data_type'] not in ('smallint', 'integer', 'bigint'):
                raise ValueError('Unsupported sequence')
            command = sql.SQL('create sequence {}.{} as {} increment by {} minvalue {} maxvalue {} start with {} cache {} {}').format(
                sql.Identifier(seq['schema']), sql.Identifier(seq['name']), sql.SQL(seq['data_type']),
                *[sql.Literal(seq[k]) for k in ('increment_by', 'min_value', 'max_value', 'start_value', 'cache_size')],
                sql.SQL('cycle' if seq['cycle'] else 'no cycle'))
            db.execute(command)
        for table in snapshot['tables']:
            definitions = []
            for col in table['columns']:
                definition = sql.SQL('{} {}').format(sql.Identifier(col['column_name']), sql.SQL(col['postgres_type']))
                if col['column_default'] is not None:
                    definition += sql.SQL(' default ') + sql.SQL(col['column_default'])
                if col['not_null']:
                    definition += sql.SQL(' not null')
                definitions.append(definition)
            db.execute(sql.SQL('create table {}.{} ({})').format(sql.Identifier(table['schema']),
                sql.Identifier(table['name']), sql.SQL(',').join(definitions)))
        for table in snapshot['tables']:
            columns = [c['column_name'] for c in table['columns']]
            query = sql.SQL('insert into {}.{} ({}) values ({})').format(
                sql.Identifier(table['schema']), sql.Identifier(table['name']),
                sql.SQL(',').join(map(sql.Identifier, columns)), sql.SQL(',').join(sql.Placeholder() for _ in columns))
            if table['rows']:
                with db.cursor() as cursor:
                    cursor.executemany(query, [tuple(decode(row[c]) for c in columns) for row in table['rows']])
            actual = db.execute(sql.SQL('select count(*) from {}.{}').format(
                sql.Identifier(table['schema']), sql.Identifier(table['name']))).fetchone()[0]
            if actual != table['row_count']:
                raise ValueError('Restored row count mismatch')
            count += actual
        for constraint in snapshot['constraints']:
            db.execute(sql.SQL('alter table {}.{} add constraint {} {}').format(
                sql.Identifier(constraint['schema']), sql.Identifier(constraint['table_name']),
                sql.Identifier(constraint['name']), sql.SQL(constraint['definition'])))
        for index in snapshot['indexes']:
            db.execute(index['definition'])
        for seq in snapshot['sequences']:
            db.execute('select setval(%s::regclass,%s,%s)',
                       (sql.Identifier(seq['schema'], seq['name']).as_string(db), seq['last_value'], seq['is_called']))
        for owner in snapshot['sequence_ownership']:
            db.execute(sql.SQL('alter sequence {}.{} owned by {}.{}.{}').format(
                *[sql.Identifier(owner[k]) for k in ('sequence_schema', 'sequence_name', 'table_schema', 'table_name', 'column_name')]))
    return {'tables': len(snapshot['tables']), 'rows': count}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    parser.add_argument('--database-url-env', required=True, help='Environment variable containing a NEW EMPTY database connection URL')
    args = parser.parse_args()
    result = restore(json.loads(args.snapshot.read_text(encoding='utf-8')), os.environ[args.database_url_env])
    print(f"Restored and verified {result['tables']} tables, {result['rows']} rows.")
