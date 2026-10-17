"""Operator-installed, versioned provider adapters; never returned or editable through the client.

Public vendor docs do not establish every required booking field or installed edition. A catalogue entry is NOT
an approved data adapter. Enabling live imports requires a complete reviewed mapping and sandbox evidence.
"""
import json
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.integrations import fields
from .catalog import BY_ID
from .prepared import prepare

class Resource(BaseModel):
    model_config = ConfigDict(extra='forbid')
    path: str
    method: Literal['GET', 'POST'] = 'GET'
    body: dict | None = None
    records_path: str = ''
    next_path: str | None = None
    next_parameter: str = 'cursor'
    next_kind: Literal['cursor', 'url'] = 'cursor'
    parameters: dict = Field(default_factory=dict)
    adapter: Literal['generic','rose_rocket_board'] = 'generic'
    detail_paths: list[str] = Field(default_factory=list, max_length=50)
    page_size: int = Field(default=50, ge=1, le=200)

class Profile(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider: str
    revision: str = Field(min_length=1, max_length=80)
    api_version: str = Field(min_length=1, max_length=80)
    evidence: str = Field(min_length=1, max_length=500)
    base_url: str
    identity: Resource
    account_id_path: str
    account_label_path: str
    identity_scope: Literal['mapped', 'cartoncloud_tenant'] = 'mapped'
    tenant_id: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    resources: dict[Literal['vehicle', 'driver', 'order'], Resource]
    maps: dict[Literal['vehicle', 'driver', 'order'], dict[str, str]]
    weight_unit: Literal['kg', 'lb']
    dimension_unit: Literal['cm', 'in', 'm']
    currency: str = Field(pattern=r'^[A-Z]{3}$')
    authorize_url: str | None = None
    token_url: str | None = None
    scopes: str = ''
    audience: str | None = None
    # Registered applications and secrets are environment references, never profile contents.
    client_id_env: str | None = None
    client_secret_env: str | None = None
    token_format: Literal['form', 'json'] = 'form'
    token_auth: Literal['body', 'basic'] = 'body'
    session_header: str | None = None
    status_path: str | None = None
    status_method: Literal['POST', 'PATCH', 'PUT'] = 'POST'
    # Outbound status is explicitly translated into the vendor's own field/value format.
    status_field: str | None = None
    status_values: dict[str, str] = Field(default_factory=dict)
    sync_minutes: int = Field(default=5, ge=1, le=1440)

    @model_validator(mode='after')
    def reviewed_contract(self):
        if self.provider not in BY_ID: raise ValueError('Unknown provider')
        if BY_ID[self.provider].auth == 'request_setup': raise ValueError('This provider is awaiting connector preparation')
        if self.identity_scope == 'cartoncloud_tenant' and self.provider != 'cartoncloud':
            raise ValueError('CartonCloud tenant identity is provider-specific')
        if self.identity_scope == 'mapped' and not self.account_id_path:
            raise ValueError('An account identity mapping is required')
        for target in [self.base_url, self.authorize_url, self.token_url]:
            if target:
                p = urlsplit(target)
                if p.scheme != 'https' or not p.hostname or p.username or p.password or p.fragment or p.query:
                    raise ValueError('Profile endpoints require clean HTTPS URLs')
        if not self.resources: raise ValueError('At least one import resource is required')
        for kind, resource in {**self.resources, 'identity': self.identity}.items():
            if not resource.path.startswith('/') or resource.path.startswith('//') or '..' in resource.path or '#' in resource.path:
                raise ValueError('Resource paths must remain under the configured origin')
            if kind == 'identity': continue
            if resource.adapter == 'rose_rocket_board':
                if self.provider != 'rose-rocket' or kind != 'order' or resource.method != 'POST':
                    raise ValueError('Rose Rocket board import is only supported for Rose Rocket orders')
                if resource.path != '/api/v2/platformModel/objects/search' or not resource.body or not resource.body.get('boardId') or not resource.detail_paths:
                    raise ValueError('A reviewed Rose Rocket board and detail paths are required')
                if any(not path or any(c in path for c in ',{}?#') for path in resource.detail_paths):
                    raise ValueError('Detail paths must be separate period-delimited field paths')
            mapping = self.maps.get(kind, {})
            if fields.problems(kind, mapping) or fields.missing(kind, mapping):
                raise ValueError(f'Incomplete {kind} mapping')
        if any(key.lower() in {'authorization', 'x-api-key', 'x-api_key'} for key in self.headers):
            raise ValueError('Credentials must be stored separately')
        if BY_ID[self.provider].auth == 'session' and not self.session_header:
            raise ValueError('Aljex token header must be verified against its provisioned API')
        if self.status_path and (not self.status_path.startswith('/') or '..' in self.status_path):
            raise ValueError('Status path must remain under the configured origin')
        if self.status_path and ('{id}' not in self.status_path or not self.status_field or not self.status_values):
            raise ValueError('Status delivery requires an explicit vendor translation')
        if BY_ID[self.provider].auth == 'oauth' and not all([self.authorize_url, self.token_url, self.client_id_env, self.client_secret_env]):
            raise ValueError('Registered OAuth application is required')
        if BY_ID[self.provider].auth == 'client_credentials' and not self.token_url:
            raise ValueError('Token endpoint is required')
        return self

def installed(provider: str) -> Profile | None:
    """A missing/invalid installation blocks connection; it cannot quietly use guessed defaults."""
    if provider not in BY_ID or BY_ID[provider].auth == 'request_setup': return None
    path = os.environ.get('TMS_CONNECTOR_PROFILES_FILE')
    if not path: return None
    try:
        data = json.loads(Path(path).read_text())
        if not isinstance(data, dict): return None
        found = data.get(provider)
        profile = Profile.model_validate(prepare(provider, found)) if isinstance(found, dict) else None
        if profile and profile.provider != provider: return None
        if profile and BY_ID[provider].auth == 'oauth' and not all(os.environ.get(key or '') for key in [profile.client_id_env, profile.client_secret_env]): return None
        return profile
    except (OSError, ValueError, TypeError): return None
