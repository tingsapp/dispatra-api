from datetime import datetime, timezone
from types import SimpleNamespace
from app.operations.reporting import attention_order_ids


def test_badge_counts_each_open_order_once_and_excludes_closed_and_archived():
    now=datetime(2026,11,19,tzinfo=timezone.utc)
    def order(id,status='NEW',archived_at=None):
        return SimpleNamespace(id=id,status=status,archived_at=archived_at,scheduled_at=now,
            facts={'stops':[{'id':id,'kind':'PICKUP'}]})
    rows=[order('due'),order('issues'),order('hidden','IN_PROGRESS'),order('done','COMPLETED'),order('cancelled','CANCELLED'),order('archived',archived_at=now)]
    warnings={'issues':[{'hidden':False},{'hidden':False}],'hidden':[{'hidden':True}]}
    assert attention_order_ids(rows,warnings,{'issues','done'}, {}, now)=={'due','issues'}
