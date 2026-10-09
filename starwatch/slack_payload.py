"""Pure Slack formatting; event IDs remain attached to chunks for partial ack."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SlackChunk:
    payload: dict[str, str]
    event_ids: list[str]


def escape(text: object) -> str:
    value = str(text or "")
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", " ").replace("\r", " ")


def release_line(event: dict) -> str:
    repo = escape(event["repository"])
    title = f"⭐ {repo}" if event.get("is_special") else repo
    tag = escape(event["tag_name"])
    url = str(event.get("html_url") or "")
    if not url.startswith("https://github.com/") or any(c in url for c in "<>|\r\n"):
        link = f"`{tag}`"
    else:
        link = f"<{url}|`{tag}`>"
    name = escape(event.get("release_name") or "")
    suffix = f" — {name}" if name and name != tag else ""
    date = escape(str(event.get("published_at") or "")[:10])
    return f"• {title} {link}{suffix} ({date}) [{escape(event['event_id'])}]"


def build_chunks(events: list[dict], max_length: int) -> list[SlackChunk]:
    if not events:
        return []
    header = f"🚀 *새로운 릴리스 {len(events)}개를 확인했습니다*\n"
    chunks: list[SlackChunk] = []
    lines: list[str] = []
    ids: list[str] = []
    for event in events:
        line = release_line(event)
        if len(header) + sum(len(s) + 1 for s in lines) + len(line) + 1 > max_length and lines:
            chunks.append(SlackChunk({"text": header + "\n".join(lines)}, ids))
            lines, ids = [], []
        # Never emit a Slack payload above its configured budget.
        allowed = max(0, max_length - len(header) - 1)
        lines.append(line[:allowed])
        ids.append(event["event_id"])
    if lines:
        chunks.append(SlackChunk({"text": header + "\n".join(lines)}, ids))
    return chunks
