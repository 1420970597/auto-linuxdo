import argparse
import json
import sys
from ipaddress import IPv4Address
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from linuxdo.capture import CAPTURE_TOKEN_FILE
from linuxdo.state import Store

ASSETS = Path(__file__).resolve().parent.parent / "extension"


def main() -> None:
    parser = argparse.ArgumentParser(description="Write a private, preconfigured Chrome extension ZIP to stdout")
    parser.add_argument("ip", type=IPv4Address)
    parser.add_argument("--port", type=int, default=8443)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    if sys.stdout.isatty():
        parser.error("redirect stdout to a private ZIP file")

    origin = f"https://{args.ip}:{args.port}"
    manifest = json.loads(ASSETS.joinpath("manifest.json").read_text(encoding="utf-8"))
    manifest["host_permissions"] = [f"https://{args.ip}/*"]
    store = Store()
    key = store.read_token(store.root / CAPTURE_TOKEN_FILE, create=False)
    config = {"url": origin + "/page-capture", "key": key}
    with ZipFile(sys.stdout.buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("extension/manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr("extension/connection.js", "export default " + json.dumps(config) + ";\n")
        for filename in ("background.js", "capture.js", "sync.html", "sync.js", "sync.css"):
            archive.write(ASSETS / filename, "extension/" + filename)


if __name__ == "__main__":
    main()
