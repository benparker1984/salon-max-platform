"""Software-manager-only logical backup of every Salon Max application table."""
import base64
from datetime import date, datetime, timezone
from decimal import Decimal
import os

from flask import Blueprint, abort, jsonify, session
import storage

bp = Blueprint('database_backup', __name__)
SCHEMAS = ('public', 'salon_config')


def encode(value):
    if isinstance(value, (bytes, memoryview)):
        return {'__backup_type__': 'bytes', 'value': base64.b64encode(bytes(value)).decode()}
    if isinstance(value, Decimal):
        return {'__backup_type__': 'decimal', 'value': str(value)}
    if isinstance(value, (datetime, date)):
        return {'__backup_type__': type(value).__name__, 'value': value.isoformat()}
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [encode(v) for v in value]
    return value


def snapshot():
    # Separate read-only connection: one repeatable snapshot includes accounts,
    # ledgers, summaries, orders, staff, settings and commissioning together.
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
    tables = []
    with psycopg.connect(storage.DATABASE_URL, row_factory=dict_row) as db:
        db.execute('set transaction isolation level repeatable read read only')
        rows = db.execute('''select table_schema,table_name from information_schema.tables
            where table_schema=any(%s) and table_type='BASE TABLE'
            order by table_schema,table_name''', (list(SCHEMAS),)).fetchall()
        for table in rows:
            schema, name = table['table_schema'], table['table_name']
            columns = db.execute('''select a.attname as column_name,
                format_type(a.atttypid,a.atttypmod) as postgres_type,
                a.attnotnull as not_null,pg_get_expr(d.adbin,d.adrelid) as column_default
                from pg_attribute a join pg_class c on c.oid=a.attrelid
                join pg_namespace n on n.oid=c.relnamespace
                left join pg_attrdef d on d.adrelid=c.oid and d.adnum=a.attnum
                where n.nspname=%s and c.relname=%s and a.attnum>0 and not a.attisdropped
                order by a.attnum''', (schema, name)).fetchall()
            data = db.execute(sql.SQL('select * from {}.{}').format(
                sql.Identifier(schema), sql.Identifier(name))).fetchall()
            tables.append({'schema': schema, 'name': name, 'columns': columns,
                           'row_count': len(data), 'rows': encode(data)})
        constraints = db.execute('''select n.nspname as schema,c.relname as table_name,
            con.conname as name,pg_get_constraintdef(con.oid) as definition,con.contype as kind
            from pg_constraint con join pg_class c on c.oid=con.conrelid
            join pg_namespace n on n.oid=c.relnamespace where n.nspname=any(%s)
            order by case when con.contype='f' then 1 else 0 end,n.nspname,c.relname,con.conname''', (list(SCHEMAS),)).fetchall()
        indexes = db.execute('''select pg_get_indexdef(i.indexrelid) as definition
            from pg_index i join pg_class c on c.oid=i.indrelid
            join pg_namespace n on n.oid=c.relnamespace
            where n.nspname=any(%s) and not exists(select 1 from pg_constraint co where co.conindid=i.indexrelid)
            order by n.nspname,c.relname,i.indexrelid''', (list(SCHEMAS),)).fetchall()
        sequences = db.execute('''select schemaname as schema,sequencename as name,
            data_type::text as data_type,start_value,min_value,max_value,increment_by,cycle,cache_size,last_value
            from pg_sequences where schemaname=any(%s) order by schemaname,sequencename''', (list(SCHEMAS),)).fetchall()
        for sequence in sequences:
            state = db.execute(sql.SQL('select last_value,is_called from {}.{}').format(
                sql.Identifier(sequence['schema']), sql.Identifier(sequence['name']))).fetchone()
            sequence.update(state)
        ownership = db.execute('''select sn.nspname as sequence_schema,s.relname as sequence_name,
            tn.nspname as table_schema,t.relname as table_name,a.attname as column_name
            from pg_class s join pg_namespace sn on sn.oid=s.relnamespace
            join pg_depend d on d.objid=s.oid and d.deptype='a'
            join pg_class t on t.oid=d.refobjid join pg_namespace tn on tn.oid=t.relnamespace
            join pg_attribute a on a.attrelid=t.oid and a.attnum=d.refobjsubid
            where s.relkind='S' and sn.nspname=any(%s)''', (list(SCHEMAS),)).fetchall()
        captured = db.execute("select current_timestamp as timestamp").fetchone()['timestamp']
    return {'format': 'salonmax-application-database', 'version': 1,
            'captured_at': captured.isoformat(), 'database': 'postgresql',
            'source_commit': os.environ.get('COMMIT_HASH', os.environ.get('GIT_COMMIT', '')),
            'schemas': list(SCHEMAS), 'tables': tables, 'constraints': constraints,
            'indexes': indexes, 'sequences': sequences, 'sequence_ownership': ownership}


@bp.get('/platform/backups/database.json')
def download():
    if not (session.get('platform_admin_authenticated') or session.get('salonmax_platform_authed')):
        abort(403)
    if not storage.postgres():
        abort(503, 'Hosted PostgreSQL backup is not configured')
    response = jsonify(snapshot())
    response.headers['Cache-Control'] = 'no-store, private'
    response.headers['Content-Disposition'] = 'attachment; filename="salonmax-database.json"'
    return response
