"""
This is the one file you'll swap in your real AI provider details.

If an ANTHROPIC_API_KEY environment variable is set, summarize_transcript()
calls the real Claude API to turn a pasted transcript into a short summary
plus a list of action items. If no key is set (e.g. running this demo
locally without one yet), it falls back to a simple offline heuristic so
the rest of the app still works end to end.

To go live: `export ANTHROPIC_API_KEY=sk-ant-...` before running app.py.
"""
import os
import json
import re

from translations import SUPPORTED_LANGUAGES

MODEL = "claude-sonnet-4-5"


def summarize_transcript(transcript, title, language="en"):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        return _summarize_with_claude(transcript, title, language, api_key)
    return _summarize_offline_fallback(transcript)


def _summarize_with_claude(transcript, title, language, api_key):
    import urllib.request

    language_name = SUPPORTED_LANGUAGES.get(language, "English")
    prompt = f"""You are summarizing the meeting "{title}" for a small office.
Write your response in {language_name}.

Transcript:
{transcript}

Respond with ONLY valid JSON, no markdown fences, no preamble, in this exact shape:
{{"summary": "3-5 sentence summary in {language_name}", "action_items": [{{"description": "... in {language_name}", "owner": "name or 'unassigned'"}}]}}"""

    body = json.dumps(
        {
            "model": MODEL,
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = "".join(
            block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
        )
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        parsed = json.loads(text)
        return parsed.get("summary", ""), parsed.get("action_items", [])
    except Exception as exc:
        # Never let an API hiccup break meeting creation - fall back gracefully.
        summary, items = _summarize_offline_fallback(transcript)
        return f"[AI summary unavailable: {exc}] {summary}", items


def _summarize_offline_fallback(transcript):
    """No API key configured yet - do something reasonable so the app is
    still fully usable during local development."""
    if not transcript or not transcript.strip():
        return "No transcript provided.", []

    sentences = re.split(r"(?<=[.!?])\s+", transcript.strip())
    summary = " ".join(sentences[:3]) or transcript[:280]

    action_items = []
    for line in transcript.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith(("action:", "todo:", "action item:", "- [ ]", "follow up:")):
            cleaned = re.split(r":", stripped, maxsplit=1)[-1].strip(" -[]")
            if cleaned:
                action_items.append({"description": cleaned, "owner": "unassigned"})

    return summary, action_items
