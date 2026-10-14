"""Bounded HTTPS transport. No redirects, response-body logging or cross-origin pagination."""
import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from app.integrations import client
from app.integrations.mapping import read
from .catalog import BY_ID

Error = client.TmsError

def token(profile, values, credentials=None):
    credentials = credentials or {}
    cid = credentials.get('client_id') or os.environ.get(profile.client_id_env or '')
    secret = credentials.get('client_secret') or os.environ.get(profile.client_secret_env or '')
    if not cid or not secret or not profile.token_url: raise Error('ACTIVATION_REQUIRED')
    headers = {'Accept':'application/json'}
    if credentials.get('api_key'): headers['x-api-key'] = credentials['api_key']
    values = {**values, **({'audience':profile.audience} if profile.audience else {})}
    if profile.token_auth == 'basic': headers['Authorization'] = 'Basic ' + base64.b64encode(f'{cid}:{secret}'.encode()).decode()
    else: values |= {'client_id':cid,'client_secret':secret}
    client.check_host(profile.token_url)
    headers['Content-Type'] = 'application/json' if profile.token_format == 'json' else 'application/x-www-form-urlencoded'
    raw = json.dumps(values).encode() if profile.token_format == 'json' else urllib.parse.urlencode(values).encode()
    req = urllib.request.Request(profile.token_url,data=raw,headers=headers,method='POST')
    try:
        with client._opener.open(req,timeout=client.TIMEOUT) as response: body=response.read(client.MAX_BYTES+1)
    except urllib.error.HTTPError as error: raise Error('UNAUTHORIZED' if error.code in [400,401,403] else 'CONNECTION_FAILED') from None
    except (OSError,urllib.error.URLError): raise Error('CONNECTION_FAILED') from None
    if len(body)>client.MAX_BYTES: raise Error('RESPONSE_TOO_LARGE')
    try: result=json.loads(body)
    except ValueError: raise Error('INVALID_JSON') from None
    if not isinstance(result,dict) or not isinstance(result.get('access_token'),str) or not result['access_token']: raise Error('UNAUTHORIZED')
    return result

def headers(provider, profile, credentials):
    kind=BY_ID[provider].auth
    secret=credentials.get('access_token') if kind in ['oauth','client_credentials'] else credentials.get('api_key') if kind!='session' else credentials.get('session_token')
    if not secret: raise Error('UNAUTHORIZED')
    result={'Accept':'application/json','User-Agent':'Dispatra-Connector/1',**profile.headers}
    if kind=='api_key': result |= {'X-Api-Key':secret,'X-Mcld-Tenant':credentials.get('tenant','')}
    elif kind=='session':
        if not profile.session_header: raise Error('ACTIVATION_REQUIRED')
        result[profile.session_header]=secret
    else: result['Authorization']='Bearer '+secret
    return result

def target(profile, resource, credentials, parameters=None):
    path=resource.path
    for key in ('tenant',):
        path=path.replace('{'+key+'}',urllib.parse.quote(credentials.get(key,''),safe=''))
    if '{' in path: raise Error('ACTIVATION_REQUIRED')
    base=credentials.get('base_url') or profile.base_url
    parts=urllib.parse.urlsplit(base)
    if parts.scheme!='https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment: raise Error('HTTPS_REQUIRED')
    return client.url(base,path,parameters or resource.parameters)

def identity(provider,profile,credentials):
    resource=profile.identity
    payload=client.request(resource.method,target(profile,resource,credentials),headers(provider,profile,credentials),resource.body)
    account=read(payload,profile.account_id_path)
    label=read(payload,profile.account_label_path)
    if not isinstance(account,(str,int)) or not str(account): raise Error('IDENTITY_NOT_VERIFIED')
    return str(account),str(label or account)[:160]

def fetch(provider,profile,resource,credentials):
    auth=headers(provider,profile,credentials); endpoint=target(profile,resource,credentials)
    origin=urllib.parse.urlsplit(endpoint)[:2]; seen=set(); rows=[]; body=resource.body
    for _ in range(client.MAX_PAGES):
        if endpoint in seen: raise Error('PAGINATION_LOOP')
        seen.add(endpoint)
        payload=client.request(resource.method,endpoint,auth,body)
        found=read(payload,resource.records_path) if resource.records_path else payload
        if not isinstance(found,list) or any(not isinstance(row,dict) for row in found): raise Error('FORMAT_CHANGED')
        rows.extend(found)
        next_=read(payload,resource.next_path) if resource.next_path else None
        if next_ in (None,'',False): return rows
        if resource.next_kind=='url':
            endpoint=urllib.parse.urljoin(endpoint,str(next_))
            if urllib.parse.urlsplit(endpoint)[:2]!=origin: raise Error('PAGINATION_HOST')
        else:
            if resource.method=='POST':
                body={**(resource.body or {}),resource.next_parameter:next_}
                # Detect repeating cursors independently of the stable POST URL.
                marker=str(next_)
                if marker in seen: raise Error('PAGINATION_LOOP')
                seen.add(marker);seen.discard(endpoint)
            else:endpoint=target(profile,resource,credentials,{**resource.parameters,resource.next_parameter:next_})
    raise Error('TOO_MANY_PAGES')
