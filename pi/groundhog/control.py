"""Privileged, fail-closed Ground Hog controller. Never installed/enabled implicitly."""
import base64
from contextlib import closing
from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from build_image import build
from backup_fingerprint import business_fingerprint

ROOT = Path('/var/lib/salonmax-groundhog')
HOME = Path('/home/benparker1984')
BOOT = Path('/boot/firmware')


class Blocked(ValueError):
    pass


def fingerprint(db):
    return business_fingerprint(db)


def verify_receipt(receipt, public_xml, config, current_fingerprint, now=None):
    now = now or datetime.now(timezone.utc)
    xml = ET.fromstring(public_xml)
    numbers = rsa.RSAPublicNumbers(
        int.from_bytes(base64.b64decode(xml.findtext('Exponent')), 'big'),
        int.from_bytes(base64.b64decode(xml.findtext('Modulus')), 'big'))
    payload = base64.b64decode(receipt['payload'], validate=True)
    numbers.public_key().verify(base64.b64decode(receipt['signature'], validate=True), payload,
                                padding.PKCS1v15(), hashes.SHA256())
    data = json.loads(payload)
    if data.get('format') != 'salonmax-pc-backup-receipt' or data.get('version') != 1 or data.get('cloud_status') != 'ok':
        raise Blocked('A complete verified PC backup is required.')
    age = (now - datetime.fromisoformat(data['created_at'].replace('Z', '+00:00'))).total_seconds()
    if not 0 <= age <= 24*3600:
        raise Blocked('The PC backup receipt is older than 24 hours. Run a new backup first.')
    if data.get('pi_serial') != config['pi_serial'] or data.get('sd_cid') != config['sd_cid']:
        raise Blocked('The backup belongs to another Pi or SD card.')
    if not hmac.compare_digest(data.get('business_data_sha256', ''), current_fingerprint):
        raise Blocked('Till business data changed after the backup. Run a new PC backup first.')
    return data


def check_pins(db, request):
    owner = db.execute('select pin_code,role,is_active from staff_users where id=?',
                       (int(request.get('owner_id', 0)),)).fetchone()
    if not owner or str(owner[1]).lower() != 'owner' or not owner[2] or not hmac.compare_digest(str(owner[0]), str(request.get('owner_pin', ''))):
        raise Blocked('Owner PIN confirmation failed.')
    manager = db.execute("select 1 from staff_users where is_active=1 and role in ('manager','owner') and pin_code=?",
                         (str(request.get('manager_pin', '')),)).fetchone()
    if not manager:
        raise Blocked('An active manager or owner PIN is required.')
    if request.get('confirmation') != 'ERASE SD CARD':
        raise Blocked('Explicit full-card erase confirmation is required.')


def check_hardware(config):
    if subprocess.check_output(['/usr/bin/findmnt', '-n', '-o', 'SOURCE', '/'], text=True).strip() != '/dev/mmcblk0p2':
        raise Blocked('The running root filesystem is not the bound Pi SD card.')
    actual = {
        'pi_serial': Path('/sys/firmware/devicetree/base/serial-number').read_text().strip('\x00\n '),
        'sd_cid': Path('/sys/block/mmcblk0/device/cid').read_text().strip(),
        'sectors': int(Path('/sys/block/mmcblk0/size').read_text()),
    }
    if any(actual[k] != config[k] for k in actual):
        raise Blocked('Bound hardware identity or card capacity changed.')
    heartbeat = json.loads((HOME / '.local/share/salonmax-updater/heartbeat.json').read_text())
    if heartbeat.get('hardware_idle') is not True or not 0 <= time.time()-heartbeat.get('timestamp', 0) <= 8:
        raise Blocked('A fresh idle-bed/queue/basket check is required.')


def check_backup(config, db):
    receipt_path = HOME / '.local/share/salonmax-recovery/backup-receipt.json'
    if receipt_path.is_symlink() or receipt_path.stat().st_size > 16384:
        raise Blocked('Invalid backup receipt file.')
    return verify_receipt(json.loads(receipt_path.read_text()),
                          (ROOT / 'pc-signing-public.xml').read_text(), config, fingerprint(db))


def user_services(action):
    subprocess.run(['/usr/sbin/runuser', '-u', 'benparker1984', '--', '/usr/bin/env',
        'XDG_RUNTIME_DIR=/run/user/1000', '/usr/bin/systemctl', '--user', action,
        'salonmax-native-till.service', 'salonmax-profile-sync.timer', 'salonmax-course-sync.timer',
        'salonmax-profile-sync.service', 'salonmax-course-sync.service'], check=True)


def execute(config):
    ticket = json.loads((ROOT / 'pending.json').read_text())
    if not 0 <= time.time()-ticket['at'] <= 90:
        raise Blocked('Erase authorization expired.')
    check_hardware(config)
    database = HOME / 'till-build/till_backoffice.db'
    with closing(sqlite3.connect(database, timeout=10)) as db:
        check_backup(config, db)
    # Build and verify prerequisites before stopping any service or changing boot.
    image = build(config, ticket['nonce'])
    before_config = (BOOT / 'config.txt').read_bytes()
    before_cmdline = (BOOT / 'cmdline.txt').read_bytes()
    (ROOT / 'original-config.txt').write_bytes(before_config)
    (ROOT / 'original-cmdline.txt').write_bytes(before_cmdline)
    stopped = False
    try:
        check_hardware(config)
        stopped = True
        user_services('stop')
        subprocess.run(['/usr/bin/systemctl', 'stop', 'salonmax-backoffice.service'], check=True)
        with closing(sqlite3.connect(database, timeout=10)) as db:
            db.execute('begin exclusive')
            check_backup(config, db)
            image_path = BOOT / 'salonmax-groundhog.img'
            image_path.write_bytes(image)
            with image_path.open('rb') as file:
                os.fsync(file.fileno())
            cmdline = before_cmdline.decode().strip()
            if 'salonmax_groundhog=' in cmdline:
                raise Blocked('Recovery boot is already configured; manual review is required.')
            (BOOT / 'cmdline.txt').write_text(cmdline + ' salonmax_groundhog=' + ticket['nonce'] + '\n')
            (BOOT / 'config.txt').write_bytes(before_config + b'\n[all]\nauto_initramfs=0\ninitramfs salonmax-groundhog.img followkernel\n')
            os.sync()
            subprocess.run(['/usr/bin/systemctl', 'reboot'], check=True)
    except Exception:
        (BOOT / 'config.txt').write_bytes(before_config)
        (BOOT / 'cmdline.txt').write_bytes(before_cmdline)
        os.sync()
        if stopped:
            subprocess.run(['/usr/bin/systemctl', 'start', 'salonmax-backoffice.service'])
            user_services('start')
        raise


def main():
    if os.geteuid() != 0:
        raise Blocked('The protected recovery controller requires its one-time root installation.')
    config = json.loads((ROOT / 'config.json').read_text())
    if config.get('enabled') is not True:
        raise Blocked('Full-card erasure is staged but disabled until the spare-card recovery test is completed.')
    with (ROOT / 'controller.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if sys.argv[1:] == ['--execute']:
            if 'SUDO_UID' in os.environ:
                raise Blocked('Direct execution is not permitted.')
            execute(config)
            return
        if sys.argv[1:]:
            raise Blocked('Unknown controller operation.')
        request = json.loads(sys.stdin.buffer.read(4096))
        check_hardware(config)
        with closing(sqlite3.connect(HOME / 'till-build/till_backoffice.db', timeout=10)) as db:
            receipt = check_backup(config, db)
            if request.get('action') == 'check':
                print(json.dumps({'ok': True, 'backup_at': receipt['created_at']}))
                return
            if request.get('action') != 'arm':
                raise Blocked('Unknown recovery action.')
            attempts_path = ROOT / 'attempts.json'
            attempts = json.loads(attempts_path.read_text()) if attempts_path.exists() else []
            attempts = [t for t in attempts if time.time()-t < 900]
            if len(attempts) >= 5:
                raise Blocked('Too many PIN attempts. Wait 15 minutes.')
            attempts.append(time.time())
            attempts_path.write_text(json.dumps(attempts))
            check_pins(db, request)
            attempts_path.write_text('[]')
        ticket = {'at': time.time(), 'nonce': secrets.token_hex(24)}
        (ROOT / 'pending.json').write_text(json.dumps(ticket))
        subprocess.run(['/usr/bin/systemd-run', '--unit=salonmax-groundhog', '--collect', '--on-active=10s',
            '/usr/bin/python3', '/usr/local/lib/salonmax-groundhog/control.py', '--execute'],
            env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin'}, check=True, capture_output=True)
        print(json.dumps({'ok': True}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'ok': False, 'message': str(error) if isinstance(error, Blocked) else
                         'Recovery prerequisite failed (' + type(error).__name__ + '). Erasure was not authorized.'}))
        sys.exit(1)
