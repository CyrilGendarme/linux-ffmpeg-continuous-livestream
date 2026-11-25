#!/usr/bin/env python3
"""Fix UTF-8 BOM in JSON config files."""

import json
import sys
from pathlib import Path

config_path = Path.home() / "Desktop/linux-ffmpeg-continuous-livestream/config.json"

# Read the file as binary to remove BOM
with open(config_path, 'rb') as f:
    content = f.read()

# Remove UTF-8 BOM if present
if content.startswith(b'\xef\xbb\xbf'):
    content = content[3:]
    print(f"Removed UTF-8 BOM from {config_path}")

# Verify it's valid JSON
try:
    data = json.loads(content)
    print("JSON is valid")
except json.JSONDecodeError as e:
    print(f"ERROR: Invalid JSON: {e}", file=sys.stderr)
    sys.exit(1)

# Write back without BOM using UTF-8 encoding
with open(config_path, 'w', encoding='utf-8') as f:
    json.dump(data, f, indent=2)
    f.write('\n')

print(f"Successfully fixed {config_path}")
