#!/usr/bin/python3
"""Database-independent watchdog: bounded recovery, durable deduplication, SMTP escalation."""
from email.message import EmailMessage
from http import client as http_client
import fcntl
import json
import os
from pathlib import Path
import re
import smtplib
import ssl
import socket
import shutil
import subprocess
import time
from urllib.request import urlopen, Request

STATE = Path('/var/lib/dispatra-watchdog')
SERVICES = tuple('dispatra-' + name + '.service' for name in ('api','intake','dispatch','operational','technical',
    'email','verification','integrations','connectors','push','company-billing','code-repair')) + ('caddy.service',)


def web(path, method='GET'):
    # Verify the real domain certificate and Caddy routing over loopback: no recurring cloud egress.
    connection = http_client.HTTPSConnection('dispatra.com', timeout=8)
    connection.sock = ssl.create_default_context().wrap_socket(
        socket.create_connection(('127.0.0.1', 443), timeout=8), server_hostname='dispatra.com')
    try:
        connection.request(method, path, headers={'User-Agent':'Dispatra availability watchdog'})
        response = connection.getresponse()
        return response.status, response.read(2_000_000).decode(errors='replace')
    finally: connection.close()


def http(url, kind):
    try:
        if kind == 'html':
            status, body = web(url.removeprefix('https://dispatra.com'))
            assets = re.findall(r'(?:src|href)="(/assets/[^"?]+\.(?:js|css))"', body)
            return status == 200 and bool(assets) and all(web(asset, 'HEAD')[0] == 200 for asset in assets[:3])
        with urlopen(Request(url, headers={'User-Agent':'Dispatra availability watchdog'}), timeout=8) as response:
            body = response.read(2_000_000).decode(errors='replace')
            if response.status != 200: return False
            if kind == 'ready': return json.loads(body).get('status') == 'ready'
            return True
    except (OSError, ValueError, http_client.HTTPException): return False


def service(name, action='is-active'):
    try:
        return subprocess.run(['/usr/bin/systemctl',action,name], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired): return False


def notify(keys):
    message = EmailMessage()
    message['From'] = os.environ['SMTP_FROM']; message['To'] = os.environ['TECHNICAL_ALERT_EMAIL']
    message['Subject'] = 'Dispatra: unresolved live application problem'
    message.set_content('The live Technical watchdog could not restore normal operation within its recovery window.\n'
        'Checks needing attention: ' + ', '.join(keys) + '\nReview the live Monitor or server services. '
        'No customer data or logs are included.\nhttps://dispatra.com/admin/monitor')
    port = int(os.environ.get('SMTP_PORT','465'))
    if port == 465:
        connection = smtplib.SMTP_SSL(os.environ['SMTP_HOST'],port,timeout=20,context=ssl.create_default_context())
    else:
        connection = smtplib.SMTP(os.environ['SMTP_HOST'],port,timeout=20)
        connection.starttls(context=ssl.create_default_context())
    with connection:
        connection.login(os.environ['SMTP_USERNAME'],os.environ['SMTP_APP_PASSWORD'])
        connection.send_message(message)


def decisions(previous, checks, timestamp):
    incidents = {}
    for key, ok in checks.items():
        if ok: continue
        incidents[key] = previous.get(key, {'since':timestamp,'attempted':False,'notified':False})
    due = [key for key, value in incidents.items() if timestamp-value['since'] >= 180 and not value['notified']]
    return incidents, due


def release_in_progress():
    try:
        with (STATE/'release.lock').open('r') as lock:
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: return True
        return False
    except FileNotFoundError: return False


def cycle():
    STATE.mkdir(exist_ok=True)
    try: previous = json.loads((STATE/'state.json').read_text())
    except (OSError, ValueError): previous = {}
    timestamp = time.time()
    try: lease = json.loads((STATE/'maintenance.json').read_text())
    except (OSError, ValueError): lease = {}
    if timestamp < lease.get('until',0) < timestamp+181:
        # Do not claim healthy during maintenance; the lease has a fixed recovery deadline.
        return
    monitored = [name for name in SERVICES if name != 'dispatra-code-repair.service'
        or os.environ.get('TECHNICAL_PRODUCTION_CODE_REPAIR') == '1']
    checks = {name: service(name) for name in monitored}
    checks['database_and_api'] = http('http://127.0.0.1:8000/ready', 'ready')
    checks['public_web_and_assets'] = http('https://dispatra.com/', 'html')
    checks['public_login'] = http('https://dispatra.com/admin/login', 'html')
    checks['disk_capacity'] = shutil.disk_usage(STATE).free >= 1024**3
    incidents, _ = decisions(previous.get('incidents',{}), checks, timestamp)
    busy = release_in_progress()
    for key, incident in incidents.items():
        if key in SERVICES and not incident['attempted'] and not busy:
            # Only start an inactive process. Never kill an active business worker or reset a database.
            incident['attempted'] = True
            service(key, 'start')
    incidents, due = decisions(incidents, checks, timestamp)
    if due:
        try:
            notify(due)
            for key in due: incidents[key]['notified'] = True
        except Exception: pass  # Keep alert pending and retry; complete server/SMTP outages need an external channel.
    state = {'at':timestamp, 'ok':all(checks.values()), 'checks':checks, 'incidents':incidents}
    temp = STATE/'state.next'; temp.write_text(json.dumps(state)); temp.chmod(0o644); temp.replace(STATE/'state.json')


if __name__ == '__main__':
    while True:
        try: cycle()
        except Exception as error: print('Watchdog check failed: ' + type(error).__name__,flush=True)
        time.sleep(30)
