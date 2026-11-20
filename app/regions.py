"""Canada and US address regions: province/state codes, postal/ZIP codes and pricing/dispatch postal areas.

Each helper returns a normalized value or None; nothing here guesses a country from a city name."""
import re

COUNTRIES = ('CA', 'US')
PROVINCES = {'AB': 'alberta', 'BC': 'british columbia', 'MB': 'manitoba', 'NB': 'new brunswick', 'NL': 'newfoundland and labrador',
    'NS': 'nova scotia', 'NT': 'northwest territories', 'NU': 'nunavut', 'ON': 'ontario', 'PE': 'prince edward island',
    'QC': 'quebec', 'SK': 'saskatchewan', 'YT': 'yukon'}
STATES = {'AL': 'alabama', 'AK': 'alaska', 'AZ': 'arizona', 'AR': 'arkansas', 'CA': 'california', 'CO': 'colorado',
    'CT': 'connecticut', 'DE': 'delaware', 'DC': 'district of columbia', 'FL': 'florida', 'GA': 'georgia', 'HI': 'hawaii',
    'ID': 'idaho', 'IL': 'illinois', 'IN': 'indiana', 'IA': 'iowa', 'KS': 'kansas', 'KY': 'kentucky', 'LA': 'louisiana',
    'ME': 'maine', 'MD': 'maryland', 'MA': 'massachusetts', 'MI': 'michigan', 'MN': 'minnesota', 'MS': 'mississippi',
    'MO': 'missouri', 'MT': 'montana', 'NE': 'nebraska', 'NV': 'nevada', 'NH': 'new hampshire', 'NJ': 'new jersey',
    'NM': 'new mexico', 'NY': 'new york', 'NC': 'north carolina', 'ND': 'north dakota', 'OH': 'ohio', 'OK': 'oklahoma',
    'OR': 'oregon', 'PA': 'pennsylvania', 'RI': 'rhode island', 'SC': 'south carolina', 'SD': 'south dakota',
    'TN': 'tennessee', 'TX': 'texas', 'UT': 'utah', 'VT': 'vermont', 'VA': 'virginia', 'WA': 'washington',
    'WV': 'west virginia', 'WI': 'wisconsin', 'WY': 'wyoming'}
ALIASES = {'b.c.': 'BC', 'p.e.i.': 'PE', 'pei': 'PE', 'newfoundland': 'NL', 'québec': 'QC', 'nwt': 'NT', 'yukon territory': 'YT',
    'd.c.': 'DC', 'washington dc': 'DC', 'washington d.c.': 'DC'}
CA_POSTAL = re.compile(r'^([A-Za-z]\d[A-Za-z]) ?(\d[A-Za-z]\d)$')
US_ZIP = re.compile(r'^(\d{5})(?:-?(\d{4}))?$')
# Either postal format inside free text, such as a typed one-line address.
POSTAL_IN_TEXT = re.compile(r'\b[A-Za-z]\d[A-Za-z] ?\d[A-Za-z]\d\b|\b\d{5}(?:-\d{4})?\b(?=\W*(?:USA?|United States)?\W*$)', re.I)
POSTAL_PATTERN = r'^(?:[A-Za-z]\d[A-Za-z] ?\d[A-Za-z]\d|\d{5}(?:-\d{4})?)$'
# Pricing/dispatch areas: a Canadian FSA (V6A), a US 3-digit ZIP prefix (981) or a full 5-digit ZIP (98101).
AREA_PATTERN = r'^(?:[A-Za-z]\d[A-Za-z]|\d{3}|\d{5})$'


def province(value, country=None):
    """Two-letter province (CA) or state (US) code for a code or full name; None when unknown or in another country.
    Province and state codes do not overlap, so the code alone identifies its country."""
    text = (value or '').strip().lower()
    for code, names in (('CA', PROVINCES), ('US', STATES)):
        if country and country != code: continue
        if text.upper() in names: return text.upper()
        alias = ALIASES.get(text)
        if alias in names: return alias
        found = next((key for key, name in names.items() if name == text), None)
        if found: return found
    return None


def postal(value, country=None):
    """Canadian `A1A 1A1` or US `12345`/`12345-6789`; None when the value is neither (or not in `country`)."""
    text = ''.join((value or '').split()).upper()
    if country != 'US' and (match := CA_POSTAL.match(text)): return f'{match[1]} {match[2]}'
    if country != 'CA' and (match := US_ZIP.match(text)): return f'{match[1]}-{match[2]}' if match[2] else match[1]
    return None


def country(code=None, postal_code=None):
    """The country a postal code and/or province/state code identify; None when unknown or when they disagree."""
    by_postal = 'CA' if postal(postal_code, 'CA') else 'US' if postal(postal_code, 'US') else None
    text = (code or '').strip().upper()
    by_code = 'CA' if text in PROVINCES else 'US' if text in STATES else None
    if by_postal and by_code and by_postal != by_code: return None
    return by_postal or by_code


def area(value):
    """Store a pricing/dispatch postal area: a Canadian FSA, or a US 3-digit prefix or 5-digit ZIP (ZIP+4 keeps five)."""
    if not isinstance(value, str): return value
    compact = value.replace(' ', '').upper()
    if re.fullmatch(r'[A-Z]\d[A-Z](?:\d[A-Z]\d)?', compact): return compact[:3]
    if re.fullmatch(r'\d{3}|\d{5}', compact): return compact
    if re.fullmatch(r'\d{5}-?\d{4}', compact): return compact[:5]
    raise ValueError('Use a Canadian three-character postal area, such as V6A, or a US ZIP code or 3-digit ZIP prefix, such as 98101 or 981.')


def postal_key(value):
    """Comparable postal code for prefix matching against areas: no spaces or ZIP+4 hyphen."""
    return (value or '').replace(' ', '').replace('-', '').upper()
