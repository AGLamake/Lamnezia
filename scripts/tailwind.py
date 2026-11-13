"""Build static/css/app.css with the Tailwind standalone CLI (no Node.js).

The pinned binary is downloaded once into .cache/tailwind/ and verified by SHA-256.
Usage: python scripts/tailwind.py [--watch]
"""

import argparse
import hashlib
import platform
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "4.3.3"
ATTEMPTS = 5
RETRY_PAUSE = 5  # seconds, times the attempt number
SERVER_ERROR = 500  # this status and above are worth a retry
CHECKSUMS = {
    "tailwindcss-linux-arm64": "55fd0b241214eff3de1e8ee4f22796662f2d2e7a49bcfca7477cfd0bac398195",
    "tailwindcss-linux-x64": "dc61b3ac6b8c9ca874c0cc4c57b2409791a64c5540404ca5f5367360babc313a",
    "tailwindcss-macos-arm64": "cdf646702987a743464dff4d9c60fd4480d1c1e73dd819a9a67f1078815dce9d",
    "tailwindcss-macos-x64": "7922e0953f2110c05976e3bf58f14e643d90427575e766b7d433f5f80cbee7e1",
    "tailwindcss-windows-x64.exe": (
        "e0e260ce048014e9268f6237ff18f8ccf02cef521cbd0ae04e82c2cdf7aa3955"
    ),
}
ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "assets" / "css" / "app.css"
OUTPUT = ROOT / "static" / "css" / "app.css"
CACHE = ROOT / ".cache" / "tailwind"

_SYSTEMS = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}
_MACHINES = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}


def binary_name() -> str:
    system = _SYSTEMS.get(platform.system())
    machine = _MACHINES.get(platform.machine().lower())
    name = f"tailwindcss-{system}-{machine}" + (".exe" if system == "windows" else "")
    if name not in CHECKSUMS:
        sys.exit(f"Tailwind CLI is not available for {platform.system()} {platform.machine()}.")
    return name


def ensure_binary() -> Path:
    name = binary_name()
    path = CACHE / VERSION / name
    if path.exists() and _sha256(path) == CHECKSUMS[name]:
        return path
    url = f"https://github.com/tailwindlabs/tailwindcss/releases/download/v{VERSION}/{name}"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _download(url)
    if hashlib.sha256(data).hexdigest() != CHECKSUMS[name]:
        sys.exit(f"Checksum mismatch for {name}; refusing to run it.")
    path.write_bytes(data)
    path.chmod(0o755)
    return path


def _download(url: str) -> bytes:
    """GitHub sometimes answers 5xx or drops the connection: try again after a pause."""
    for attempt in range(1, ATTEMPTS + 1):
        print(f"Downloading {url}")
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                data: bytes = response.read()
                return data
        except urllib.error.HTTPError as error:
            if error.code < SERVER_ERROR or attempt == ATTEMPTS:
                raise
            print(f"HTTP {error.code}, retrying in {RETRY_PAUSE * attempt} s")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            if attempt == ATTEMPTS:
                raise
            print(f"{error}, retrying in {RETRY_PAUSE * attempt} s")
        time.sleep(RETRY_PAUSE * attempt)
    raise AssertionError("unreachable")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true", help="rebuild on template changes")
    args = parser.parse_args()
    command = [str(ensure_binary()), "-i", str(SOURCE), "-o", str(OUTPUT)]
    command.append("--watch" if args.watch else "--minify")
    subprocess.run(command, check=True, cwd=ROOT)


if __name__ == "__main__":
    main()
