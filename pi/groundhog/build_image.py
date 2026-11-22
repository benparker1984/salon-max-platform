"""Build a standalone RAM image without mounting/writing any block device."""
import gzip
from pathlib import Path
import re
import stat
import subprocess


def newc(entries):
    output = bytearray()
    for number, (name, mode, data, major, minor) in enumerate(entries + [('TRAILER!!!', 0, b'', 0, 0)], 1):
        encoded = name.encode() + b'\0'
        fields = (number, mode, 0, 0, 1, 0, len(data), 0, 0, major, minor, len(encoded), 0)
        output.extend(b'070701' + ''.join(f'{field:08x}' for field in fields).encode())
        output.extend(encoded)
        output.extend(b'\0' * (-len(output) % 4))
        output.extend(data)
        output.extend(b'\0' * (-len(output) % 4))
    return bytes(output)


def build(config, nonce):
    for value in (config['pi_serial'], config['sd_cid'], nonce):
        if not re.fullmatch('[0-9a-fA-F]{8,64}', value):
            raise ValueError('Invalid bound hardware identity')
    sectors = int(config['sectors'])
    if not 2048 < sectors < 2**40:
        raise ValueError('Invalid SD capacity')
    # The bound Pi must have its SD drivers compiled into its current kernel.
    for module in ('mmc_block', 'sdhci_iproc'):
        result = subprocess.run(['/sbin/modinfo', '-F', 'filename', module], text=True, capture_output=True, check=True)
        if result.stdout.strip() != '(builtin)':
            raise ValueError('RAM eraser requires tested built-in SD drivers')
    busybox = Path('/bin/busybox')
    result = subprocess.run(['/usr/bin/ldd', str(busybox)], text=True, capture_output=True, check=True)
    libraries = set(re.findall(r'(/[^\s()]+)', result.stdout))
    files = {'bin/busybox': busybox.read_bytes(),
             'init': Path(__file__).with_name('init').read_bytes().replace(b'\r\n', b'\n')}
    for library in libraries:
        path = Path(library)
        if path.is_file():
            files[library.lstrip('/')] = path.read_bytes()
    files['groundhog.conf'] = (f'ARMED=yes\nEXPECTED_SERIAL={config["pi_serial"]}\n'
        f'EXPECTED_CID={config["sd_cid"]}\nEXPECTED_SECTORS={sectors}\nNONCE={nonce}\n').encode()
    directories = {'dev', 'proc', 'sys', 'bin'}
    for name in files:
        parent = Path(name).parent
        while str(parent) != '.':
            directories.add(parent.as_posix())
            parent = parent.parent
    entries = [(name, stat.S_IFDIR | 0o755, b'', 0, 0) for name in sorted(directories, key=lambda n:(n.count('/'),n))]
    entries += [('dev/console', stat.S_IFCHR | 0o600, b'', 5, 1), ('dev/null', stat.S_IFCHR | 0o666, b'', 1, 3)]
    entries += [(name, stat.S_IFREG | (0o600 if name == 'groundhog.conf' else 0o755), data, 0, 0) for name,data in sorted(files.items())]
    return gzip.compress(newc(entries), mtime=0)
