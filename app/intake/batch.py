"""Split an email into stable, independently validated intake rows in one transaction."""
import hashlib
from .models import EmailIntake


def save(db,parent,prepared,settle):
    source_id=parent.message_id
    digest=hashlib.sha256(source_id.encode()).hexdigest()
    total=len(prepared)
    for index,(result,draft,missing) in enumerate(prepared,1):
        row=parent if index==1 else EmailIntake(organization_id=parent.organization_id,
            message_id=f'<email-booking:{digest}:{index}>',mailbox_uid=None,
            received_at=parent.received_at,from_address=parent.from_address,from_name=parent.from_name,
            subject=parent.subject,body=parent.body,attachment_secret=None,sender_verified=parent.sender_verified,
            shipper_id=parent.shipper_id,status='RECEIVED',missing=[],summary='',attempts=0)
        if index>1:
            db.add(row);db.flush()
        row.extraction={'_email_batch':{'source_message_id':source_id,'index':index,'total':total}}
        settle(db,row,result,draft,missing)
    db.flush()
