"""Network board order retrieval with full pagination and explicit relationship expansion.

https://roserocket.readme.io/docs/using-boards-in-rose-rocket
https://roserocket.readme.io/reference/get_objects-recordid
Account board IDs, detail paths and mappings require vendor sandbox verification.
"""
from urllib.parse import quote
from . import transport
from app.integrations import client

def orders_resource(board_id: str, detail_paths: list[str], timezone: str):
    from .profiles import Resource
    return Resource(path='/api/v2/platformModel/objects/search',method='POST',adapter='rose_rocket_board',
        body={'boardId':board_id,'timezone':timezone,'filters':[],'filtersOperator':'and'},detail_paths=detail_paths)

def fetch(profile,resource,credentials):
    account=credentials.get('account_id')
    if not account:raise transport.Error('IDENTITY_NOT_VERIFIED')
    headers=transport.headers('rose-rocket',profile,credentials)
    records=[];seen=set();offset=0;expected=None
    for _ in range(client.MAX_PAGES):
        body={**resource.body,'offset':offset,'limit':resource.page_size}
        payload=client.request('POST',transport.target(profile,resource,credentials),headers,body)
        total=payload.get('total') if isinstance(payload,dict) else None
        rows=payload.get('results') if isinstance(payload,dict) else None
        if isinstance(total,bool) or not isinstance(total,int) or total<0 or not isinstance(rows,list) or len(rows)>resource.page_size:
            raise transport.Error('FORMAT_CHANGED')
        if expected is not None and total!=expected:raise transport.Error('COLLECTION_CHANGED')
        expected=total
        for row in rows:
            identity=row.get('id') if isinstance(row,dict) else None
            if not isinstance(identity,str) or not identity or identity in seen:raise transport.Error('PAGINATION_LOOP')
            if row.get('objectKey')!='order' or row.get('orgId')!=account:raise transport.Error('ACCOUNT_CHANGED')
            seen.add(identity)
            path='/api/v2/platformModel/objects/'+quote(identity,safe='')
            detail=client.request('GET',client.url(profile.base_url,path,{'boardId':resource.body['boardId'],
                'paths':','.join(resource.detail_paths)}),headers)
            if not isinstance(detail,dict) or detail.get('id')!=identity or detail.get('objectKey')!='order' or detail.get('orgId')!=account:
                raise transport.Error('ACCOUNT_CHANGED')
            records.append(detail)
        offset+=len(rows)
        if offset==total:return records
        if not rows or offset>total:raise transport.Error('FORMAT_CHANGED')
    raise transport.Error('TOO_MANY_PAGES')
