"""After publishing a GitHub release, run:  python make_latest.py "short notes"
Writes latest.json (what installed copies check for updates), then commit and push it."""
import hashlib
import json
import re
import sys
from pathlib import Path

version = re.search(r'^VERSION = "([^"]+)"', Path("app.py").read_text(encoding="utf-8"), re.M).group(1)
exe = Path("dist") / f"Transcriber-Setup-{version}.exe"
data = exe.read_bytes()
Path("latest.json").write_text(json.dumps({
    "version": version,
    "url": f"https://github.com/stefanwallach/transcriber/releases/download/v{version}/{exe.name}",
    "sha256": hashlib.sha256(data).hexdigest(),
    "size": len(data),
    "notes": sys.argv[1] if len(sys.argv) > 1 else "",
}, indent=2) + "\n", encoding="utf-8")
print("latest.json ->", version)
