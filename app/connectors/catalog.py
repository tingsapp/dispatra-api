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
    Provider('rose-rocket', 'Rose Rocket', 'RR', 'Connect freight orders and manage your transport operations.', 'oauth', (('base_url', 'Rose Rocket API URL', False), ('client_id', 'Rose Rocket Client ID', False), ('client_secret', 'Rose Rocket Client Secret', True))),
    Provider('cartoncloud', 'CartonCloud', 'CC', 'Connect transport jobs from your CartonCloud logistics workspace.', 'oauth', (('base_url', 'CartonCloud API URL', False), ('tenant', 'CartonCloud Tenant ID', False), ('client_id', 'CartonCloud Client ID', False), ('client_secret', 'CartonCloud Client Secret', True))),
    Provider('truckbase', 'Truckbase', 'TB', 'Connect trucking orders and daily carrier operations.', 'request_setup', (('base_url', 'Truckbase API URL', False), ('api_key', 'Truckbase API token', True))),
    Provider('tailwind', 'Tailwind TMS', 'TW', 'Connect orders across your carrier and brokerage operations.', 'request_setup', (('base_url', 'Tailwind TMS API URL', False), ('api_key', 'Tailwind TMS API token', True))),
    Provider('ascendtms', 'AscendTMS', 'AT', 'Connect transport orders and coordinate your fleet operations.', 'request_setup', (('base_url', 'AscendTMS API URL', False), ('api_key', 'AscendTMS API token', True))),
    Provider('samsara', 'Samsara', 'SA', 'Verify and connect your Samsara account. Fleet data imports are not enabled.', 'bearer', (('api_key', 'Samsara API token', True),)),
)
# Retain saved connections and worker support when a provider leaves the launch catalogue.
LEGACY_PROVIDERS = (
    Provider('truckmate', 'Trimble TruckMate', 'TM', 'Connect your carrier and fleet operations.', 'bearer', (('base_url', 'TruckMate API address', False), ('api_key', 'TruckMate API key', True))),
    Provider('mcleod', 'McLeod', 'MC', 'Connect LoadMaster or PowerBroker.', 'api_key', (('tenant', 'Company identifier', False), ('api_key', 'McLeod API key', True))),
    Provider('aljex', 'Descartes Aljex', 'DA', 'Connect your freight brokerage workspace.', 'session', (('tenant', 'Company identifier', False), ('session_token', 'Aljex access token', True))),
    Provider('turvo', 'Turvo', 'TU', 'Connect shipments from your logistics network.', 'client_credentials', (('client_id', 'Client ID', False), ('client_secret', 'Client secret', True), ('api_key', 'Turvo API key', True))),
)
BY_ID = {item.id: item for item in (*PROVIDERS, *LEGACY_PROVIDERS)}
