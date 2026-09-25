#!/usr/bin/env python3
"""Claude Code status line.

Line 1: ◆ Model ✦ effort ┈ directory ┈ branch ✓ ↑1 ↓2 ┈ ▰▰▰▰▱▱▱ 42% 84k/200k ⟳ 30%
Line 2: ⏱ duration ◎ $cost ┈ ⚡ 5h% ↯ 7d% ┈ ▲ added ▼ removed +staged ~unstaged ┈ ⇄ #PR✓ ┈ ▸ task
Line 3: ✧ rotating tip read from ~/.claude/statusline-tips/pool.json
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

GIT_CACHE_TTL = 0.8
USAGE_CACHE_TTL = 300
STALE_USAGE_TTL = 86400
PR_DISPLAY_MAX = 4
CACHE_DIR = Path(tempfile.gettempdir()) / "claude-statusline-cache"
SHOW_STAGED_UNSTAGED_COUNTS = True
BAR_WIDTH = 15
MAX_REPO_WIDTH = 20
MAX_BRANCH_WIDTH = 25
MAX_TASK_WIDTH = 35
TIPS_POOL = Path.home() / ".claude" / "statusline-tips" / "pool.json"
TIP_ROTATE_SECONDS = 45


def _fg(red, green, blue):
    return f"\x1b[38;2;{red};{green};{blue}m"


RESET = "\x1b[0m"
BOLD = "\x1b[1m"

FROST = _fg(136, 192, 208)
FROST_DEEP = _fg(129, 161, 193)
GREEN = _fg(163, 190, 140)
YELLOW = _fg(235, 203, 139)
ORANGE = _fg(208, 135, 112)
RED = _fg(191, 97, 106)
PURPLE = _fg(180, 142, 173)
TEAL = _fg(94, 199, 183)
TEAL_GREEN = _fg(143, 188, 187)
MUTED = _fg(76, 86, 106)
MUTED_LT = _fg(100, 112, 134)

SEP = f"  {MUTED}┈{RESET}  "
ACCENT_GLYPH = "▎"
ACCENT_COLORS = [FROST, FROST_DEEP, MUTED]
ACCENT_WIDTH = 2

_MODEL_TIERS = {
    "opus": ("◆", PURPLE),
    "sonnet": ("◇", FROST),
    "haiku": ("○", GREEN),
}

_EFFORT_COLORS = {
    "low": GREEN,
    "medium": YELLOW,
    "high": ORANGE,
    "xhigh": PURPLE,
    "max": RED,
}

_BAR_STOPS = [
    (0.0, 163, 190, 140),
    (0.35, 235, 203, 139),
    (0.65, 208, 135, 112),
    (1.0, 191, 97, 106),
]

_PR_STATE_ICONS = {
    "approved": ("✓", GREEN),
    "changes_requested": ("✗", RED),
    "pending": ("●", YELLOW),
    "draft": ("◌", MUTED_LT),
}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _accent(line_index, content):
    color = ACCENT_COLORS[min(line_index, len(ACCENT_COLORS) - 1)]
    return f"{color}{ACCENT_GLYPH}{RESET} {content}"


def _model_icon(model_name, thinking):
    shape, tier_color = "●", MUTED
    lower_name = (model_name or "").lower()
    for tier, (tier_shape, color) in _MODEL_TIERS.items():
        if tier in lower_name:
            shape, tier_color = tier_shape, color
            break
    if thinking is True:
        color = PURPLE
    elif thinking is False:
        color = MUTED
    else:
        color = tier_color
    return f"{color}{shape}{RESET}"


def _effort_chip(level, thinking_on):
    if thinking_on is False:
        return f"{MUTED}✦ off{RESET}"
    if not level:
        return None
    color = _EFFORT_COLORS.get(level, FROST)
    return f"{color}✦ {level}{RESET}"


def _fmt_tokens(count):
    if count is None or count <= 0:
        return "0"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1000:
        return f"{count / 1000:.0f}k"
    return str(count)


def _interp_color(position):
    position = max(0.0, min(1.0, position))
    for index in range(len(_BAR_STOPS) - 1):
        start, red0, green0, blue0 = _BAR_STOPS[index]
        end, red1, green1, blue1 = _BAR_STOPS[index + 1]
        if position <= end:
            fraction = (position - start) / (end - start) if end > start else 0
            return (
                int(red0 + (red1 - red0) * fraction),
                int(green0 + (green1 - green0) * fraction),
                int(blue0 + (blue1 - blue0) * fraction),
            )
    return _BAR_STOPS[-1][1:]


def _pct_color(pct):
    if pct < 50:
        return GREEN
    if pct < 70:
        return YELLOW
    if pct < 85:
        return ORANGE
    return RED


def _gradient_bar(pct, max_tokens):
    pct = max(0, min(100, pct))
    filled = round(pct / 100 * BAR_WIDTH)
    bar = ""
    for index in range(BAR_WIDTH):
        if index < filled:
            red, green, blue = _interp_color(index / max(BAR_WIDTH - 1, 1))
            bar += f"{_fg(red, green, blue)}▰"
        else:
            bar += f"{MUTED}▱"
    used_tokens = int(pct / 100 * max_tokens)
    return (
        f"{bar}{RESET} {_pct_color(pct)}{pct:.0f}%{RESET} "
        f"{MUTED_LT}{_fmt_tokens(used_tokens)}/{_fmt_tokens(max_tokens)}{RESET}"
    )


def _visible_len(text):
    return len(_ANSI_RE.sub("", text))


def _truncate(text, max_width):
    if len(text) <= max_width:
        return text
    return text[: max_width - 1] + "…"


def _fit_from_left(sections, sep, max_width):
    """Drop sections from the left until the line fits."""
    for start in range(len(sections)):
        line = sep.join(sections[start:])
        if _visible_len(line) <= max_width:
            return line
    return sections[-1] if sections else ""


def _fit_from_right(sections, sep, max_width):
    """Drop sections from the right until the line fits."""
    for end in range(len(sections), 0, -1):
        line = sep.join(sections[:end])
        if _visible_len(line) <= max_width:
            return line
    return ""


def _format_duration(milliseconds):
    if not milliseconds or milliseconds <= 0:
        return None
    seconds = int(milliseconds / 1000)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s" if seconds else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _cache_path(key):
    CACHE_DIR.mkdir(exist_ok=True)
    safe_key = "".join(char if char.isalnum() else "_" for char in key)[-100:]
    return CACHE_DIR / f"{safe_key}.json"


def _read_cache(key, ttl):
    try:
        path = _cache_path(key)
        if path.exists():
            age = time.time() - path.stat().st_mtime
            # Negative age means a future-dated file (WSL2 clock drift).
            if 0 <= age < ttl:
                return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _write_cache(key, data):
    try:
        _cache_path(key).write_text(json.dumps(data))
    except OSError:
        pass


def _oauth_token():
    credentials_file = Path.home() / ".claude" / ".credentials.json"
    try:
        if credentials_file.exists():
            credentials = json.loads(credentials_file.read_text())
            return credentials.get("claudeAiOauth", {}).get("accessToken")
    except (json.JSONDecodeError, OSError):
        pass
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, timeout=2.0,
            )
            if result.returncode == 0 and result.stdout.strip():
                credentials = json.loads(result.stdout.strip())
                return credentials.get("claudeAiOauth", {}).get("accessToken")
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            pass
    return os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")


def _fetch_usage():
    cached = _read_cache("claude_usage", USAGE_CACHE_TTL)
    if cached is not None:
        return cached
    token = _oauth_token()
    if not token:
        return _read_cache("claude_usage", STALE_USAGE_TTL)
    try:
        request = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", method="GET")
        request.add_header("Accept", "application/json")
        request.add_header("Content-Type", "application/json")
        request.add_header("Authorization", f"Bearer {token}")
        request.add_header("anthropic-beta", "oauth-2025-04-20")
        request.add_header("User-Agent", "claude-code/2.0.32")
        with urllib.request.urlopen(request, timeout=3.0) as response:
            data = json.loads(response.read().decode())
        usage = {
            "five_hour": data.get("five_hour", {}).get("utilization"),
            "seven_day": data.get("seven_day", {}).get("utilization"),
        }
        _write_cache("claude_usage", usage)
        return usage
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return _read_cache("claude_usage", STALE_USAGE_TTL)


def _rate_limits(data):
    """Rate limits from the payload, else from the usage API (older Claude Code versions)."""
    payload = data.get("rate_limits") or {}
    five_hour = payload.get("five_hour") or {}
    seven_day = payload.get("seven_day") or {}
    if five_hour.get("used_percentage") is not None or seven_day.get("used_percentage") is not None:
        return {
            "five_hour": five_hour.get("used_percentage"),
            "seven_day": seven_day.get("used_percentage"),
            "five_reset": five_hour.get("resets_at"),
            "seven_reset": seven_day.get("resets_at"),
        }
    usage = _fetch_usage()
    if usage:
        return {
            "five_hour": usage.get("five_hour"),
            "seven_day": usage.get("seven_day"),
            "five_reset": None,
            "seven_reset": None,
        }
    return None


def _format_reset(resets_at):
    if resets_at is None:
        return None
    seconds = int(resets_at - time.time())
    if seconds <= 0:
        return None
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours}h"


def _rate_segment(label_color, icon, pct, resets_at):
    if pct is None:
        return f"{MUTED_LT}{icon} —{RESET}"
    segment = f"{label_color}{icon} {RESET}{_pct_color(pct)}{pct:.0f}%{RESET}"
    countdown = _format_reset(resets_at)
    if countdown:
        segment += f" {MUTED_LT}{countdown}{RESET}"
    return segment


def _session_prs(data):
    """Accumulate every PR the session touched; the payload only carries the current one."""
    current = data.get("pr") or {}
    session_id = data.get("session_id")
    if not session_id:
        return [current] if current.get("number") is not None else []

    store_key = f"prs_{session_id}"
    store = _read_cache(store_key, 86400) or {}
    if current.get("number") is not None:
        key = str(current.get("url") or current["number"])
        previous = store.get(key, {})
        store[key] = {
            "number": current["number"],
            "review_state": current.get("review_state"),
            "seen": time.time(),
        }
        changed = (
            store[key]["number"] != previous.get("number")
            or store[key]["review_state"] != previous.get("review_state")
            or time.time() - previous.get("seen", 0) > 60
        )
        if changed:
            _write_cache(store_key, store)
    return sorted(store.values(), key=lambda pr: pr.get("seen", 0))


def _pr_segment(prs):
    parts = []
    for pr in prs[-PR_DISPLAY_MAX:]:
        number = pr.get("number")
        if number is None:
            continue
        icon, color = _PR_STATE_ICONS.get(pr.get("review_state"), ("", FROST))
        parts.append(f"{TEAL_GREEN}#{number}{RESET}{color}{icon}{RESET}")
    if not parts:
        return None
    return f"{TEAL_GREEN}⇄ {RESET}" + " ".join(parts)


def _thinking_enabled(transcript_path):
    """Scan the transcript for a thinking block (older Claude Code versions omit `thinking` from the payload)."""
    if not transcript_path:
        return None
    try:
        raw = Path(transcript_path).read_bytes()
    except OSError:
        return None
    if b'"type":"thinking"' not in raw:
        return False
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if '"type":"thinking"' not in line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "assistant":
            continue
        blocks = entry.get("message", {}).get("content", [])
        if any(block.get("type") == "thinking" for block in blocks):
            return True
    return False


def _current_task(session_id):
    if not session_id:
        return None
    todos_dir = Path.home() / ".claude" / "todos"
    if not todos_dir.exists():
        return None
    try:
        todo_files = [
            path for path in todos_dir.iterdir()
            if path.name.startswith(session_id) and "-agent-" in path.name and path.suffix == ".json"
        ]
        if not todo_files:
            return None
        newest = max(todo_files, key=lambda path: path.stat().st_mtime)
        for todo in json.loads(newest.read_text()):
            if todo.get("status") == "in_progress":
                return todo.get("activeForm")
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _run_git(args, cwd):
    try:
        result = subprocess.run(
            ["git"] + args, cwd=cwd,
            capture_output=True, text=True, timeout=2.0,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def _parse_branch_line(info, branch_line, cwd):
    if not branch_line.startswith("## "):
        return
    description = branch_line[3:]
    if "HEAD (no branch)" in description or "HEAD detached" in description:
        info["detached"] = True
        sha = _run_git(["rev-parse", "--short", "HEAD"], cwd)
        info["branch"] = sha[:7] if sha else "detached"
    elif description.startswith(("No commits yet on ", "Initial commit on ")):
        info["branch"] = description.split("on ", 1)[-1]
    elif "..." in description:
        name, tracking = description.split("...", 1)
        info["branch"] = name
        info["upstream"] = True
        ahead = re.search(r"ahead (\d+)", tracking)
        behind = re.search(r"behind (\d+)", tracking)
        if ahead:
            info["ahead"] = int(ahead.group(1))
        if behind:
            info["behind"] = int(behind.group(1))
    else:
        info["branch"] = description.split()[0] if description else "unknown"


def _count_file_status(info, file_lines):
    entries = [line for line in file_lines if len(line) >= 2]
    if SHOW_STAGED_UNSTAGED_COUNTS:
        for line in entries:
            index_status, worktree_status = line[0], line[1]
            if index_status in "MADRC":
                info["staged"] += 1
            if worktree_status in "MD" or line.startswith("??"):
                info["unstaged"] += 1
    info["dirty"] = bool(
        info["staged"] or info["unstaged"] or any(line[0] != "#" for line in entries)
    )


def _git_info(cwd):
    cache_key = f"git_{cwd}"
    cached = _read_cache(cache_key, GIT_CACHE_TTL)
    if cached is not None:
        return cached

    info = {
        "branch": None, "detached": False, "upstream": False,
        "ahead": 0, "behind": 0, "staged": 0, "unstaged": 0, "dirty": False,
    }
    if _run_git(["rev-parse", "--show-toplevel"], cwd):
        status = _run_git(["status", "--porcelain=v1", "-b"], cwd)
        if status:
            branch_line, *file_lines = status.split("\n")
            _parse_branch_line(info, branch_line, cwd)
            _count_file_status(info, file_lines)
    _write_cache(cache_key, info)
    return info


def _load_tips():
    try:
        tips = json.loads(TIPS_POOL.read_text()).get("tips")
        return tips if isinstance(tips, list) and tips else None
    except (OSError, json.JSONDecodeError):
        return None


def _tip_line(max_width):
    tips = _load_tips()
    if not tips:
        return None
    tip = tips[int(time.time() // TIP_ROTATE_SECONDS) % len(tips)]
    command = str(tip.get("cmd", "")).strip()
    text = str(tip.get("text", "")).strip()
    if not command:
        return None

    glyph_color = GREEN if tip.get("src") in ("changelog", "ai") else PURPLE
    head = f"{glyph_color}✧{RESET} {FROST}{BOLD}{command}{RESET}"
    if not text:
        return head
    room = max_width - _visible_len(head) - 2
    if room < 12:
        return head
    if len(text) > room:
        text = text[: room - 1].rstrip() + "…"
    return f"{head}  {MUTED_LT}{text}{RESET}"


def _model_section(data):
    model_name = data.get("model", {}).get("display_name", "Claude")
    thinking_on = (data.get("thinking") or {}).get("enabled")
    thinking = thinking_on if thinking_on is not None else _thinking_enabled(data.get("transcript_path"))
    section = f"{_model_icon(model_name, thinking)} {FROST}{model_name}{RESET}"
    chip = _effort_chip((data.get("effort") or {}).get("level"), thinking_on)
    if chip:
        section += f" {chip}"
    return section


def _branch_section(git):
    if not git["branch"]:
        return None
    name = _truncate(git["branch"], MAX_BRANCH_WIDTH)
    if git["detached"]:
        branch = f"{ORANGE}@{name}{RESET}"
    elif git["dirty"]:
        branch = f"{YELLOW}{name} ✗{RESET}"
    else:
        branch = f"{GREEN}{name} ✓{RESET}"
    parts = [branch]
    if git["upstream"]:
        if git["ahead"] > 0:
            parts.append(f"{GREEN}↑{git['ahead']}{RESET}")
        if git["behind"] > 0:
            parts.append(f"{RED}↓{git['behind']}{RESET}")
    return " ".join(parts)


def _context_bar(data):
    context = data.get("context_window", {})
    remaining = context.get("remaining_percentage")
    if remaining is None and context.get("used_percentage") is not None:
        remaining = 100 - context["used_percentage"]
    if remaining is None:
        return f"{MUTED}{'▱' * BAR_WIDTH} —{RESET}"

    used_pct = max(0, min(100, 100 - remaining))
    max_tokens = context.get("context_window_size") or 200_000
    bar = _gradient_bar(used_pct, max_tokens)
    if data.get("exceeds_200k_tokens"):
        bar += f" {ORANGE}⟐{RESET}"
    compact_pct = float(os.environ.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "80"))
    # Auto-compact reserves ~16k output tokens that used_pct does not count.
    output_budget_pct = 16_384 / max_tokens * 100
    left_pct = max(0, compact_pct - output_budget_pct - used_pct)
    bar += f" {MUTED_LT}⟳ {RESET}{_pct_color(100 - left_pct)}{left_pct:.0f}%{RESET}"
    return bar


def _cost_color(cost_usd):
    if cost_usd < 1:
        return GREEN
    if cost_usd < 5:
        return YELLOW
    if cost_usd < 15:
        return ORANGE
    return RED


def _line2_clusters(data, git):
    """Clusters left to right: time+cost, rate limits, changes, PRs, task. Trailing clusters drop first."""
    cost_data = data.get("cost", {})
    time_cost, rates, changes, prs, task = [], [], [], [], []

    duration = _format_duration(cost_data.get("total_duration_ms"))
    if duration:
        time_cost.append(f"{FROST}⏱ {duration}{RESET}")
    cost_usd = cost_data.get("total_cost_usd")
    if cost_usd:
        time_cost.append(f"{TEAL}◎ {RESET}{_cost_color(cost_usd)}${cost_usd:.2f}{RESET}")

    limits = _rate_limits(data) or {}
    rates.append(_rate_segment(YELLOW, "⚡", limits.get("five_hour"), limits.get("five_reset")))
    rates.append(_rate_segment(PURPLE, "↯", limits.get("seven_day"), limits.get("seven_reset")))

    lines_added = cost_data.get("total_lines_added") or 0
    lines_removed = cost_data.get("total_lines_removed") or 0
    if lines_added > 0:
        changes.append(f"{GREEN}▲ {lines_added}{RESET}")
    if lines_removed > 0:
        changes.append(f"{RED}▼ {lines_removed}{RESET}")
    if SHOW_STAGED_UNSTAGED_COUNTS and git["dirty"]:
        changes.append(f"{ORANGE}+{git['staged']} ~{git['unstaged']}{RESET}")

    pr_segment = _pr_segment(_session_prs(data))
    if pr_segment:
        prs.append(pr_segment)

    current_task = _current_task(data.get("session_id", ""))
    if current_task:
        task.append(f"{BOLD}{FROST_DEEP}▸ {_truncate(current_task, MAX_TASK_WIDTH)}{RESET}")

    return [" ".join(cluster) for cluster in (time_cost, rates, changes, prs, task) if cluster]


def format_status_line(data):
    cwd = data.get("workspace", {}).get("current_dir") or data.get("cwd") or os.getcwd()
    git = _git_info(cwd)

    header_sections = [
        _model_section(data),
        f"{FROST_DEEP}{_truncate(Path(cwd).name, MAX_REPO_WIDTH)}{RESET}",
    ]
    branch = _branch_section(git)
    if branch:
        header_sections.append(branch)
    context_bar = _context_bar(data)

    terminal_width = shutil.get_terminal_size((120, 24)).columns
    content_width = max(24, terminal_width - ACCENT_WIDTH)

    lines = []
    header = _fit_from_left(header_sections, SEP, content_width)
    if _visible_len(header + SEP + context_bar) <= content_width:
        lines.append(header + SEP + context_bar)
    else:
        lines.extend([header, context_bar])
    lines.append(_fit_from_right(_line2_clusters(data, git), SEP, content_width) or f"{MUTED}—{RESET}")
    tip = _tip_line(content_width)
    if tip:
        lines.append(tip)
    return "\n" + "\n".join(_accent(index, line) for index, line in enumerate(lines))


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        data = {}
    print(format_status_line(data))


if __name__ == "__main__":
    main()
