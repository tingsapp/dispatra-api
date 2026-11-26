"""Decimal pricing. Every result carries the exact settings used for the quoted price."""
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP, ROUND_CEILING
from uuid import UUID
from fastapi import HTTPException
from sqlalchemy import select
from app.models import now
from .models import Catalog, RateCard
from .schemas import Booking, RateData, CatalogData, SettingsData, Discount
from .common import record, settings, operational_shipper
from .calendar import delivered_off_day
from . import stop_reasons

ZERO = Decimal('0')
# Added once per Order when a delivery falls on a weekend or company holiday; never selected by hand.
OFF_DAY_CODE = 'HOLIDAY_WEEKEND'
# Added once per Order when a pickup (LIFTGATE) or drop-off (LIFTGATE_DROPOFF) needs a power liftgate; their required equipment binds the vehicle.
LIFTGATE_CODES = {'PICKUP': 'LIFTGATE', 'DROPOFF': 'LIFTGATE_DROPOFF'}
CENT = Decimal('0.01')


def money(value):
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def line(group, label, amount, taxable=True, fuel=False):
    return {'group': group, 'label': label, 'amount': str(money(amount)), 'taxable': taxable, 'fuel_eligible': fuel}


def catalog(db, actor, identity, kind):
    row = record(db, Catalog, actor, identity)
    if row.kind != kind or not row.active: raise HTTPException(409, 'Selected ' + kind.lower() + ' is unavailable.')
    return row


# Small parcel: no pallets, each piece up to 70 lb and the whole order up to 150 lb.
PARCEL_PIECE_KG, PARCEL_TOTAL_KG = Decimal('31.75'), Decimal('68.04')
LOAD_SIZE_NAMES = {'FTL': 'a full truckload', 'LTL': 'less than truckload loads'}


def suggested_load_size(booking):
    """Load size for a booking that names none (email/TMS intake, older clients). FTL is never guessed."""
    weight = sum((item.weight_kg * item.quantity for item in booking.items), ZERO)
    parcel = all(item.pallets == 0 and item.weight_kg <= PARCEL_PIECE_KG for item in booking.items) and weight <= PARCEL_TOTAL_KG
    return 'SMALL_PARCEL' if parcel else 'LTL'


def planned_stop_minutes(db, actor, booking):
    """Stops that state no service minutes take the company's planned stop time for the booking's load size."""
    if all('service_minutes' in stop.model_fields_set for stop in booking.stops): return booking
    _, config = settings(db, actor)
    minutes = {'SMALL_PARCEL': config.stop_minutes_small_parcel, 'LTL': config.stop_minutes_ltl, 'FTL': config.stop_minutes_ftl}[booking.load_size]
    return booking.model_copy(update={'stops': [stop if 'service_minutes' in stop.model_fields_set else
        stop.model_copy(update={'service_minutes': minutes}) for stop in booking.stops]})


def prepare_booking(db, actor, booking):
    """Complete load facts before pricing or saving: a missing load size is suggested, a stated FTL/LTL load refuses vehicle
    types not offered for it, and a full truckload takes the chosen vehicle type's payload and cargo space for lines without
    a weight or size (the load fills the truck)."""
    stated = booking.load_size
    if stated is None: booking = booking.model_copy(update={'load_size': suggested_load_size(booking)})
    booking = planned_stop_minutes(db, actor, booking)
    bay = catalog(db, actor, booking.vehicle_type_id, 'VEHICLE_TYPE').data if booking.vehicle_type_id else None
    # A stated FTL or LTL load only takes vehicle types offered for it; a suggested load size refuses nothing.
    if bay and stated in ('FTL', 'LTL') and stated not in bay.get('load_sizes', ['FTL', 'LTL']):
        raise HTTPException(422, f"{bay['name']} isn’t offered for {LOAD_SIZE_NAMES[stated]}. Choose another vehicle type.")
    if booking.load_size != 'FTL': return booking
    payload = Decimal(str(bay['payload_kg'])) if bay.get('payload_kg') else None
    given = sum((item.weight_kg * item.quantity for item in booking.items if item.weight_kg is not None), ZERO)
    if payload is not None and given > payload:
        raise HTTPException(422, f'This load is heavier than the vehicle type can carry ({payload.normalize():f} kg).')
    blank = [item for item in booking.items if item.weight_kg is None]
    if blank and payload is None:
        raise HTTPException(422, 'Add a payload to this vehicle type before booking a full truckload.')
    sized = all(None not in (i.length_cm, i.width_cm, i.height_cm) for i in booking.items)
    if not sized and not all(bay.get(key) for key in ('length_cm', 'width_cm', 'height_cm')):
        raise HTTPException(422, 'Add cargo length, width and height to this vehicle type before booking a full truckload.')
    size = {} if sized else {key: Decimal(str(bay[key])) for key in ('length_cm', 'width_cm', 'height_cm')}
    share = ((payload - given) / sum(item.quantity for item in blank)).quantize(Decimal('0.000001')) if blank else None
    items = [item.model_copy(update={**({'weight_kg': share} if item.weight_kg is None else {}),
        **({k: v for k, v in size.items() if getattr(item, k) is None})}) for item in booking.items]
    return booking.model_copy(update={'items': items})


def rate_card(db, actor, booking, shipper):
    """The card that prices a booking: an explicit override; else the Shipper's attached card for the booking's load size
    (each card prices one size); else the Shipper's default (first) card; else the company Default."""
    from .directory import shipper_rate_ids
    card_id = booking.rate_card_id
    if not card_id and shipper:
        cards = [card for card in (record(db, RateCard, actor, identity) for identity in shipper_rate_ids(db, actor, shipper)) if card.active]
        card_id = next((card.id for card in cards if card.data.get('load_size', 'LTL') == (booking.load_size or 'LTL')), cards[0].id if cards else None)
    card = record(db, RateCard, actor, card_id) if card_id else db.scalar(select(RateCard).where(
        RateCard.organization_id == actor.organization_id, RateCard.is_default.is_(True), RateCard.active.is_(True)))
    if not card or not card.active: raise HTTPException(409, 'An active Rate Card is required.')
    return card


def price_context(db, actor, booking):
    _, config = settings(db, actor)
    shipper = operational_shipper(db, actor, booking.shipper_id) if booking.shipper_id else None
    card = rate_card(db, actor, booking, shipper)
    service = catalog(db, actor, booking.service_id, 'SERVICE') if booking.service_id else None
    vehicle = catalog(db, actor, booking.vehicle_type_id, 'VEHICLE_TYPE') if booking.vehicle_type_id else None
    package_counts = {
        'FRAGILE': sum(item.quantity for item in booking.items if item.fragile),
        'DG': sum(item.quantity for item in booking.items if item.dangerous_goods),
    }
    accessorials = []
    for selection in booking.accessorials:
        accessory = catalog(db,actor,selection.id,'ACCESSORIAL')
        if accessory.code in package_counts or accessory.code == OFF_DAY_CODE: continue
        accessorials.append({**accessory.data,'code':accessory.code,'quantity':str(selection.quantity),'id':str(selection.id)})
    for code, quantity in package_counts.items():
        if quantity == 0: continue
        accessory = db.scalar(select(Catalog).where(Catalog.organization_id == actor.organization_id,
            Catalog.kind == 'ACCESSORIAL', Catalog.code == code, Catalog.active.is_(True)))
        if not accessory: raise HTTPException(409, f'{code} package handling Accessorial is unavailable.')
        accessorials.append({**accessory.data, 'code': code, 'quantity': str(quantity), 'id': str(accessory.id)})
    # Pickups and drop-offs that need a power liftgate add their own Liftgate charge once per Order; a company without one bills nothing extra.
    for kind, code in LIFTGATE_CODES.items():
        if not any(stop.liftgate and stop.kind == kind for stop in booking.stops) or any(item['code'] == code for item in accessorials): continue
        accessory = db.scalar(select(Catalog).where(Catalog.organization_id == actor.organization_id,
            Catalog.kind == 'ACCESSORIAL', Catalog.code == code, Catalog.active.is_(True)))
        if accessory: accessorials.append({**accessory.data, 'code': code, 'quantity': '1', 'id': str(accessory.id)})
    if delivered_off_day(config, booking):
        # A company that deactivated or deleted the charge does not bill it.
        accessory = db.scalar(select(Catalog).where(Catalog.organization_id == actor.organization_id,
            Catalog.kind == 'ACCESSORIAL', Catalog.code == OFF_DAY_CODE, Catalog.active.is_(True)))
        if accessory: accessorials.append({**accessory.data, 'code': OFF_DAY_CODE, 'quantity': '1', 'id': str(accessory.id)})
    distance, minutes = booking.distance_km, booking.estimated_minutes
    # Road travel only when the card needs it: distance cards need km, hourly cards need service minutes.
    if (card.data['method'] == 'BASE_PLUS_DISTANCE' and distance is None) or (card.data['method'] == 'HOURLY' and minutes is None):
        from .travel import order_travel
        road_km, driving = order_travel(db,actor,booking)
        if distance is None: distance = road_km
        # Hourly estimate = driving time plus each stop's planned service time.
        if minutes is None: minutes = min(10080, driving + sum(stop.service_minutes for stop in booking.stops))
    return {'distance_km': str(distance) if distance is not None else None, 'estimated_minutes': minutes, 'card_id': str(card.id), 'card_version': card.version,
        'card': {key:value for key,value in card.data.items() if key != 'settle_actual'},
        'package_handling_unit_pricing': True,
        'settings': config.model_dump(mode='json'), 'service': service.data if service else None,
        'vehicle': vehicle.data if vehicle else None, 'accessorials': accessorials,
        'discount': shipper.discount if shipper else Discount().model_dump(mode='json')}


def freight_amount(booking, card):
    if card.method == 'FIXED': return money(card.fixed_amount)
    if card.method == 'BASE_PLUS_DISTANCE':
        if booking.distance_km is None: raise HTTPException(409, 'Verified standalone Order distance is required for pricing review.')
        return money(card.base_fee + max(ZERO, booking.distance_km - card.included_km) * card.per_km)
    if card.method == 'HOURLY':
        minutes = booking.estimated_minutes
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


def calculate(booking: Booking, context):
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
        freight = freight_amount(booking, card)
        lines.append(line('FREIGHT', card.name, freight, fuel=True))
        service = CatalogData.model_validate(context['service']) if context.get('service') else None
        if card.apply_service and service and service.amount: lines.append(line('SERVICE', service.name, service.amount, service.taxable, True))
        if card.apply_vehicle and context['vehicle']:
            vehicle = CatalogData.model_validate(context['vehicle'])
            if vehicle.amount: lines.append(line('VEHICLE', vehicle.name, vehicle.amount, vehicle.taxable, vehicle.fuel_eligible))
        if card.apply_accessorials:
            for item in context['accessorials']:
                # Fragile and DG follow package quantities; other current Accessorials remain flat per order.
                package_handling = context.get('package_handling_unit_pricing', False) and item.get('code') in {'FRAGILE', 'DG'}
                charge = Decimal(str(item['amount'])) * Decimal(str(item['quantity'])) if package_handling else item['amount']
                label = f"{item['name']} ({item['quantity']} package{'s' if Decimal(str(item['quantity'])) != 1 else ''})" if package_handling else item['name']
                lines.append(line('ACCESSORIAL', label, charge, item['taxable'], False))
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
        'stage': 'ESTIMATE', 'context': context, 'review_reason': None}


def quote(db, actor, booking):
    booking = prepare_booking(db, actor, booking)
    return calculate(booking, price_context(db, actor, booking))


def retain_price(db, order):
    """Preserve the previous price snapshot on an explicit Order revision."""
    from .models import PricingRevision
    db.add(PricingRevision(organization_id=order.organization_id, order_id=order.id,
        order_version=order.version, snapshot=order.pricing))


def blocks_over(minutes_free, arrived, completed):
    """Whole minutes past a stop's free time and the started blocks they make; each block is `minutes_free` long."""
    over = int((completed - arrived).total_seconds() // 60) - minutes_free
    return (over, -(-over // minutes_free)) if over > 0 else (0, 0)


def stop_reasons_by_stop(db, actor, order):
    """Driver reasons per stop from stop reports and answered progress questions; older reports read from their kind."""
    from app.order_watch.models import Incident, Question
    from .models import Issue
    result = {}
    for issue in db.scalars(select(Issue).where(Issue.organization_id == actor.organization_id, Issue.order_id == order.id, Issue.stop_id.is_not(None))):
        result.setdefault(issue.stop_id, []).extend(issue.reasons or [stop_reasons.LEGACY_KIND.get(issue.kind, 'OTHER')])
    # The driver who completes the Order cannot read incidents under RLS; their answered questions still count.
    from .orders import company_scope
    with company_scope(db, actor):
        answered = db.execute(select(Question, Incident.stop_id).join(Incident, Incident.id == Question.incident_id).where(
            Question.organization_id == actor.organization_id, Question.order_id == order.id, Question.status == 'ANSWERED', Incident.stop_id.is_not(None))).all()
    for question, stop_id in answered:
        result.setdefault(stop_id, []).extend(question.reasons or ([question.reason] if question.reason else []))
    return {stop: [r for r in dict.fromkeys(reasons) if r != 'NO_PROBLEM'] for stop, reasons in result.items()}


def apply_extra_time(db, actor, order):
    """On completion, price Extra time per driver-confirmed stop that ran past the free time. A stop whose driver
    reported a carrier reason is EXCLUDED; every other stop is CHARGED until a dispatcher waives it.
    The rule is the one saved in the Order's price; Orders priced before the rule, imported prices and
    stops closed by a dispatcher override are not charged."""
    from .models import OrderStop, RouteStop
    pricing = order.pricing or {}
    rule = (pricing.get('context') or {}).get('settings') or {}
    if pricing.get('status') != 'PRICED' or pricing.get('method') == 'IMPORTED' or 'extra_time_minutes' not in rule: return
    if pricing.get('extra_time') or any(l['group'] == 'EXTRA_TIME' for l in pricing['lines']): return
    minutes_free, amount = int(rule['extra_time_minutes']), Decimal(str(rule['extra_time_amount']))
    if not amount: return
    visits = db.execute(select(RouteStop, OrderStop).join(OrderStop, OrderStop.id == RouteStop.stop_id).where(
        RouteStop.organization_id == actor.organization_id, OrderStop.order_id == order.id, RouteStop.status == 'COMPLETED',
        RouteStop.arrived_at.is_not(None), RouteStop.completed_at.is_not(None))).all()
    names = stop_names(order)
    reasons = stop_reasons_by_stop(db, actor, order)
    stops = []
    for visit, stop in sorted(visits, key=lambda row: row[0].arrived_at):
        if (visit.movements or {}).get('completed_by') == 'DISPATCHER': continue
        over, blocks = blocks_over(minutes_free, visit.arrived_at, visit.completed_at)
        if not blocks: continue
        said = reasons.get(stop.id, [])
        side = stop_reasons.stop_responsibility(said) if said else 'UNDECIDED'
        stops.append({'stop_id': str(stop.id), 'stop': names.get(str(stop.id), stop.kind.title()), 'minutes_over': over,
            'amount': str(money(blocks * amount)), 'reasons': [stop_reasons.label(r) for r in said], 'responsibility': side,
            'status': 'EXCLUDED' if side == 'CARRIER' else 'CHARGED', 'decided_by_dispatcher': False})
    if not stops: return
    retain_price(db, order)
    order.pricing = priced_extra_time({**pricing, 'extra_time': {'minutes_free': minutes_free, 'amount': str(amount), 'stops': stops}})


def decide_extra_time(db, actor, identity, data, key):
    """Dispatcher waives or charges one stop's Extra time on a completed Order; the previous price is kept as a revision."""
    from .common import command, company_lock, version, changed
    from .models import Order
    from .orders import visible_order
    def run():
        company_lock(db, actor)
        if actor.role != 'DISPATCHER': raise HTTPException(403, 'Dispatcher access required.')
        order = visible_order(db, actor, identity, True); version(order, data.version)
        breakdown = (order.pricing or {}).get('extra_time')
        if order.status != 'COMPLETED' or not breakdown: raise HTTPException(409, 'This Order has no Extra time to review.')
        stop = next((s for s in breakdown['stops'] if s['stop_id'] == str(data.stop_id)), None)
        if stop is None: raise HTTPException(404, 'This stop has no Extra time.')
        status = 'CHARGED' if data.charge else 'WAIVED'
        if stop['status'] == status: return orders_view(order, actor)
        retain_price(db, order)
        stops = [{**s, 'status': status, 'decided_by_dispatcher': True} if s is stop else s for s in breakdown['stops']]
        order.pricing = priced_extra_time({**order.pricing, 'extra_time': {**breakdown, 'stops': stops}})
        changed(db, actor, order, 'order.updated')
        return orders_view(order, actor)
    return command(db, actor, key, f'extra-time:{identity}:{data.stop_id}', data.model_dump(mode='json'), run)


def orders_view(order, actor):
    from .orders import order_view
    return order_view(order, actor)


def stop_names(order):
    """Pickup 1, Drop-off 2… in booked stop order."""
    counts, names = {}, {}
    for stop in order.facts.get('stops', []):
        kind = 'Pickup' if stop['kind'] == 'PICKUP' else 'Drop-off'
        counts[kind] = counts.get(kind, 0) + 1
        names[stop['id']] = f'{kind} {counts[kind]}'
    return names


def priced_extra_time(pricing):
    """Rebuild the Extra time line from the CHARGED stops of the breakdown."""
    charged = [s for s in pricing['extra_time']['stops'] if s['status'] == 'CHARGED']
    total = sum((Decimal(s['amount']) for s in charged), ZERO)
    label = 'Extra time (' + ', '.join(f"{s['stop']} {s['minutes_over']} min" for s in charged) + ')' if charged else 'Extra time'
    return with_extra_time(pricing, total, label)


def with_extra_time(pricing, charge, label):
    """Replace the Extra time line, keeping the other lines; the minimum top-up and taxes follow the new subtotal."""
    old_extra = sum((Decimal(l['amount']) for l in pricing['lines'] if l['group'] == 'EXTRA_TIME'), ZERO)
    lines = [l for l in pricing['lines'] if l['group'] not in {'MINIMUM', 'TAX', 'EXTRA_TIME'}]
    old_top_up = sum((Decimal(l['amount']) for l in pricing['lines'] if l['group'] == 'MINIMUM'), ZERO)
    if charge:
        at = next((i for i, l in enumerate(lines) if l['group'] in {'DISCOUNT', 'ADJUSTMENT'}), len(lines))
        lines.insert(at, line('EXTRA_TIME', label, charge))
    subtotal = sum((Decimal(l['amount']) for l in lines), ZERO)
    top_up = max(ZERO, Decimal(str(pricing['context']['card'].get('minimum_subtotal') or 0)) - subtotal)
    if top_up: lines.append(line('MINIMUM', 'Minimum Order subtotal', top_up)); subtotal += top_up
    tax = ZERO
    for old in (l for l in pricing['lines'] if l['group'] == 'TAX'):
        base = money(Decimal(old['taxable_base']) - old_top_up - old_extra + charge + top_up)
        amount = money(base * Decimal(old['rate_percent']) / 100)
        tax += amount
        lines.append({**old, 'amount': str(amount), 'taxable_base': str(base)})
    return {**pricing, 'lines': lines, 'subtotal': str(money(subtotal)), 'tax': str(money(tax)), 'total': str(money(subtotal + tax))}
