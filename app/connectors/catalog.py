"""Public catalogue. Provider access is activated separately from shipping a card."""
from dataclasses import dataclass

@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    initials: str
    description: str
    auth: str
    credential_fields: tuple[tuple[str, str, bool], ...] = ()

PROVIDERS = (
    Provider('rose-rocket', 'Rose Rocket', 'RR', 'Connect your freight orders and transport operations.', 'oauth'),
    Provider('cartoncloud', 'CartonCloud', 'CC', 'Connect transport jobs from your logistics workspace.', 'oauth'),
    Provider('truckmate', 'Trimble TruckMate', 'TM', 'Connect your carrier and fleet operations.', 'bearer', (('base_url', 'TruckMate API address', False), ('api_key', 'TruckMate API key', True))),
    Provider('mcleod', 'McLeod', 'MC', 'Connect LoadMaster or PowerBroker.', 'api_key', (('tenant', 'Company identifier', False), ('api_key', 'McLeod API key', True))),
    Provider('aljex', 'Descartes Aljex', 'DA', 'Connect your freight brokerage workspace.', 'session', (('tenant', 'Company identifier', False), ('session_token', 'Aljex access token', True))),
    Provider('turvo', 'Turvo', 'TU', 'Connect shipments from your logistics network.', 'client_credentials', (('client_id', 'Client ID', False), ('client_secret', 'Client secret', True), ('api_key', 'Turvo API key', True))),
)
BY_ID = {item.id: item for item in PROVIDERS}
