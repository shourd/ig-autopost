"""Draft a one-line caption for a photo, using Claude with vision.

Two things are deliberately kept away from the model:

  1. The date bracket. The model returns only the descriptive clause; Python
     appends " (Mon, YYYY)". The format is then structurally impossible to get
     wrong rather than merely instructed.
  2. The place. A model will invent a confident, plausible, wrong place name
     from image content alone, and a wrong location on a photography account is
     worse than no location. The place is resolved before the call — from the
     batch label set in the curation app — and passed in as a stated fact. If
     there isn't one, the model is told to write a caption with no location and
     the photo is flagged for manual attention.
"""

from __future__ import annotations

import base64
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from src.config import CaptionConfig, load_config, secret
from src.exif import date_suffix, read_capture_date

# Flags the curation app renders as warning badges.
FLAG_NO_DATE = "no_date"
FLAG_NO_PLACE = "no_place"
FLAG_CAPTION_FAILED = "caption_failed"

SYSTEM_PROMPT = """\
You write one-line captions for a photography account. The voice is plain and \
observational, with room for a little quiet lyricism — never florid, and never \
the kind of line a caption generator produces.

Write a single short caption describing what is actually visible in the frame.

- One line. Short: the house style runs from about three to ten words.
- Start with a lowercase letter.
- No hashtags, no emoji, no exclamation marks, no quotation marks.
- No date, and no year. Those are added afterwards.
- Describe what is in the frame. Don't infer the occasion, the photographer's \
intent, or what anyone in the photo is feeling.

On place names: you may use only the place given to you in the user message. \
If no place is given, the caption must contain no location at all — no country, \
no region, no landmark, and no stand-in like "the savannah" or "the tropics" \
standing where a name would go. A photograph does not tell you where it was \
taken, so any place name you supply yourself is a guess, and a confident wrong \
one is the single worst thing this caption can contain.

Naming the place is optional even when you have been given one — use it when it \
reads well, leave it out when the picture is about something else.

Examples of the target register (each written for a photo, place in brackets):
  [Lamu] sunset on Lamu
  [Masai Mara] a leopard crossing the last light
  [no place given] low cloud coming over the ridge
"""

CAPTION_SCHEMA = {
    "type": "object",
    "properties": {
        "caption": {
            "type": "string",
            "description": (
                "The caption clause. One line, lowercase first letter, no date, "
                "no hashtags, no emoji, no exclamation marks."
            ),
        }
    },
    "required": ["caption"],
    "additionalProperties": False,
}


@dataclass
class CaptionDraft:
    """A drafted caption plus everything the curation app needs to badge it."""

    file: str
    caption: str
    place: str | None
    date: datetime | None
    date_source: str | None = None
    flags: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return FLAG_CAPTION_FAILED not in self.flags


def _validate(text: str, max_chars: int) -> str | None:
    """Return a correction message if the caption breaks house style, else None.

    Structured outputs guarantee well-formed JSON, so this checks the things a
    schema can't: register, punctuation, and length.
    """
    if not text.strip():
        return "The caption was empty. Write one short line."
    if "\n" in text.strip():
        return "The caption must be a single line with no line breaks."
    if len(text) > max_chars:
        return f"The caption was {len(text)} characters; keep it under {max_chars}."
    if text[0].isupper():
        return "The caption must start with a lowercase letter."
    if "#" in text:
        return "Remove the hashtag. Captions never contain hashtags."
    if "!" in text:
        return "Remove the exclamation mark."
    if any(unicodedata.category(ch) == "So" for ch in text):
        return "Remove the emoji. Captions never contain emoji."
    if re.search(r"\b(19|20)\d{2}\b", text):
        return "Don't write a year. The date is added separately."
    return None


def _user_content(image: Path, place: str | None) -> list[dict]:
    data = base64.standard_b64encode(image.read_bytes()).decode()
    if place:
        stated = f"Place: {place}. This is confirmed — you may use this name."
    else:
        stated = (
            "Place: not known. Write a caption containing no location of any kind."
        )
    return [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": data},
        },
        {"type": "text", "text": stated},
    ]


def draft_caption(
    image: Path | str,
    place: str | None = None,
    source_for_date: Path | str | None = None,
    cfg: CaptionConfig | None = None,
    client=None,
) -> CaptionDraft:
    """Draft a caption for `image`, reading the date from `source_for_date`.

    `image` should be the processed 1080x1350 JPEG — it is small, already sRGB,
    and is what actually gets posted. The date comes from the raw file, which
    still has its EXIF.
    """
    cfg = cfg or load_config().caption
    image = Path(image)
    when, date_source = read_capture_date(source_for_date or image)

    flags: list[str] = []
    if when is None:
        flags.append(FLAG_NO_DATE)
    if not place:
        flags.append(FLAG_NO_PLACE)

    draft = CaptionDraft(
        file=image.name, caption="", place=place, date=when,
        date_source=date_source, flags=flags,
    )

    if client is None:
        import anthropic

        client = anthropic.Anthropic(api_key=secret("ANTHROPIC_API_KEY"))

    messages = [{"role": "user", "content": _user_content(image, place)}]

    # One retry: the schema makes malformed JSON impossible, so a second attempt
    # is only ever needed for a style violation, and the model gets told which.
    for attempt in range(2):
        response = client.messages.create(
            model=cfg.model,
            max_tokens=cfg.max_tokens,
            system=SYSTEM_PROMPT,
            output_config={
                "format": {"type": "json_schema", "schema": CAPTION_SCHEMA},
                "effort": cfg.effort,
            },
            messages=messages,
        )

        if response.stop_reason == "refusal":
            draft.flags.append(FLAG_CAPTION_FAILED)
            return draft

        raw = next((b.text for b in response.content if b.type == "text"), "")
        try:
            text = json.loads(raw)["caption"].strip()
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
            text, problem = "", "The response was not the expected JSON object."
        else:
            problem = _validate(text, cfg.max_chars)

        if problem is None:
            draft.caption = text.rstrip(".") + date_suffix(when)
            return draft

        if attempt == 0:
            messages += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": f"{problem} Rewrite the caption."},
            ]

    # Both attempts failed style validation. An empty caption is honest; the app
    # badges it and Sjoerd writes one himself.
    draft.flags.append(FLAG_CAPTION_FAILED)
    return draft
