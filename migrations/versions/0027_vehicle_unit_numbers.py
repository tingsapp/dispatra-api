"""Use unique fleet unit numbers as the suffix of vehicle public IDs."""
from alembic import op
from sqlalchemy import text

revision = '0027_vehicle_unit_numbers'
down_revision = '0026_package_accessorials'
branch_labels = None
depends_on = None

UNIT = "upper(btrim(coalesce(nullif(btrim(data->>'unit_number'), ''), data->>'name', '')))"


def upgrade():
    db = op.get_bind()
    db.execute(text("SELECT set_config('app.platform', 'true', true)"))
    db.execute(text('LOCK TABLE vehicles IN ACCESS EXCLUSIVE MODE'))
    invalid = db.scalar(text(f'SELECT count(*) FROM vehicles WHERE length({UNIT}) NOT BETWEEN 1 AND 50'))
    duplicates = db.scalar(text(f'''SELECT count(*) FROM (
        SELECT organization_id, {UNIT} FROM vehicles GROUP BY organization_id, {UNIT} HAVING count(*) > 1
    ) repeated'''))
    if invalid or duplicates:
        raise RuntimeError(f'Vehicle unit-number migration requires valid unique units: {invalid} invalid vehicles, '
                           f'{duplicates} duplicate groups. Correct unit numbers before retrying; no vehicles were changed.')
    op.execute('ALTER TABLE vehicles ALTER COLUMN number TYPE varchar(54)')
    # Remove immediate uniqueness while replacing old numbers, which may overlap another unit's new ID.
    op.execute('ALTER TABLE vehicles DROP CONSTRAINT vehicles_company_number')
    op.execute(f'''UPDATE vehicles SET number = split_part(number, '-', 1) || '-' || {UNIT},
        data = jsonb_set(data, '{{unit_number}}', to_jsonb({UNIT})),
        version = version + 1, updated_at = now()''')
    op.execute('ALTER TABLE vehicles ADD CONSTRAINT vehicles_company_number UNIQUE (organization_id, number)')
    op.execute("ALTER TABLE vehicles ADD CONSTRAINT vehicles_unit_required CHECK (length(btrim(coalesce(data->>'unit_number', ''))) BETWEEN 1 AND 50)")
    op.execute("CREATE UNIQUE INDEX vehicles_company_unit ON vehicles (organization_id, upper(btrim(data->>'unit_number')))")


def downgrade():
    op.execute('DROP INDEX vehicles_company_unit')
    op.execute('ALTER TABLE vehicles DROP CONSTRAINT vehicles_unit_required')
    # Preserve published IDs and long unit suffixes rather than recreating random references.
