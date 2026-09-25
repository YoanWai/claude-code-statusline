#!/bin/sh
set -eu

here=$(cd "$(dirname "$0")" && pwd)
claude_dir="$HOME/.claude"
statusline="$claude_dir/hooks/statusline.py"
refresher="$claude_dir/statusline-tips/refresh_tips.py"

mkdir -p "$claude_dir/hooks" "$claude_dir/statusline-tips"
cp "$here/statusline.py" "$statusline"
cp "$here/refresh_tips.py" "$refresher"
chmod +x "$statusline" "$refresher"

python3 "$refresher"

python3 - "$claude_dir/settings.json" "$statusline" <<'EOF'
import json
import sys

settings_path, script_path = sys.argv[1], sys.argv[2]
try:
    with open(settings_path) as settings_file:
        settings = json.load(settings_file)
except FileNotFoundError:
    settings = {}
settings["statusLine"] = {"type": "command", "command": f'python3 "{script_path}"'}
with open(settings_path, "w") as settings_file:
    json.dump(settings, settings_file, indent=2)
    settings_file.write("\n")
EOF

echo "Installed. Restart Claude Code to see the status line."
echo "Schedule the tip refresh with cron or /schedule:"
echo "  python3 $refresher"
