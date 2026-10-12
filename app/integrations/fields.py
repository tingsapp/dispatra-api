"""Dispatra fields a TMS record can fill. The web field-map popups mirror these keys; the API is authoritative.

`stops`/`items` name lists in the order payload; `stop.*`/`item.*` are read from each list entry. Value rows
(`stop.pickup_value`, `stop.delivery_value`, `cancelled_value`) hold the TMS's literal value, not a path.
"""
import re

ADDRESS = ('address', 'city', 'province', 'postal_code')
REQUIRED = {
    'driver': {'id', 'name', 'email', 'phone', *ADDRESS},
    'vehicle': {'id', 'unit_number', 'vehicle_type', 'plate', 'province'},
    'order': {'id', 'scheduled_at', 'service', 'total', 'tax', 'shipper.id', 'shipper.name', 'shipper.email',
        'stops', 'stop.type', 'stop.pickup_value', 'stop.delivery_value', *(f'stop.{key}' for key in ADDRESS),
        'items', 'item.weight', 'item.length', 'item.width', 'item.height'},
}
OPTIONAL = {
    'driver': {'vehicle_unit', 'active'},
    'vehicle': {'name', 'payload', 'length', 'width', 'height', 'pallet_capacity', 'make_model', 'year', 'vin', 'active'},
    'order': {'vehicle_type', 'notes', 'status', 'cancelled_value', 'shipper.company_name', 'shipper.phone', *(f'shipper.{key}' for key in ADDRESS),
        'stop.id', 'stop.window_start', 'stop.window_end', 'stop.contact', 'stop.phone', 'stop.instructions',
        'item.pickup_stop', 'item.delivery_stop', 'item.quantity', 'item.pallets', 'item.fragile', 'item.dangerous_goods', 'item.description'},
}
VALUES = {'stop.pickup_value', 'stop.delivery_value', 'cancelled_value'}
PATH = re.compile(r'^\$?\.?[A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*|\[\d+\])*$')


def problems(record: str, mapping: dict[str, str]) -> list[str]:
    """Unknown keys or malformed paths; empty when the map is acceptable to save (required gaps are reported separately)."""
    allowed = REQUIRED[record] | OPTIONAL[record]
    errors = [f'Unknown {record} field: {key}' for key in mapping if key not in allowed]
    errors += [f'Invalid TMS field for {key}' for key, path in mapping.items()
        if key in allowed and (len(path) > 200 or (key not in VALUES and not PATH.match(path)))]
    return errors


def missing(record: str, mapping: dict[str, str]) -> list[str]:
    return sorted(key for key in REQUIRED[record] if not (mapping.get(key) or '').strip())
