"""Fixed-period commercial reporting with small-cohort suppression."""
import json
from collections import defaultdict
from itertools import chain
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from flask import Blueprint, render_template

import provider

bp = Blueprint("commercial", __name__)
SALE_TYPES = {"package_sale", "tanning_sale", "retail_sale"}


def monthly_rollup(rows):
    revenue = defaultdict(int)
    counts = defaultdict(int)
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
            if not isinstance(payload, dict) or payload.get("transaction_type") not in SALE_TYPES:
                continue
            amount = Decimal(str(payload.get("total_amount")))
            if not amount.is_finite() or not 0 < amount <= 100_000:
                continue
            pennies = int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            if not pennies:
                continue
        except (ValueError, TypeError, InvalidOperation):
            continue
        business = row["business_account_public_id"]
        revenue[business] += pennies
        counts[business] += 1
    gross, transactions = sum(revenue.values()), sum(counts.values())
    if len(revenue) < 5 or transactions < 30:
        return None
    if max(revenue.values()) * 2 >= gross or max(counts.values()) * 2 >= transactions:
        return None
    return {"gross": ((gross + 5000) // 10000) * 100, "transactions": ((transactions + 5) // 10) * 10}


@bp.get("/platform/commercial")
def overview():
    end = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start = (end - timedelta(days=1)).replace(day=1)
    sql = """
        select e.business_account_public_id, e.payload_json
        from cloud_sync_events e
        join cloud_business_accounts b on b.business_account_public_id=e.business_account_public_id
        where b.product_type='salon' and e.event_type='transaction.completed'
          and e.occurred_at >= ? and e.occurred_at < ?
    """
    rows = provider.get_platform_db().execute(
        sql, tuple(value.strftime("%Y-%m-%dT%H:%M:%SZ") for value in (start, end))
    )
    dates = tuple(value.strftime("%Y-%m-%dT%H:%M:%SZ") for value in (start, end))
    web_orders = provider.platform_query_all("""
        select o.tenant, o.amount from web_orders o
        join cloud_business_accounts b on b.business_account_public_id=o.tenant
        where b.product_type='salon' and o.paid_at >= ? and o.paid_at < ?
    """, dates)
    web_sales = ({
        "business_account_public_id": order["tenant"],
        "payload_json": json.dumps({"transaction_type": "package_sale", "total_amount": str(Decimal(order["amount"]) / 100)})
    } for order in web_orders)
    return render_template("commercial.html", title="Commercial Benchmarks",
                           month=start.strftime("%B %Y"), totals=monthly_rollup(chain(rows, web_sales)))
