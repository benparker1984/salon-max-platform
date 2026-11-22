"""Apply the audited dashboard fixes to the separately installed Pi back office."""
import shutil
from datetime import datetime, timezone
from pathlib import Path


REPLACEMENTS = {
    'select count(*) as value from customers"':
        'select count(*) as value from customers where is_active = 1"',
    "select count(*) as value from till_sessions where status = 'open'":
        "select count(distinct terminal_id) as value from till_sessions where status = 'open'",
    'select coalesce(sum(package_minutes), 0) as value from customers"':
        'select coalesce(sum(package_minutes), 0) as value from customers where is_active = 1"',
    'select coalesce(sum(account_balance), 0) as value from customers"':
        'select coalesce(sum(account_balance), 0) as value from customers where is_active = 1"',
}


def patch(source):
    start = source.index('def dashboard_totals():')
    end = source.index('\ndef ', start + 1)
    section = source[start:end]
    for before, after in REPLACEMENTS.items():
        if before not in section and after not in section:
            raise RuntimeError('Dashboard differs from audited version; review before applying')
        section = section.replace(before, after)
    if 'totals["archived_customer_count"]' not in section:
        section = section.replace('    return totals', '''    totals["archived_customer_count"] = query_one(
        "select count(*) as value from customers where is_active = 0"
    )["value"]
    totals["extra_open_session_count"] = query_one(
        "select count(*) - count(distinct terminal_id) as value from till_sessions where status = 'open'"
    )["value"]
    return totals''')
    return source[:start] + section + source[end:]


if __name__ == '__main__':
    path = Path.home() / 'Salon-Max/app.py'
    original = path.read_text()
    updated = patch(original)
    compile(updated, str(path), 'exec')
    if updated != original:
        backup = path.with_name('app.py.before-consistency-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
        shutil.copy2(path, backup)
        path.write_text(updated)
        print('Dashboard patch applied; previous source backed up.')
    else:
        print('Dashboard patch already applied.')
    template = path.parent / 'templates/dashboard.html'
    original = template.read_text()
    updated = original.replace('>Customers</div>', '>Active Customers</div>')
    updated = updated.replace('>Account Balance Total</div>', '>Active Account Balance Total</div>')
    updated = updated.replace('>Package Minutes Total</div>', '>Active Package Minutes Total</div>')
    if 'totals.archived_customer_count' not in updated:
        marker = '{{ totals.customer_count }}'
        if marker not in updated:
            raise RuntimeError('Customer counter template differs from audited version')
        updated = updated.replace(marker, marker + '''</div>
            <div class="muted">{{ totals.archived_customer_count }} archived (excluded from active totals)''', 1)
    if 'totals.extra_open_session_count' not in updated:
        warning = '''
{% if totals.extra_open_session_count %}
<div class="panel">
    <strong>Cash sessions need review</strong>
    <p>{{ totals.extra_open_session_count }} additional open session records exist for tills already counted above.
    Their cash history has been preserved. Review these in <a href="{{ url_for('till_sessions') }}">Till Sessions</a>.</p>
</div>
{% endif %}
'''
        updated = updated.replace('{% endblock %}', warning + '{% endblock %}', 1)
    if updated != original:
        shutil.copy2(template, template.with_suffix('.html.before-consistency-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')))
        template.write_text(updated)
