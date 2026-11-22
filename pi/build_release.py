"""Build and sign a native release. Keep the private key outside the repository."""
import argparse
import base64
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("version")
    parser.add_argument("--key", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9][0-9A-Za-z._-]{0,63}", args.version):
        parser.error("Use a version such as 2026.11.22.1")
    root = Path(__file__).parent
    source = root / "till-source"
    destination = root / "releases"
    destination.mkdir(exist_ok=True)
    if (destination / (args.version + ".json")).exists():
        parser.error("Published releases are immutable. Choose a new version.")
    stream = io.BytesIO()
    files = [source / "run_native_till.py", *sorted((source / "native_till_app").glob("*.py"))]
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for file in files:
            data = file.read_bytes()
            compile(data, str(file), "exec")
            info = zipfile.ZipInfo(file.relative_to(source).as_posix(), (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(info, data)
    archive = stream.getvalue()
    release = {"version": args.version, "sha256": hashlib.sha256(archive).hexdigest(),
               "schema": 1, "channel": "stable"}
    key = Ed25519PrivateKey.from_private_bytes(args.key.read_bytes())
    signature = key.sign(json.dumps(release, sort_keys=True, separators=(",", ":")).encode())
    (destination / (args.version + ".zip")).write_bytes(archive)
    (destination / (args.version + ".json")).write_text(json.dumps(
        {"release": release, "signature": base64.b64encode(signature).decode()}, indent=2))
    (destination / "stable.json").write_text(json.dumps({"version": args.version}))
    print("Signed native till release", args.version)


if __name__ == "__main__":
    main()
