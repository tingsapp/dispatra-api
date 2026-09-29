"""Company-initial public references for Orders, Drivers, Shippers and Vehicles."""
import secrets
import unicodedata
from alembic import op
from sqlalchemy import text

revision = '0012_public_entity_numbers'
down_revision = '0011_email_delivery'
branch_labels = None
depends_on = None


def initial(name, slug):
    for value in (name, slug):
        for char in unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode().upper():
            if 'A' <= char <= 'Z':
                return char
    return 'X'


def allocate(prefix, used):
    digits = 4
    while True:
        start, count = 10 ** (digits - 1), 9 * 10 ** (digits - 1)
        occupied = {int(v[len(prefix):]) for v in used if v.startswith(prefix) and v[len(prefix):].isdigit() and len(v[len(prefix):]) == digits}
        if len(occupied) >= count:
            digits += 1
            continue
        for _ in range(32):
            value = start + secrets.randbelow(count)
            if value not in occupied:
                return prefix + str(value)
        return prefix + str(secrets.choice([v for v in range(start, start + count) if v not in occupied]))


def upgrade():
    op.execute('ALTER TABLE vehicles ADD COLUMN number varchar(50)')
    db = op.get_bind()
    db.execute(text("SELECT set_config('app.platform', 'true', true)"))
    for org in db.execute(text('SELECT id, name, slug FROM organizations')).mappings():
        for table, code in [('orders', 'O'), ('drivers', 'D'), ('customers', 'S'), ('vehicles', 'V')]:
            condition = ' AND id IN (SELECT id FROM shippers)' if table == 'customers' else ''
            rows = list(db.execute(text(f'SELECT id, number FROM {table} WHERE organization_id=:org{condition} ORDER BY created_at, id'), {'org': org['id']}).mappings())
            used = {r['number'] for r in rows if r['number']}
            prefix = f'D{initial(org["name"], org["slug"])}{code}-'
            for row in rows:
                number = row['number'] or ''
                if number.startswith(prefix) and number[len(prefix):].isdigit() and len(number[len(prefix):]) >= 4:
                    continue
                number = allocate(prefix, used)
                used.add(number)
                db.execute(text(f'UPDATE {table} SET number=:number, version=version+1 WHERE id=:id'), {'number': number, 'id': row['id']})
    op.execute('ALTER TABLE vehicles ALTER COLUMN number SET NOT NULL')
    op.execute('ALTER TABLE vehicles ADD CONSTRAINT vehicles_company_number UNIQUE (organization_id, number)')


def downgrade():
    op.execute('ALTER TABLE vehicles DROP CONSTRAINT vehicles_company_number')
    op.execute('ALTER TABLE vehicles DROP COLUMN number')
    # Existing published references remain stable when the schema is downgraded.
