"""Generate a local TTS preview page.

Synthesizes a handful of representative store lines through the *active* Sarvam
TTS config (model/speaker/language read from .env + the business profile), writes
one WAV per line into ``recordings/``, and emits ``recordings/tts_preview.html``
— a self-contained page that shows each line's text next to a play button.

This is deliberately a *local* page (no hosting): open the HTML directly in a
browser. Re-run this script to regenerate after changing the voice/model/lines.

Run from server/::

    uv run python scripts/generate_tts_preview.py
"""

from __future__ import annotations

import base64
import html
import json
import os
import sys
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

# Import the active business profile so the preview uses the same language the
# bot would (e.g. the store's hi-IN).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from business import get_active_profile  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

SARVAM_TTS_URL = "https://api.sarvam.ai/text-to-speech"
RECORDINGS_DIR = Path(__file__).resolve().parent.parent / "recordings"

# The lines to preview: (filename stem, short label, text to speak). These mirror
# what the store bot actually says — a mix of Hinglish and English so you can hear
# both the Hindi pronunciation and how it mirrors an English caller.
LINES: list[tuple[str, str, str]] = [
    (
        "greeting",
        "Greeting (Hinglish)",
        "Namaste! Mango and Co. mein aapka swagat hai. Main aapki kaise help kar sakti hoon?",
    ),
    (
        "stock",
        "Stock check (Hinglish)",
        "Ji haan, air fryer stock mein hai — humare paas abhi baarah units available hain.",
    ),
    (
        "order",
        "Order status (Hinglish)",
        "Aapka order number five four three one three one ship ho chuka hai, "
        "aur nau July tak deliver ho jayega.",
    ),
    (
        "returns",
        "Returns policy (Hinglish)",
        "Bilkul, aap delivery ke saat din ke andar, receipt ke saath item return kar sakte hain.",
    ),
    (
        "english_mirror",
        "English caller (mirrored, light touch)",
        "Sure! We sell appliances like air fryers, electric kettles, and mixer grinders. "
        "Aap kis cheez ke baare mein jaanna chahenge?",
    ),
]


def synthesize(api_key: str, text: str, *, model: str, speaker: str, language: str) -> bytes:
    """Call Sarvam's REST TTS and return the WAV bytes for ``text``."""
    payload = {
        "text": text,
        "target_language_code": language,
        "speaker": speaker,
        "model": model,
        "sample_rate": 22050,
        "enable_preprocessing": True,
    }
    req = urllib.request.Request(
        SARVAM_TTS_URL,
        data=json.dumps(payload).encode(),
        headers={"api-subscription-key": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode())
    audios = data.get("audios")
    if not audios:
        raise RuntimeError(f"No audio returned for: {text!r} ({data})")
    # Sarvam returns a base64 WAV (RIFF header included) — write it straight out.
    return base64.b64decode(audios[0])


def build_html(profile_name: str, model: str, speaker: str, language: str, clips) -> str:
    """Render the preview page. ``clips`` is a list of (label, text, wav_filename)."""
    cards = []
    for label, text, wav in clips:
        cards.append(
            f"""    <div class="card">
      <div class="label">{html.escape(label)}</div>
      <p class="text">{html.escape(text)}</p>
      <audio controls preload="none" src="{html.escape(wav)}"></audio>
    </div>"""
        )
    cards_html = "\n".join(cards)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Sarvam TTS preview — {html.escape(profile_name)}</title>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 720px;
           margin: 40px auto; padding: 0 20px; line-height: 1.5; }}
    h1 {{ font-size: 1.4rem; margin-bottom: 0.2rem; }}
    .meta {{ color: #888; font-size: 0.85rem; margin-bottom: 1.5rem; }}
    .card {{ border: 1px solid #8883; border-radius: 12px; padding: 16px 18px;
            margin-bottom: 14px; }}
    .label {{ font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em;
             color: #888; margin-bottom: 6px; }}
    .text {{ font-size: 1.05rem; margin: 0 0 12px; }}
    audio {{ width: 100%; }}
  </style>
</head>
<body>
  <h1>Sarvam TTS preview</h1>
  <div class="meta">
    profile: <b>{html.escape(profile_name)}</b> &middot;
    model: <b>{html.escape(model)}</b> &middot;
    speaker: <b>{html.escape(speaker)}</b> &middot;
    language: <b>{html.escape(language)}</b>
  </div>
{cards_html}
</body>
</html>
"""


def main() -> None:
    api_key = os.environ.get("SARVAM_API_KEY")
    if not api_key:
        raise SystemExit("SARVAM_API_KEY not set in server/.env")

    profile = get_active_profile()
    model = os.environ.get("SARVAM_TTS_MODEL", "bulbul:v2")
    speaker = os.environ.get("SARVAM_VOICE", "anushka")
    language = profile.tts_language

    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    clips = []
    for stem, label, text in LINES:
        print(f"synthesizing: {label} …")
        wav_bytes = synthesize(api_key, text, model=model, speaker=speaker, language=language)
        wav_name = f"preview_{stem}.wav"
        (RECORDINGS_DIR / wav_name).write_bytes(wav_bytes)
        clips.append((label, text, wav_name))

    html_path = RECORDINGS_DIR / "tts_preview.html"
    html_path.write_text(
        build_html(profile.display_name, model, speaker, language, clips)
    )
    print(f"\nDone. Open: {html_path}")


if __name__ == "__main__":
    main()
