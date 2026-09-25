# Contributing

Open an issue for bugs and ideas, or send a pull request. Small focused changes are the easiest to review.

## Run the status line locally

Claude Code feeds the script JSON on stdin. `examples/payload.json` holds a representative payload.

```sh
python3 statusline.py < examples/payload.json
```

Try a few terminal widths to see the responsive layout:

```sh
COLUMNS=80 python3 statusline.py < examples/payload.json
COLUMNS=40 python3 statusline.py < examples/payload.json
```

Point `cwd` in the payload at a real repository to exercise the git segment. The script caches git and usage data under `$TMPDIR/claude-statusline-cache`. Delete that directory to force a fresh read.

## Run the tip refresher against a scratch pool

`refresh_tips.py` writes to `~/.claude/statusline-tips/pool.json`. Point `HOME` at a scratch directory to keep your real pool untouched:

```sh
HOME=/tmp/statusline-test python3 refresh_tips.py
cat /tmp/statusline-test/.claude/statusline-tips/pool.json
```

## Before you open a pull request

- Render the status line before and after your change with the same payload and compare the output.
- Keep the scripts dependency free. Both run on the Python standard library only.
- Match the surrounding style. Names carry the meaning, so comments stay rare.
- Say which platforms you tested on.
