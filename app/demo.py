"""Explicit provisioning of the local demo admin and the persistent demo company. Never run for production."""
import argparse
import json
import os
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.database import context
from app.models import Organization, User
from app.security import hash_password
from app.services import audit
from app.operations.directory import save_driver, save_shipper, save_vehicle
from app.operations.models import Catalog, RateCard
from app.operations.schemas import Address, DriverData, ShipperData, VehicleData
from app.operations.seeding import seed_pricing

DEMO_ID = uuid5(NAMESPACE_URL, 'dispatra:demo-company:v1')
DISPATCHER_ID = uuid5(DEMO_ID, 'dispatcher')
ADMIN_ID = uuid5(NAMESPACE_URL, 'dispatra:demo-admin:v1')
ADMIN_LOGIN = 'admin@example.com'
DEMO_INITIAL_PASSWORD = '123456'


def seed_admin(db):
    """Ensure the installation has its single ADMIN. An existing admin (for example one created by
    app.bootstrap) and any later password change are preserved; only a new account gets the demo password."""
    context(db, platform=True)
    admin = db.scalar(select(User).where(User.role == 'ADMIN').with_for_update())
    if admin is not None:
        return admin, None
    admin = User(id=ADMIN_ID, scope='platform', login_id=ADMIN_LOGIN, role='ADMIN',
        display_name='Demo Admin', password_hash=hash_password(DEMO_INITIAL_PASSWORD))
    db.add(admin)
    db.flush()
    audit(db, admin, 'platform_owner.created', admin.id, None)
    return admin, DEMO_INITIAL_PASSWORD


def seed_demo(db):
    """One transaction; replay service commands without resetting edited records."""
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('dispatra:demo:v1', 0))"))
    admin, admin_password = seed_admin(db)
    context(db, platform=True)
    org = db.scalar(select(Organization).where(Organization.slug == 'demo'))
    owned = db.get(Organization, DEMO_ID)
    if (org and org.id != DEMO_ID) or (owned and owned.slug != 'demo'):
        raise ValueError('The demo identity conflicts with an existing company; nothing was changed.')
    if org is None:
        org = Organization(id=DEMO_ID, slug='demo', name='Dispatra Demo')
        db.add(org)
        db.flush()
    context(db, org.id)
    dispatcher = db.get(User, DISPATCHER_ID)
    password = None
    if dispatcher is None:
        password = DEMO_INITIAL_PASSWORD
        dispatcher = User(id=DISPATCHER_ID, organization_id=org.id, scope=str(org.id),
            login_id='dispatcher@example.com', role='DISPATCHER', password_hash=hash_password(password))
        db.add(dispatcher)
        db.flush()
        audit(db, dispatcher, 'demo.provisioned', org.id, org.id)
    if dispatcher.organization_id != org.id or dispatcher.role != 'DISPATCHER':
        raise ValueError('Demo dispatcher identity is inconsistent; nothing was changed.')
    pricing = seed_pricing(db, dispatcher, org)
    # Fixed pricing allows demo bookings without a paid road-distance provider.
    fixed = db.scalar(select(RateCard).where(RateCard.organization_id == org.id, RateCard.code == 'NBH-DIRECT'))
    vehicle_type = db.scalar(select(Catalog).where(Catalog.organization_id == org.id,
        Catalog.kind == 'VEHICLE_TYPE', Catalog.code == 'veh_1_ton'))
    address = Address(text='123 Demo Street, Vancouver, BC V5Y 1V4, Canada', city='Vancouver',
        province='BC', postal_code='V5Y 1V4')
    shipper = save_shipper(db, dispatcher, ShipperData(name='Demo Shipper', kind='BUSINESS',
        company_name='Demo Shipping Company', email='shipper@example.com', phone='6045550101',
        warehouse=address, rate_card_id=fixed.id), 'demo-v1-shipper-create')
    vehicle = save_vehicle(db, dispatcher, VehicleData(name='Demo Van', unit_number='DEMO-001',
        type_id=vehicle_type.id, plate='DEMO001', province='BC', payload_kg=1000,
        length_cm=300, width_cm=180, height_cm=180, pallet_capacity=2, maximum_stops=20,
        description='Fictional demonstration vehicle.'), 'demo-v1-vehicle-create')
    driver = save_driver(db, dispatcher, DriverData(name='Demo Driver', email='driver@example.com',
        phone='6045550102', address=address, vehicle_id=UUID(vehicle['id']),
        employment='OWNER_OPERATOR', revenue_share_percent=60, fuel_surcharge_share_percent=100),
        'demo-v1-driver-create')
    shipper_user = db.scalar(select(User).where(User.organization_id == org.id, User.shipper_id == UUID(shipper['id'])))
    driver_user = db.scalar(select(User).where(User.organization_id == org.id, User.driver_id == UUID(driver['id'])))
    # Only newly seeded demo identities get the shared demonstration password.
    # Replays preserve subsequent user password changes.
    for account, result in [(shipper_user, shipper), (driver_user, driver)]:
        if result['initial_password'] is not None:
            account.password_hash = hash_password(DEMO_INITIAL_PASSWORD)
            result['initial_password'] = DEMO_INITIAL_PASSWORD
    return {'company': org.slug, 'pricing': pricing, 'accounts': [
        {'role': 'ADMIN', 'portal': 'platform', 'path': '/admin', 'login_id': admin.login_id, 'initial_password': admin_password},
        {'role': 'DISPATCHER', 'portal': 'dispatch', 'path': '/demo/', 'login_id': dispatcher.login_id, 'initial_password': password},
        {'role': 'SHIPPER', 'portal': 'customer', 'path': '/demo/shipper-portal', 'login_id': shipper_user.login_id, 'initial_password': shipper['initial_password']},
        {'role': 'DRIVER', 'portal': 'driver', 'path': '/demo/driver', 'login_id': driver_user.login_id, 'initial_password': driver['initial_password']},
    ]}


def main():
    parser = argparse.ArgumentParser(description='Provision the demo admin, demo company, accounts, vehicle and pricing; preserve existing edits/passwords.')
    parser.add_argument('--credentials-file', type=Path, default=Path('.local/demo-accounts.json'))
    args = parser.parse_args()
    url = os.environ.get('MIGRATION_DATABASE_URL')
    if not url:
        parser.error('Load MIGRATION_DATABASE_URL from your local .env first.')
    engine = create_engine(url)
    with Session(engine) as db, db.begin():
        result = seed_demo(db)
        if any(account['initial_password'] for account in result['accounts']):
            args.credentials_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Exclusive create: never replace the only copy of existing credentials.
            fd = os.open(args.credentials_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, 'w') as output:
                    json.dump(result, output, indent=2)
                    output.write('\n')
                    output.flush()
                    os.fsync(output.fileno())
                db.commit()
            except BaseException:
                args.credentials_file.unlink(missing_ok=True)
                raise
            print(f'Demo accounts created. Private login details: {args.credentials_file.resolve()}')
        else:
            print('Demo accounts already exist; passwords and profile edits were preserved.')
    print(f"Demo pricing: {result['pricing']['created']} missing presets added.")


if __name__ == '__main__':
    main()
