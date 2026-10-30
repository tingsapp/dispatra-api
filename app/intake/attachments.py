"""Bounded attachment intake; never execute file content, macros or external links."""
import base64
import io
import re
import zipfile
from pathlib import PurePosixPath

MAX_FILES=5
MAX_BYTES=4_000_000
TYPES={'.pdf':'application/pdf','.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.webp':'image/webp',
    '.docx':'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.xlsx':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','.csv':'text/csv','.tsv':'text/tab-separated-values','.txt':'text/plain'}


def valid(payload,extension):
    if extension=='.pdf': return payload.lstrip().startswith(b'%PDF-')
    if extension=='.png': return payload.startswith(b'\x89PNG\r\n\x1a\n')
    if extension in ('.jpg','.jpeg'): return payload.startswith(b'\xff\xd8\xff')
    if extension=='.webp': return payload[:4]==b'RIFF' and payload[8:12]==b'WEBP'
    if extension in ('.csv','.tsv'): return len(payload.splitlines()) <= 1000
    if extension not in ('.docx','.xlsx'): return True
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entries=archive.infolist();names={e.filename for e in entries}
            if len(entries)>2000 or sum(e.file_size for e in entries)>20_000_000: return False
            required='word/document.xml' if extension=='.docx' else 'xl/workbook.xml'
            if required not in names or '[Content_Types].xml' not in names: return False
            # Office inputs extract text only. Never silently omit embedded pictures or truncated sheets.
            if any('vbaProject' in n or n.startswith(('word/media/','xl/media/')) for n in names): return False
            if extension=='.xlsx' and any(len(re.findall(rb'<(?:[A-Za-z_][\w.-]*:)?row(?=[\s/>])',archive.read(n)))>1000 for n in names if n.startswith('xl/worksheets/') and n.endswith('.xml')): return False
            return True
    except (zipfile.BadZipFile,RuntimeError,OSError): return False


def collect(message):
    files=[];total=0
    for part in message.walk():
        disposition=part.get_content_disposition();filename=part.get_filename()
        inline_image=part.get_content_maintype()=='image' and bool(part.get('Content-ID'))
        if disposition!='attachment' and not filename and not inline_image: continue
        if not filename and inline_image: filename='inline-image.'+part.get_content_subtype()
        if len(files)>=MAX_FILES: return [{'error':'Attachments exceed the five-file processing limit.'}]
        if part.is_multipart():
            files.append({'error':'An attached message or archive requires review.'});continue
        name=PurePosixPath((filename or 'attachment').replace('\\','/')).name
        name=re.sub(r'[\x00-\x1f\x7f]','',name)[:160]
        extension=PurePosixPath(name).suffix.lower();mime=TYPES.get(extension);payload=part.get_payload(decode=True)
        if mime is None or not payload:
            files.append({'error':'An unsupported or empty attachment requires review.'});continue
        total+=len(payload)
        if total>MAX_BYTES: return [{'error':'Attachments exceed the 4 MB processing limit.'}]
        if not valid(payload,extension):
            files.append({'error':'An unreadable or complex attachment requires review. Send a PDF or clear photo instead.'});continue
        files.append({'filename':name,'mime':mime,'data':base64.b64encode(payload).decode('ascii')})
    return files


def content(files):
    result=[]
    for file in files:
        if file.get('error'): raise ValueError('Attachment requires review')
        data='data:'+file['mime']+';base64,'+file['data']
        if file['mime'].startswith('image/'):
            result.extend([{'type':'input_text','text':'Attached image: '+file['filename']},
                {'type':'input_image','image_url':data,'detail':'high'}])
        else: result.append({'type':'input_file','filename':file['filename'],'file_data':data})
    return result
