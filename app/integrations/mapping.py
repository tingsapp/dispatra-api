"""Read values out of arbitrary TMS JSON using the company's field map. Never guesses beyond the documented rules."""
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation

TOKENS = re.compile(r'[A-Za-z_][\w-]*|\[(\d+)\]')
LIST_KEYS = ('data', 'items', 'results', 'records', 'drivers', 'vehicles', 'orders', 'rows')
TRUE, FALSE = {'true', 'yes', 'y', '1', 'active', 'enabled'}, {'false', 'no', 'n', '0', 'inactive', 'disabled', 'archived', 'terminated'}


def read(source, path: str | None):
    """`customer.name`, `$.stops[0].address` and similar; missing steps give None."""
    if not path: return None
    value = source
    for token in TOKENS.finditer(path.lstrip('$').lstrip('.')):
        if token.group(1) is not None:
            index = int(token.group(1))
            value = value[index] if isinstance(value, list) and index < len(value) else None
        else: value = value.get(token.group(0)) if isinstance(value, dict) else None
        if value is None: return None
    return value


def records(payload) -> list[dict]:
    """A TMS list response: a bare array, or the first list of objects under a common key or the only list-valued key."""
    if isinstance(payload, list): return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict): return []
    lists = {key: value for key, value in payload.items() if isinstance(value, list)}
    key = next((key for key in LIST_KEYS if key in lists), next(iter(lists)) if len(lists) == 1 else None)
    return [row for row in lists.get(key, []) if isinstance(row, dict)]


def text(value, limit=500) -> str:
    if value is None or isinstance(value, (dict, list)): return ''
    return ' '.join(str(value).split())[:limit]


def boolean(value) -> bool | None:
    if isinstance(value, bool): return value
    word = text(value).lower()
    return True if word in TRUE else False if word in FALSE else None


def number(value) -> Decimal | None:
    if isinstance(value, bool) or value is None or value == '': return None
    try: result = Decimal(str(value).replace(',', '').strip())
    except InvalidOperation: return None
    return result if result.is_finite() else None


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(',', ':')).encode()).hexdigest()


def get(record: dict, mapping: dict, key: str):
    return read(record, mapping.get(key))
