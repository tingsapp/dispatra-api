"""Explicit, additive demo pricing seeds. Never reads browser storage."""
import argparse
import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid5, NAMESPACE_URL
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.database import engine, context
from app.models import Organization, User
from app.services import audit
from .models import Catalog, RateCard, Settings
from .schemas import SettingsData, CatalogData, RateData, Zone, WeightBand
from .common import company_lock

SEED = json.loads((Path(__file__).parent / 'seeds/demo_pricing_v1.json').read_text())


def stable_id(org, source):
    return uuid5(NAMESPACE_URL, f'dispatra:{org}:pricing-v1:{source}')


def seed_pricing(db, actor, organization=None):
    organization = organization or db.get(Organization, actor.organization_id)
    from types import SimpleNamespace
    company_lock(db, SimpleNamespace(organization_id=organization.id))
    org_id = organization.id
    existing = db.scalar(select(Settings).where(Settings.organization_id == org_id))
    if not existing:
        billing = SEED['billing']
        data = SettingsData(company_name=organization.name,
            gst_enabled=billing['companyTax']['enabled'], gst_percent=billing['companyTax']['ratePercent'],
            provincial_enabled=billing['companyTax']['provincialEnabled'], provincial_percent=billing['companyTax']['provincialRatePercent'],
            fuel_enabled=billing['fuelSurcharge']['enabled'], fuel_percent=billing['fuelSurcharge']['percent'],
            maximum_active_orders=billing['dispatch']['maxActiveOrdersPerDriver'])
        db.add(Settings(id=stable_id(org_id, 'settings'), organization_id=org_id, data=data.model_dump(mode='json')))
    created = 0
    groups = [('SERVICE', 'services'), ('VEHICLE_TYPE', 'vehicles'), ('ACCESSORIAL', 'accessorials')]
    for kind, group in groups:
        for source in SEED['catalogue'][group]:
            identity = stable_id(org_id, source['id'])
            code = source.get('code', source['id'])
            if db.scalar(select(Catalog.id).where(Catalog.organization_id == org_id, Catalog.code == code, Catalog.kind == kind)):
                continue
            payload = dict(name=source['name'], description=source.get('description', ''),
                amount=source.get('additionalCharge', source.get('baseSurcharge', source.get('rate', 0))),
                taxable=source.get('taxable', True), fuel_eligible=kind == 'SERVICE' or source.get('fuelEligible', False),
                exclusive_vehicle=source.get('exclusiveVehicle', False))
            if kind == 'VEHICLE_TYPE':
                payload.update(payload_kg=source['payloadCapacityKg'], volume_m3=source.get('cargoVolumeCbm'),
                    pallet_capacity=source['palletCapacity'], equipment=['LIFTGATE'] if source.get('hasLiftgate') else [])
            if code == 'LIFTGATE': payload['required_equipment'] = ['LIFTGATE']
            if code == 'HELPER': payload['required_crew'] = 2
            db.add(Catalog(id=identity, organization_id=org_id, kind=kind, code=code,
                active=source['active'], data=CatalogData(**payload).model_dump(mode='json')))
            created += 1
    for source in SEED['pricing']['rateCards']:
        if db.scalar(select(RateCard.id).where(RateCard.organization_id == org_id, RateCard.code == source['code'])): continue
        data = RateData(name=source['name'], method=source['pricingMethod'], base_fee=source['baseFee'],
            included_km=source['includedKm'], per_km=source['kmRate'], fixed_amount=source['fixedAmount'],
            hourly_rate=source['hourlyRate'], minimum_minutes=source['minimumBillableMinutes'],
            increment_minutes=source['billingIncrementMinutes'], minimum_subtotal=source.get('minimumOrderSubtotal') or 0,
            dimensional_divisor=source['dimensionalDivisor'])
        if data.method == 'ZONE':
            data.zones = [Zone(id=z['id'], name=z['name'], postal_codes=z['postalCodes']) for z in SEED['pricing']['zones']]
            data.weight_bands = [WeightBand(from_kg=0, to_kg=Decimal('99') * Decimal('0.45359237'), prices={'zone_1': Decimal('20')})]
        is_default = source['scope'] == 'ORGANIZATION' and source['status'] == 'ACTIVE'
        if is_default and db.scalar(select(RateCard.id).where(RateCard.organization_id == org_id, RateCard.is_default.is_(True))):
            is_default = False
        db.add(RateCard(id=stable_id(org_id, source['id']), organization_id=org_id, code=source['code'],
            active=source['status'] == 'ACTIVE', is_default=is_default, data=data.model_dump(mode='json')))
        created += 1
    db.flush()
    audit(db, actor, 'pricing.seeded', org_id, org_id)
    return {'seed_version': SEED['version'], 'created': created}


def main():
    parser = argparse.ArgumentParser(description='Add demo pricing to one existing company without overwriting edits.')
    parser.add_argument('--company', required=True)
    args = parser.parse_args()
    with Session(engine) as db, db.begin():
        org = db.scalar(select(Organization).where(Organization.slug == args.company))
        if not org: parser.error('Company does not exist; create it through the platform API first.')
        context(db, org.id)
        actor = db.scalar(select(User).where(User.organization_id == org.id, User.role == 'DISPATCHER', User.active.is_(True)))
        if not actor: parser.error('Company has no active dispatcher.')
        print(json.dumps(seed_pricing(db, actor, org)))


if __name__ == '__main__': main()
