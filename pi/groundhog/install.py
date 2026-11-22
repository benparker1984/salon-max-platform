"""One-time ROOT installation. Stages disabled unless explicitly enabled after testing.

Does not arm, reboot or erase any disk. Review README.md before running.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from build_image import build


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--enable-after-spare-card-test', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Run the reviewed installer as root on the Pi. No disk is erased by installation.')
    if subprocess.check_output(['/usr/bin/findmnt', '-n', '-o', 'SOURCE', '/'], text=True).strip() != '/dev/mmcblk0p2':
        raise SystemExit('This installer supports only the bound Pi root SD card at /dev/mmcblk0p2.')
    home = Path('/home/benparker1984')
    source_key = home / '.local/share/salonmax-recovery/pc-signing-public.xml'
    if not source_key.is_file() or source_key.is_symlink():
        raise SystemExit('Run a complete PC backup and verify its signing-key fingerprint first.')
    root = Path('/var/lib/salonmax-groundhog')
    root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    destination = Path('/usr/local/lib/salonmax-groundhog')
    destination.mkdir(mode=0o755, exist_ok=True)
    config = {
        'enabled': args.enable_after_spare_card_test,
        'pi_serial': Path('/sys/firmware/devicetree/base/serial-number').read_text().strip('\x00\n '),
        'sd_cid': Path('/sys/block/mmcblk0/device/cid').read_text().strip(),
        'sectors': int(Path('/sys/block/mmcblk0/size').read_text()),
    }
    image = build(config, '0'*48)
    # This unselected image proves packaging prerequisites only, not boot/erase.
    (root / 'packaging-check.img').write_bytes(image)
    for name in ('control.py', 'build_image.py', 'backup_fingerprint.py', 'init'):
        shutil.copyfile(Path(__file__).with_name(name), destination / name)
        os.chmod(destination / name, 0o755)
    shutil.copyfile(source_key, root / 'pc-signing-public.xml')
    os.chmod(root / 'pc-signing-public.xml', 0o600)
    (root / 'config.json').write_text(json.dumps(config, indent=2))
    os.chmod(root / 'config.json', 0o600)
    rule = 'benparker1984 ALL=(root) NOPASSWD: /usr/bin/python3 /usr/local/lib/salonmax-groundhog/control.py\n'
    with tempfile.NamedTemporaryFile(mode='w', dir=root, delete=False) as file:
        file.write(rule)
        path = Path(file.name)
    try:
        subprocess.run(['/usr/sbin/visudo', '-cf', str(path)], check=True)
        shutil.copyfile(path, '/etc/sudoers.d/salonmax-groundhog')
        os.chmod('/etc/sudoers.d/salonmax-groundhog', 0o440)
    finally:
        path.unlink()
    print('Ground Hog installed. Full-card erasure enabled:', config['enabled'])
    print('No boot configuration was changed, no restart requested, no disk erased.')


if __name__ == '__main__':
    main()
