"""Decimal pricing. Every result carries the exact settings used for settlement."""
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP, ROUND_CEILING
from uuid import UUID
from fastapi import HTTPException
from sqlalchemy import select
from app.models import now
from .models import Catalog, RateCard
from .schemas import Booking, RateData, CatalogData, SettingsData, Discount
from .common import record, settings, operational_shipper

ZERO = Decimal('0')
CENT = Decimal('0.01')


def money(value):
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def line(group, label, amount, taxable=True, fuel=False):
    return {'group': group, 'label': label, 'amount': str(money(amount)), 'taxable': taxable, 'fuel_eligible': fuel}


def catalog(db, actor, identity, kind):
    row = record(db, Catalog, actor, identity)
    if row.kind != kind or not row.active: raise HTTPException(409, 'Selected ' + kind.lower() + ' is unavailable.')
    return row


def price_context(db, actor, booking):
    _, config = settings(db, actor)
    shipper = operational_shipper(db, actor, booking.shipper_id) if booking.shipper_id else None
    card_id = booking.rate_card_id or (shipper.rate_card_id if shipper else None)
    card = record(db, RateCard, actor, card_id) if card_id else db.scalar(select(RateCard).where(
        RateCard.organization_id == actor.organization_id, RateCard.is_default.is_(True), RateCard.active.is_(True)))
    if not card or not card.active: raise HTTPException(409, 'An active Rate Card is required.')
    service = catalog(db, actor, booking.service_id, 'SERVICE')
    vehicle = catalog(db, actor, booking.vehicle_type_id, 'VEHICLE_TYPE') if booking.vehicle_type_id else None
    accessorials = []
    for selection in booking.accessorials:
        accessory = catalog(db,actor,selection.id,'ACCESSORIAL')
        accessorials.append({**accessory.data,'code':accessory.code,'quantity':str(selection.quantity),'id':str(selection.id)})
    distance, minutes = booking.distance_km, booking.estimated_minutes
    # Road travel only when the card needs it: distance cards need km, hourly cards need service minutes.
    if (card.data['method'] == 'BASE_PLUS_DISTANCE' and distance is None) or (card.data['method'] == 'HOURLY' and minutes is None):
        from .travel import order_travel
        road_km, driving = order_travel(db,actor,booking)
        if distance is None: distance = road_km
        # Hourly estimate = driving time plus each stop's planned service time.
        if minutes is None: minutes = min(10080, driving + sum(stop.service_minutes for stop in booking.stops))
    return {'distance_km': str(distance) if distance is not None else None, 'estimated_minutes': minutes, 'card_id': str(card.id), 'card_version': card.version, 'card': card.data,
        'settings': config.model_dump(mode='json'), 'service': service.data,
        'vehicle': vehicle.data if vehicle else None, 'accessorials': accessorials,
        'discount': shipper.discount if shipper else Discount().model_dump(mode='json')}


def freight_amount(booking, card, actual_minutes=None):
    if card.method == 'FIXED': return money(card.fixed_amount)
    if card.method == 'BASE_PLUS_DISTANCE':
        if booking.distance_km is None: raise HTTPException(409, 'Verified standalone Order distance is required for pricing review.')
        return money(card.base_fee + max(ZERO, booking.distance_km - card.included_km) * card.per_km)
    if card.method == 'HOURLY':
        minutes = actual_minutes if actual_minutes is not None and card.settle_actual else booking.estimated_minutes
        if minutes is None: raise HTTPException(409, 'Billable service minutes are required for hourly pricing.')
        rounded = (Decimal(minutes) / card.increment_minutes).to_integral_value(rounding=ROUND_CEILING) * card.increment_minutes
        return money(max(rounded, Decimal(card.minimum_minutes)) * card.hourly_rate / 60)
    if card.method == 'ZONE':
        stops = {stop.id: stop for stop in booking.stops}
        movements = {}
        for item in booking.items:
            pair = (item.pickup_id, item.delivery_id)
            actual, dimensional = movements.get(pair, (ZERO, ZERO))
            movements[pair] = (actual + item.weight_kg * item.quantity,
                dimensional + item.length_cm * item.width_cm * item.height_cm * item.quantity / card.dimensional_divisor)
        total = ZERO
        for (_, delivery_id), (actual, dimensional) in movements.items():
            weight = max(actual, dimensional)
            postal = stops[delivery_id].address.postal_code.replace(' ', '').upper()
            matches = [(len(code.replace(' ', '')), z.id) for z in card.zones for code in z.postal_codes if postal.startswith(code.replace(' ', '').upper())]
            if not matches: raise HTTPException(409, 'Destination does not match a configured pricing Zone.')
            longest = max(m[0] for m in matches)
            zone_ids = {z for size, z in matches if size == longest}
            if len(zone_ids) != 1: raise HTTPException(409, 'Destination matches conflicting Zones.')
            zone_id = zone_ids.pop()
            bands = [b for b in sorted(card.weight_bands, key=lambda b: b.from_kg) if b.from_kg <= weight <= b.to_kg]
            if not bands or zone_id not in bands[0].prices: raise HTTPException(409, 'No price matches the destination Zone and cargo weight.')
            total += bands[0].prices[zone_id]
        return money(total)
    raise HTTPException(409, 'Imported pricing requires explicit agreed total and tax.')


def calculate(booking: Booking, context, actual_minutes=None, final=False):
    if booking.distance_km is None and context.get('distance_km') is not None:
        booking = booking.model_copy(update={'distance_km':Decimal(context['distance_km'])})
    if booking.estimated_minutes is None and context.get('estimated_minutes') is not None:
        booking = booking.model_copy(update={'estimated_minutes':context['estimated_minutes']})
    card = RateData.model_validate(context['card'])
    config = SettingsData.model_validate(context['settings'])
    lines = []
    if card.method == 'IMPORTED':
        if booking.imported_total is None or booking.imported_tax is None or not booking.external_reference:
            raise HTTPException(409, 'Imported total, supplied tax and source reference are required.')
        if booking.imported_tax > booking.imported_total: raise HTTPException(422, 'Imported tax exceeds total.')
        subtotal = money(booking.imported_total - booking.imported_tax)
        tax = money(booking.imported_tax)
        lines = [line('FREIGHT', 'Agreed imported subtotal', subtotal), line('TAX', 'Supplied tax', tax, False)]
    else:
        freight = freight_amount(booking, card, actual_minutes)
        lines.append(line('FREIGHT', card.name, freight, fuel=True))
        service = CatalogData.model_validate(context['service'])
        if card.apply_service and service.amount: lines.append(line('SERVICE', service.name, service.amount, service.taxable, True))
        if card.apply_vehicle and context['vehicle']:
            vehicle = CatalogData.model_validate(context['vehicle'])
            if vehicle.amount: lines.append(line('VEHICLE', vehicle.name, vehicle.amount, vehicle.taxable, vehicle.fuel_eligible))
        if card.apply_accessorials:
            for item in context['accessorials']:
                # Current simplified catalogue is one flat charge per selected Accessorial per Order.
                lines.append(line('ACCESSORIAL', item['name'], item['amount'], item['taxable'], False))
        if card.apply_fuel and config.fuel_enabled:
            fuel_base = sum((Decimal(l['amount']) for l in lines if l['fuel_eligible']), ZERO)
            if fuel_base: lines.append(line('FUEL', 'Fuel Surcharge', fuel_base * config.fuel_percent / 100))
        discount = Discount.model_validate(context['discount'])
        eligible = [l for l in lines if l['group'] in {'FREIGHT','SERVICE','VEHICLE'}]
        base = sum((Decimal(l['amount']) for l in eligible), ZERO)
        reduction = min(base, money(base * discount.value / 100 if discount.kind == 'PERCENT' else discount.value if discount.kind == 'FIXED' else ZERO))
        taxable = sum((Decimal(l['amount']) for l in lines if l['taxable']), ZERO)
        remaining = reduction
        for index, charge in enumerate(eligible):
            share = remaining if index == len(eligible) - 1 else money(reduction * Decimal(charge['amount']) / base) if base else ZERO
            remaining -= share
            if charge['taxable']: taxable -= share
        if reduction: lines.append(line('DISCOUNT', 'Shipper discount', -reduction, False))
        for adjustment in booking.adjustments:
            lines.append(line('ADJUSTMENT', adjustment.reason, adjustment.amount, adjustment.taxable))
            if adjustment.taxable: taxable += adjustment.amount
        subtotal = sum((Decimal(l['amount']) for l in lines), ZERO)
        if subtotal < 0: raise HTTPException(422, 'Adjustments cannot make the subtotal negative.')
        top_up = max(ZERO, card.minimum_subtotal - subtotal)
        if top_up:
            lines.append(line('MINIMUM', 'Minimum Order subtotal', top_up)); taxable += top_up; subtotal += top_up
        taxable = max(ZERO, taxable)
        tax = ZERO
        for enabled, percent, label in [(config.gst_enabled, config.gst_percent, 'GST/HST'),
                                         (config.provincial_enabled, config.provincial_percent, 'Provincial tax')]:
            if enabled:
                amount = money(taxable * percent / 100)
                tax += amount
                lines.append({**line('TAX', label, amount, False), 'taxable_base': str(money(taxable)), 'rate_percent': str(percent)})
    return {'status': 'PRICED', 'currency': config.currency, 'method': card.method,
        'rate_card_id': context['card_id'], 'rate_card_version': context['card_version'],
        'lines': lines, 'subtotal': str(money(subtotal)), 'tax': str(money(tax)), 'total': str(money(subtotal + tax)),
        'expires_at': (now() + timedelta(days=config.quote_validity_days)).isoformat(),
        'stage': 'FINAL' if final else 'ESTIMATE', 'context': context, 'review_reason': None}


def quote(db, actor, booking):
    return calculate(booking, price_context(db, actor, booking))
