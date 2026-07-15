#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import os
import pty
import re
import select
import signal
import socket
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.json"
IMAGE_PATH = ROOT / "screen.png"
CLAUDE = Path.home() / ".local/bin/claude"
CODEX = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
FONTS = ROOT / "fonts"
F_DISPLAY = str(FONTS / "Fraunces-Display.ttf")
F_ITALIC = str(FONTS / "Fraunces-DisplayItalic.ttf")
SG_REG = str(FONTS / "SpaceGrotesk-Regular.ttf")
SG_MED = str(FONTS / "SpaceGrotesk-Medium.ttf")
SG_BOLD = str(FONTS / "SpaceGrotesk-Bold.ttf")
PORT = 8790
REFRESH_SECONDS = 60
DISPLAY_REFRESH_SECONDS = 30
ACCESS_TOKEN = os.environ.get("DASHBOARD_ACCESS_TOKEN", "change-me")
CLAUDE_WEB_FRESH_SECONDS = 600
CLAUDE_FALLBACK_SECONDS = 300
RENDER_VERSION = 8

CANVAS_W, CANVAS_H = 758, 1024
MARGIN = 54
RIGHT_EDGE = CANVAS_W - MARGIN
PAPER = 255
INK = 0
GREY = 68       # secondary text (4-bit level 4)
FAINT = 153     # hairlines / minor ticks (level 9)
TRACK = 221     # meter track (level 13)


def strip_terminal_codes(value: str) -> str:
    value = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", value)
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    value = re.sub(r"\x1b[()][A-Z0-9]", "", value)
    return value.replace("\x0f", "").replace("\r", "\n")


def last_usage_match(text: str, label: str) -> dict[str, object] | None:
    pattern = re.compile(
        re.escape(label)
        + r"\s+(\d+)%\s+(?:\d+%\s+)?used\s+Resets\s+([^\n]+)",
        re.IGNORECASE,
    )
    matches = pattern.findall(text)
    if not matches:
        return None
    used, reset = matches[-1]
    return {"used_percent": int(used), "resets": reset.strip()}


def collect_claude() -> dict[str, object]:
    master, slave = pty.openpty()
    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    proc = subprocess.Popen(
        [str(CLAUDE), "--ax-screen-reader", "--safe-mode"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        cwd=str(ROOT),
        env=env,
        start_new_session=True,
        close_fds=True,
    )
    os.close(slave)
    raw = bytearray()
    sent = False
    found_at: float | None = None
    deadline = time.monotonic() + 35
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.5)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                raw.extend(chunk)
            text = strip_terminal_codes(raw.decode("utf-8", "replace"))
            if not sent and ("manual mode on" in text or "What's new" in text):
                os.write(master, b"/usage\r")
                sent = True
            if sent and "Current week (Fable)" in text:
                found_at = found_at or time.monotonic()
                if time.monotonic() - found_at >= 2:
                    break
    finally:
        try:
            os.write(master, b"\x1b\x03\x03")
        except OSError:
            pass
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        os.close(master)

    text = strip_terminal_codes(raw.decode("utf-8", "replace"))
    session = last_usage_match(text, "Current session")
    weekly = last_usage_match(text, "Current week (all models)")
    fable = last_usage_match(text, "Current week (Fable)")
    if not session or not weekly:
        raise RuntimeError("Claude usage did not appear in the terminal output")
    return {
        "plan": "Team",
        "session": session,
        "weekly": weekly,
        "model_weekly": fable,
        "model_name": "Fable",
    }


def age_seconds(value: object) -> float:
    if not value:
        return math.inf
    try:
        recorded = datetime.fromisoformat(str(value)).astimezone()
    except (TypeError, ValueError):
        return math.inf
    return max(0.0, (datetime.now().astimezone() - recorded).total_seconds())


def claude_reset_text(value: object) -> str:
    if not value:
        return "TIME UNAVAILABLE"
    try:
        reset = datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone()
    except (TypeError, ValueError):
        return str(value)
    now = datetime.now().astimezone()
    if reset.date() == now.date():
        return reset.strftime("%-I:%M%p").lower()
    return reset.strftime("%b %-d at %-I:%M%p")


def claude_from_web(payload: dict[str, object], plan: str = "Team") -> dict[str, object]:
    limits = payload.get("limits")
    if not isinstance(limits, list):
        raise ValueError("Claude response does not contain usage limits")

    by_kind = {
        str(item.get("kind")): item
        for item in limits
        if isinstance(item, dict) and item.get("kind")
    }
    session = by_kind.get("session")
    weekly = by_kind.get("weekly_all")
    scoped = by_kind.get("weekly_scoped")
    if not session or not weekly:
        raise ValueError("Claude response is missing session or weekly usage")

    model_name = "MODEL"
    if scoped:
        scope = scoped.get("scope")
        model = scope.get("model") if isinstance(scope, dict) else None
        if isinstance(model, dict) and model.get("display_name"):
            model_name = str(model["display_name"])

    return {
        "plan": plan,
        "session": {
            "used_percent": int(session.get("percent", 0)),
            "resets": claude_reset_text(session.get("resets_at")),
        },
        "weekly": {
            "used_percent": int(weekly.get("percent", 0)),
            "resets": claude_reset_text(weekly.get("resets_at")),
        },
        "model_weekly": {
            "used_percent": int(scoped.get("percent", 0)),
            "resets": claude_reset_text(scoped.get("resets_at")),
        } if scoped else None,
        "model_name": model_name,
    }


def collect_codex() -> dict[str, object]:
    proc = subprocess.Popen(
        [str(CODEX), "app-server", "--listen", "stdio://"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    assert proc.stdin and proc.stdout
    messages = [
        {
            "method": "initialize",
            "id": 1,
            "params": {
                "clientInfo": {
                    "name": "kindle_usage_dashboard",
                    "title": "Kindle Usage Dashboard",
                    "version": "1.0.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        },
        {"method": "initialized", "params": {}},
        {"method": "account/rateLimits/read", "id": 2, "params": {}},
    ]
    for message in messages:
        proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.flush()

    result: dict[str, object] | None = None
    deadline = time.monotonic() + 20
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], 0.5)
            if not ready:
                continue
            line = proc.stdout.readline()
            if not line:
                break
            message = json.loads(line)
            if message.get("id") == 2:
                result = message.get("result")
                break
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()

    if not result:
        raise RuntimeError("Codex rate-limit response was not received")
    snapshot = result["rateLimits"]
    primary = snapshot.get("primary")
    secondary = snapshot.get("secondary")
    if not primary:
        raise RuntimeError("Codex did not report a primary rate-limit window")
    return {
        "plan": str(snapshot.get("planType") or "").title(),
        "primary": primary,
        "secondary": secondary,
    }


def reset_text(timestamp: int | float | None) -> str:
    if not timestamp:
        return "TIME UNAVAILABLE"
    value = datetime.fromtimestamp(timestamp).astimezone()
    return value.strftime("%a %-d %b · %-I:%M %p").upper()


def compact_reset(value: object) -> str:
    text = str(value or "TIME UNAVAILABLE")
    text = re.sub(r"\s*\([^)]*\)\s*$", "", text)
    text = re.sub(r"\s+at\s+", " · ", text, flags=re.IGNORECASE)
    text = re.sub(r"^resets\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(\d)(AM|PM)\b", r"\1 \2", text, flags=re.IGNORECASE)
    return text.upper()


def window_label(win: dict) -> str | None:
    mins = win.get("windowDurationMins")
    if not isinstance(mins, (int, float)) or not mins:
        return None
    if mins % 1440 == 0:
        days = int(mins // 1440)
        return "1 DAY" if days == 1 else f"{days} DAYS"
    if mins % 60 == 0:
        hours = int(mins // 60)
        return "1 HOUR" if hours == 1 else f"{hours} HOURS"
    return f"{int(mins)} MIN"


_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    key = (path, size)
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(path, size)
    return _font_cache[key]


def tracked(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    fnt: ImageFont.FreeTypeFont,
    fill: int = INK,
    tracking: float = 2.0,
    anchor: str = "ls",
) -> float:
    """Draw letterspaced text at a baseline. anchor 'ls' left, 'rs' right, 'ms' centered."""
    widths = [draw.textlength(ch, font=fnt) for ch in text]
    total = sum(widths) + tracking * (len(text) - 1 if text else 0)
    x, y = xy
    if anchor == "rs":
        x -= total
    elif anchor == "ms":
        x -= total / 2
    for ch, w in zip(text, widths):
        draw.text((x, y), ch, font=fnt, fill=fill, anchor="ls")
        x += w + tracking
    return total


def meter(draw: ImageDraw.ImageDraw, x1: int, y: float, x2: int, height: int, used: int) -> None:
    used = max(0, min(100, used))
    fill_w = round((x2 - x1) * used / 100)
    draw.rectangle((x1, y, x2, y + height), fill=TRACK)
    if fill_w > 0:
        draw.rectangle((x1, y, x1 + fill_w, y + height), fill=INK)
    draw.rectangle((x2, y, x2, y + height), fill=GREY)
    draw.rectangle((x1, y, x1, y + height), fill=GREY if fill_w == 0 else INK)


def ruler(draw: ImageDraw.ImageDraw, x1: int, y: float, x2: int) -> None:
    span = x2 - x1
    for pct in range(0, 101, 5):
        x = x1 + span * pct / 100
        major = pct % 25 == 0
        draw.line((x, y, x, y + (9 if major else 5)), fill=INK if major else FAINT, width=1)
    small = font(SG_MED, 14)
    for pct in (0, 25, 50, 75, 100):
        x = x1 + span * pct / 100
        anchor = "ls" if pct == 0 else ("rs" if pct == 100 else "ms")
        tracked(draw, (x, y + 24), str(pct), small, fill=GREY, tracking=0.5, anchor=anchor)


def battery_glyph(draw: ImageDraw.ImageDraw, x_right: int, cy: int, percent: int | None) -> None:
    label = f"{percent}%" if percent is not None else "--%"
    bw, bh = 30, 14
    bx2, bx1 = x_right, x_right - bw
    y1 = cy - bh // 2
    draw.rounded_rectangle((bx1, y1, bx2, y1 + bh), radius=2, outline=INK, width=1)
    draw.rectangle((bx2 + 1, cy - 3, bx2 + 3, cy + 3), fill=INK)
    if percent:
        w = max(2, round((bw - 4) * min(100, percent) / 100))
        draw.rectangle((bx1 + 2, y1 + 2, bx1 + 2 + w, y1 + bh - 2), fill=INK)
    tracked(draw, (bx1 - 10, cy + 6), label, font(SG_MED, 17), fill=INK, tracking=0.5, anchor="rs")


def percent_number(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    value: int,
    num_size: int,
    anchor: str = "rs",
) -> float:
    """Large serif numeral with a smaller % sign on the shared baseline."""
    num_f = font(F_DISPLAY, num_size)
    pct_f = font(F_DISPLAY, round(num_size * 0.45))
    num = str(max(0, min(100, value)))
    nw = draw.textlength(num, font=num_f)
    pw = draw.textlength("%", font=pct_f)
    gap = max(2, num_size // 24)
    total = nw + gap + pw
    x, y = xy
    if anchor == "rs":
        x -= total
    elif anchor == "ms":
        x -= total / 2
    draw.text((x, y), num, font=num_f, fill=INK, anchor="ls")
    draw.text((x + nw + gap, y), "%", font=pct_f, fill=INK, anchor="ls")
    return total


def section_head(draw: ImageDraw.ImageDraw, y: float, name: str, plan: str) -> None:
    name_f = font(F_ITALIC, 40)
    draw.text((MARGIN, y), name, font=name_f, fill=INK, anchor="ls")
    name_w = draw.textlength(name, font=name_f)
    plan_text = (plan or "").upper()
    pw = 0.0
    if plan_text:
        pw = tracked(draw, (RIGHT_EDGE, y - 2), plan_text + " PLAN", font(SG_BOLD, 17), fill=INK, tracking=2.5, anchor="rs")
    line_x1 = MARGIN + name_w + 22
    line_x2 = RIGHT_EDGE - pw - 22
    if line_x2 > line_x1:
        draw.line((line_x1, y - 13, line_x2, y - 13), fill=FAINT, width=1)


def usage_row(
    draw: ImageDraw.ImageDraw,
    y: float,
    label: str,
    used: int,
    row_h: int,
    detail: str | None = None,
) -> float:
    """One table row: hairline, label + optional right detail, numeral, thin meter."""
    draw.line((MARGIN, y, RIGHT_EDGE, y), fill=FAINT, width=1)
    base_y = y + 34
    tracked(draw, (MARGIN, base_y), label, font(SG_MED, 19), fill=INK, tracking=2.5)
    if detail:
        tracked(draw, (RIGHT_EDGE - 130, base_y), detail, font(SG_MED, 15), fill=GREY, tracking=1.0, anchor="rs")
    percent_number(draw, (RIGHT_EDGE, base_y + 12), used, 48, anchor="rs")
    meter(draw, MARGIN, y + 46, RIGHT_EDGE - 118, 10, used)
    return y + row_h


def render_dashboard(state: dict[str, object]) -> None:
    image = Image.new("L", (CANVAS_W, CANVAS_H), PAPER)
    draw = ImageDraw.Draw(image)

    checked = datetime.fromisoformat(str(state.get("checked_at") or state["updated_at"])).astimezone()

    claude = state.get("claude") or {}
    session = claude.get("session") or {}
    weekly = claude.get("weekly") or {}
    model_weekly = claude.get("model_weekly") or {}
    model_name = str(claude.get("model_name") or "MODEL").upper()

    codex = state.get("codex") or {}
    primary = codex.get("primary") or {}
    secondary = codex.get("secondary") or {}

    # Compact mode squeezes vertical rhythm when Codex reports a session window too.
    extra_row = bool(secondary)
    clock_size = 96 if extra_row else 108
    clock_base = 172 if extra_row else 188
    hero_num = 118 if extra_row else 132
    hero_drop = 132 if extra_row else 156
    row_h = 66 if extra_row else 74
    gap_section = 72 if extra_row else 100
    gap_table = 78 if extra_row else 88

    # ---- Header ---------------------------------------------------------
    tracked(draw, (MARGIN, 56), checked.strftime("%A · %-d %B %Y").upper(), font(SG_MED, 19), fill=INK, tracking=3.0)
    battery = state.get("kindle_battery")
    battery_glyph(draw, RIGHT_EDGE, 49, int(battery) if battery is not None else None)

    draw.line((MARGIN, 74, RIGHT_EDGE, 74), fill=INK, width=3)
    draw.line((MARGIN, 80, RIGHT_EDGE, 80), fill=INK, width=1)

    time_txt = checked.strftime("%-I:%M")
    clock_f = font(F_DISPLAY, clock_size)
    draw.text((MARGIN - 4, clock_base), time_txt, font=clock_f, fill=INK, anchor="ls")
    tw = draw.textlength(time_txt, font=clock_f)
    tracked(draw, (MARGIN + tw + 14, clock_base), checked.strftime("%p"), font(SG_BOLD, 26), fill=INK, tracking=2.0)

    tracked(draw, (RIGHT_EDGE, clock_base - 52), "USAGE LEDGER", font(SG_BOLD, 21), fill=INK, tracking=4.0, anchor="rs")
    tracked(draw, (RIGHT_EDGE, clock_base - 26), "CLAUDE & CODEX", font(SG_MED, 16), fill=GREY, tracking=3.0, anchor="rs")
    tracked(draw, (RIGHT_EDGE, clock_base - 4), "LIVE · EVERY MINUTE", font(SG_MED, 16), fill=GREY, tracking=3.0, anchor="rs")

    # ---- Claude ----------------------------------------------------------
    sy = clock_base + 104
    section_head(draw, sy, "Claude", str(claude.get("plan") or ""))

    session_used = int(session.get("used_percent", 0))
    hero_base = sy + hero_drop
    num_w = percent_number(draw, (MARGIN - 2, hero_base), session_used, hero_num, anchor="ls")

    info_x = MARGIN + max(num_w + 40, 280)
    tracked(draw, (info_x, hero_base - 96), "SESSION", font(SG_BOLD, 23), fill=INK, tracking=4.0)
    tracked(draw, (info_x, hero_base - 66), "5-HOUR WINDOW", font(SG_MED, 17), fill=GREY, tracking=2.5)
    remaining = max(0, 100 - session_used)
    draw.text((info_x, hero_base - 18), f"{remaining}% remaining", font=font(F_ITALIC, 32), fill=INK, anchor="ls")
    tracked(draw, (info_x, hero_base + 10), "RESETS " + compact_reset(session.get("resets")), font(SG_MED, 17), fill=GREY, tracking=1.5)

    bar_y = hero_base + 36
    meter(draw, MARGIN, bar_y, RIGHT_EDGE, 20, session_used)
    ruler(draw, MARGIN, bar_y + 26, RIGHT_EDGE)

    ty = bar_y + gap_table
    ty = usage_row(draw, ty, "WEEK · ALL MODELS", int(weekly.get("used_percent", 0)), row_h)
    if model_weekly:
        ty = usage_row(draw, ty, f"WEEK · {model_name}", int(model_weekly.get("used_percent", 0)), row_h)
    draw.line((MARGIN, ty, RIGHT_EDGE, ty), fill=FAINT, width=1)
    tracked(
        draw,
        (MARGIN, ty + 26),
        "WEEKLY LIMITS RESET " + compact_reset(weekly.get("resets")),
        font(SG_MED, 17),
        fill=GREY,
        tracking=2.0,
    )

    # ---- Codex -----------------------------------------------------------
    cy = ty + gap_section
    section_head(draw, cy, "Codex", str(codex.get("plan") or ""))

    ry = cy + 24.0
    if secondary:
        label = "SESSION" + (f" · {window_label(secondary)}" if window_label(secondary) else "")
        ry = usage_row(
            draw, ry, label, int(secondary.get("usedPercent", 0)), row_h,
            detail="RESETS " + reset_text(secondary.get("resetsAt")),
        )
    label = "WEEK" + (f" · {window_label(primary)}" if window_label(primary) else "")
    ry = usage_row(
        draw, ry, label, int(primary.get("usedPercent", 0)), row_h,
        detail=("RESETS " + reset_text(primary.get("resetsAt"))) if secondary else None,
    )
    draw.line((MARGIN, ry, RIGHT_EDGE, ry), fill=FAINT, width=1)
    if not secondary:
        tracked(draw, (MARGIN, ry + 26), "RESETS " + reset_text(primary.get("resetsAt")), font(SG_MED, 17), fill=GREY, tracking=2.0)
        tracked(draw, (RIGHT_EDGE, ry + 26), "NO SESSION CAP", font(SG_MED, 17), fill=GREY, tracking=2.0, anchor="rs")

    # ---- Footer -----------------------------------------------------------
    fy = CANVAS_H - 70
    draw.line((MARGIN, fy, RIGHT_EDGE, fy), fill=INK, width=1)
    draw.line((MARGIN, fy + 4, RIGHT_EDGE, fy + 4), fill=INK, width=3)
    tracked(draw, (MARGIN, fy + 34), "HOLD HERE TO EXIT", font(SG_MED, 16), fill=INK, tracking=2.5)
    source = "WEB FEED" if state.get("claude_source") == "claude_web" else "CLI FALLBACK"
    footer_right = f"CLAUDE VIA {source} · SYNCED {checked.strftime('%-I:%M %p').upper()}"
    tracked(draw, (RIGHT_EDGE, fy + 34), footer_right, font(SG_MED, 16), fill=INK, tracking=2.0, anchor="rs")
    image.save(IMAGE_PATH, format="PNG", optimize=True)


state_lock = threading.Lock()


def load_state() -> dict[str, object]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def refresh_state() -> None:
    with state_lock:
        previous = load_state()

    errors: list[str] = []
    claude_result: dict[str, object] | None = None
    claude_checked_at: str | None = None
    web_is_fresh = age_seconds(previous.get("claude_web_checked_at")) <= CLAUDE_WEB_FRESH_SECONDS
    fallback_is_due = age_seconds(previous.get("claude_cli_checked_at")) >= CLAUDE_FALLBACK_SECONDS
    if not web_is_fresh and fallback_is_due:
        claude_checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
        try:
            claude_result = collect_claude()
        except Exception as exc:
            errors.append(f"Claude: {exc}")

    codex_result: dict[str, object] | None = None
    try:
        codex_result = collect_codex()
    except Exception as exc:
        errors.append(f"Codex: {exc}")

    now = datetime.now().astimezone().isoformat(timespec="seconds")

    with state_lock:
        state = load_state() or previous
        previous_usage = {
            "claude": state.get("claude"),
            "codex": state.get("codex"),
        }
        if claude_result and age_seconds(state.get("claude_web_checked_at")) > CLAUDE_WEB_FRESH_SECONDS:
            state["claude"] = claude_result
            state["claude_source"] = "claude_code_fallback"
        if claude_checked_at:
            state["claude_cli_checked_at"] = claude_checked_at
        if codex_result:
            state["codex"] = codex_result
        if "claude" not in state or "codex" not in state:
            raise RuntimeError("No complete usage snapshot is available: " + "; ".join(errors))
        current_usage = {
            "claude": state.get("claude"),
            "codex": state.get("codex"),
        }
        changed = previous_usage != current_usage
        render_version_changed = state.get("render_version") != RENDER_VERSION
        state["checked_at"] = now
        state["updated_at"] = state.get("updated_at") or now
        state["render_version"] = RENDER_VERSION
        state["errors"] = errors
        if changed or render_version_changed or not IMAGE_PATH.exists():
            state["updated_at"] = now
        STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")
        render_dashboard(state)


def refresh_loop() -> None:
    while True:
        started = time.monotonic()
        try:
            refresh_state()
        except Exception as exc:
            print(f"refresh failed: {exc}", flush=True)
        time.sleep(max(1, REFRESH_SECONDS - (time.monotonic() - started)))


def record_kindle_status(value: str | None, ip_address: str) -> None:
    try:
        percent = max(0, min(100, int(value or "")))
    except ValueError:
        percent = None

    with state_lock:
        state = load_state()
        if not state:
            return
        battery_changed = percent is not None and state.get("kindle_battery") != percent
        if percent is not None:
            state["kindle_battery"] = percent
        state["kindle_ip"] = ip_address
        state["kindle_last_seen"] = datetime.now().astimezone().isoformat(timespec="seconds")
        state["render_version"] = RENDER_VERSION
        STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")
        if battery_changed and state.get("claude") and state.get("codex"):
            render_dashboard(state)


def record_claude_web(payload: dict[str, object]) -> dict[str, object]:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with state_lock:
        state = load_state()
        plan = str((state.get("claude") or {}).get("plan") or "Team")
        claude = claude_from_web(payload, plan)
        changed = state.get("claude") != claude
        state["claude"] = claude
        state["claude_source"] = "claude_web"
        state["claude_web_checked_at"] = now
        state["checked_at"] = now
        state["updated_at"] = now if changed else state.get("updated_at") or now
        state["render_version"] = RENDER_VERSION
        state["errors"] = [
            error for error in state.get("errors", [])
            if not str(error).startswith("Claude:")
        ]
        STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")
        if state.get("codex"):
            render_dashboard(state)
    return {
        "session": claude["session"]["used_percent"],
        "weekly": claude["weekly"]["used_percent"],
        "model_weekly": (claude.get("model_weekly") or {}).get("used_percent"),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "AIUsageKindle/1.0"

    def send_bytes(
        self,
        content: bytes,
        content_type: str,
        status: int = 200,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(content)

    def do_OPTIONS(self) -> None:
        if self.path.split("?", 1)[0] != "/api/claude":
            self.send_bytes(b"not found\n", "text/plain", 404)
            return
        self.send_bytes(
            b"",
            "text/plain",
            204,
            {
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, access-token",
            },
        )

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path != "/api/claude":
            self.send_bytes(b"not found\n", "text/plain", 404)
            return
        if self.headers.get("access-token") != ACCESS_TOKEN:
            self.send_bytes(b'{"error":"unauthorized"}\n', "application/json", 401)
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0 or length > 100_000:
                raise ValueError("invalid content length")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
            summary = record_claude_web(payload)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_bytes(
                json.dumps({"error": str(exc)}).encode() + b"\n",
                "application/json",
                400,
                {"Access-Control-Allow-Origin": "*"},
            )
            return
        self.send_bytes(
            json.dumps({"ok": True, **summary}).encode() + b"\n",
            "application/json",
            extra_headers={"Access-Control-Allow-Origin": "*"},
        )

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self.send_bytes(b"ok\n", "text/plain")
            return
        if path == "/api/display":
            if self.headers.get("access-token") != ACCESS_TOKEN:
                self.send_bytes(b'{"error":"unauthorized"}\n', "application/json", 401)
                return
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                record_kindle_status(self.headers.get("percent-charged"), self.client_address[0])
            host = self.headers.get("Host") or f"127.0.0.1:{PORT}"
            modified = int(IMAGE_PATH.stat().st_mtime) if IMAGE_PATH.exists() else 0
            payload = {
                "image_url": f"http://{host}/screen.png?v={modified}",
                "filename": f"ai-usage-{modified}.png",
                "refresh_rate": DISPLAY_REFRESH_SECONDS,
            }
            self.send_bytes(json.dumps(payload).encode() + b"\n", "application/json")
            return
        if path == "/screen.png" and IMAGE_PATH.exists():
            self.send_bytes(IMAGE_PATH.read_bytes(), "image/png")
            return
        self.send_bytes(b"not found\n", "text/plain", 404)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)


def main() -> None:
    thread = threading.Thread(target=refresh_loop, name="usage-refresh", daemon=True)
    thread.start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"AI usage dashboard listening on port {PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
