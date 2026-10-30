import base64
import io
import json
import zipfile
from email.message import EmailMessage
from types import SimpleNamespace
import pytest
from app.intake import attachments, mailbox, extract, service


def email_with(data=b'%PDF-1.7\norder',filename='order.pdf',mime='application/pdf'):
    message=EmailMessage();message['From']='orders@example.com';message['Subject']='Please book attached delivery';message.set_content('Please see the attached order.')
    major,minor=mime.split('/');message.add_attachment(data,maintype=major,subtype=minor,filename=filename)
    return message


def test_attachment_only_booking_file_is_preserved_alongside_body():
    parsed=mailbox.parse(email_with().as_bytes())
    assert 'Please see' in parsed.body
    assert parsed.attachments[0]['filename']=='order.pdf'
    assert base64.b64decode(parsed.attachments[0]['data'])==b'%PDF-1.7\norder'
    assert attachments.content(parsed.attachments)[0]['type']=='input_file'

@pytest.mark.parametrize('filename,mime,data',[('order.txt','text/plain',b'Pickup 2 boxes'),('order.csv','text/csv',b'quantity,weight\n2,10'),('order.tsv','text/tab-separated-values',b'quantity\tweight\n2\t10'),('scan.png','image/png',b'\x89PNG\r\n\x1a\nscan'),('photo.jpg','image/jpeg',b'\xff\xd8\xffphoto'),('photo.webp','image/webp',b'RIFF0000WEBPphoto')])
def test_supported_files_and_images_have_explicit_input_types(filename,mime,data):
    files=attachments.collect(email_with(data,filename,mime));assert not files[0].get('error')
    blocks=attachments.content(files)
    assert blocks[-1]['type']==('input_image' if mime.startswith('image/') else 'input_file')

@pytest.mark.parametrize('ext,path',[('.docx','word/document.xml'),('.xlsx','xl/workbook.xml')])
def test_office_files_supported_and_embedded_images_require_review(ext,path):
    def doc(media=False):
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w') as z:
            z.writestr('[Content_Types].xml','<Types/>');z.writestr(path,'<document/>')
            if media:z.writestr('word/media/image1.png',b'picture')
        return output.getvalue()
    assert attachments.valid(doc(),ext)
    assert not attachments.valid(doc(True),ext)


def test_inline_images_are_available_to_ai_and_unknown_files_are_reviewed():
    message=email_with(b'run malware','order.exe','application/octet-stream')
    message.add_attachment(b'\x89PNG\r\n\x1a\nlogo',maintype='image',subtype='png',filename='logo.png',disposition='inline',cid='<logo>')
    files=attachments.collect(message)
    assert len(files)==2 and files[0].get('error') and files[1]['mime']=='image/png'
    assert attachments.collect(email_with(b'not PDF','fake.pdf'))[0].get('error')


def test_attachment_limits_do_not_silently_truncate():
    message=email_with()
    for i in range(5):message.add_attachment(b'file',maintype='text',subtype='plain',filename=f'{i}.txt')
    assert attachments.collect(message)[0].get('error')
    assert attachments.collect(email_with(b'a'*(attachments.MAX_BYTES+1),'big.txt','text/plain'))[0].get('error')


def test_spreadsheet_over_thousand_rows_requires_review():
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as z:
        z.writestr('[Content_Types].xml','<Types/>');z.writestr('xl/workbook.xml','<workbook/>');z.writestr('xl/worksheets/sheet1.xml',b'<row/>'*1001)
    assert not attachments.valid(output.getvalue(),'.xlsx')


def test_ai_receives_body_and_attachment_without_persisting_provider_files(monkeypatch):
    calls=[]
    model=extract.Extraction(is_order_request=False,summary='Not an order',service=None,vehicle_type=None,scheduled_at=None,stops=[],items=[],reference=None)
    def parse(**kwargs):calls.append(kwargs);return SimpleNamespace(output_parsed=extract.ExtractionBatch(summary='Not an order',orders=[model]))
    monkeypatch.setenv('OPENAI_API_KEY','test-only')
    monkeypatch.setattr(extract.openai,'OpenAI',lambda **kwargs:SimpleNamespace(responses=SimpleNamespace(parse=parse)))
    parsed=mailbox.parse(email_with().as_bytes())
    extract.extract({'subject':parsed.subject,'body':parsed.body,'received_at':'2026-10-30T09:00:00Z','attachments':parsed.attachments},dict(time_zone='America/Vancouver',shipper=None,services=[],vehicle_types=[]))
    request=calls[0];blocks=request['input'][0]['content']
    assert blocks[0]['type']=='input_text' and blocks[1]['type']=='input_file'
    assert 'Please see' in blocks[0]['text'] and request['store'] is False
    assert 'untrusted' in request['instructions'] and 'multiple independent orders' in request['instructions']


def test_unsupported_attachment_routes_to_review_without_ai_or_order_creation(monkeypatch):
    row=SimpleNamespace(status='RECEIVED',draft=None,missing=[],error_code=None)
    monkeypatch.setattr(service,'step',lambda *a,**k:None)
    monkeypatch.setattr(service,'_claim',lambda *a:{'email':{'attachments':[{'error':'Unsupported attachment'}]}})
    monkeypatch.setattr(service,'_finish',lambda org,id,apply:apply(None,row))
    monkeypatch.setattr(service,'signal',lambda *a:None)
    monkeypatch.setattr(extract,'extract',lambda *a:pytest.fail('Must not invoke AI on unsupported input'))
    service.process('org','email')
    assert row.status=='NEEDS_REVIEW' and row.missing==['Unsupported attachment']


def test_csv_over_thousand_lines_requires_review_instead_of_truncation():
    assert not attachments.valid(b'row\n'*1001,'.csv')
    assert not attachments.valid(b'row\n'*1001,'.tsv')
