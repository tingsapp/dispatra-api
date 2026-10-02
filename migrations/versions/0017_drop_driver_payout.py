"""Drop driver payout, employment, per-driver Order limit and vehicle running cost.

Drivers no longer carry employment or payout shares, Orders no longer freeze a payout estimate,
the company maximum active Orders applies to every driver, and vehicle running cost is set per
vehicle type in Billing. Values are stripped from the JSONB records so stored data matches the API.
"""
from alembic import op

revision = '0017_drop_driver_payout'
down_revision = '0016_merge_shipper_identity'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE orders DROP COLUMN payout")
    op.execute("UPDATE drivers SET data = data - 'employment' - 'revenue_share_percent' - 'fuel_surcharge_share_percent' - 'maximum_active_orders'")
    op.execute("UPDATE vehicles SET data = data - 'running_cost_per_km'")


def downgrade():
    op.execute("ALTER TABLE orders ADD COLUMN payout jsonb")
    op.execute("""UPDATE drivers SET data = data || '{"employment": "EMPLOYEE", "revenue_share_percent": "0", "fuel_surcharge_share_percent": "0", "maximum_active_orders": null}'::jsonb""")
    op.execute("""UPDATE vehicles SET data = data || '{"running_cost_per_km": null}'::jsonb""")
