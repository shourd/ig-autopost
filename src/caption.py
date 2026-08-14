"""Draft three candidate captions for a photo, using Claude with vision.

Three, not one, because picking is faster than writing and the right register
depends on the picture: the same frame can want a dry factual line, the plain
house voice, or something with a little more music in it. The app shows all
three and Sjoerd clicks one — or writes his own, which stays the default path.

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

# Order matters: `plain` is the house voice and becomes the pre-filled default,
# the other two are alternatives to click. The keys are also the schema keys.
VOICES = ("descriptive", "plain", "poetic")
DEFAULT_VOICE = "plain"

VOICE_LABELS = {
    "descriptive": "descriptive",
    "plain": "plain",
    "poetic": "poetic",
}

SYSTEM_PROMPT = """\
You write one-line captions for a photography account. Given one photograph, \
write three different captions for it — the same picture in three registers, so \
the photographer can pick the one that fits.

The three registers:

- descriptive: concrete and specific about what is actually in the frame. Name \
the thing, the light, the weather, the hour. Dry humour is welcome when the \
picture genuinely offers it — an animal caught mid-indignity, a sign saying \
something absurd — but it must come from the photo, never be bolted on, and \
never be a joke about the viewer or a pun on the location.
- plain: the house voice. Understated and observational, a plain statement of \
what is there. No wordplay, no sentiment, no slogan, nothing that sounds like a \
motivational quote or a stock caption.
- poetic: a little more lyrical — one image, or a bit of rhythm. Still short, \
still about something you can actually see. No abstractions like soul, journey, \
magic, wanderlust, or paradise, and no metaphor that could have been written \
without looking at the photo.

Rules for all three:

- One line each. Short: the house style runs from about three to ten words.
- Start with a lowercase letter.
- No hashtags, no emoji, no exclamation marks, no quotation marks.
- No date, and no year. Those are added afterwards.
- Describe what is in the frame. Don't infer the occasion, the photographer's \
intent, or what anyone in the photo is feeling.
- Three genuinely different lines. Don't hand back the same caption reworded.

On place names: you may use only the place given to you in the user message. \
If no place is given, the captions must contain no location at all — no country, \
no region, no landmark, and no stand-in like "the savannah" or "the tropics" \
standing where a name would go. A photograph does not tell you where it was \
taken, so any place name you supply yourself is a guess, and a confident wrong \
one is the single worst thing these captions can contain.

Naming the place is optional even when you have been given one — use it when it \
reads well, leave it out when the picture is about something else.

An example of the target register, for a photo of a leopard at dusk [Masai Mara]:
  descriptive: a leopard crossing the road at last light
  plain: leopard on the track, Masai Mara
  poetic: the light goes, and the leopard goes with it
"""

CAPTION_SCHEMA = {
    "type": "object",
    "properties": {
        voice: {
            "type": "string",
            "description": (
                f"The {voice} caption. One line, lowercase first letter, no "
                "date, no hashtags, no emoji, no exclamation marks."
            ),
        }
        for voice in VOICES
    },
    "required": list(VOICES),
    "additionalProperties": False,
}


@dataclass
class CaptionDraft:
    """Three drafted captions plus everything the curation app needs to badge them."""

    file: str
    caption: str
    place: str | None
    date: datetime | None
    options: dict[str, str] = field(default_factory=dict)
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


def _user_content(images: list[Path], place: str | None) -> list[dict]:
    blocks: list[dict] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.standard_b64encode(image.read_bytes()).decode(),
            },
        }
        for image in images
    ]
    if place:
        stated = f"Place: {place}. This is confirmed — you may use this name."
    else:
        stated = (
            "Place: not known. Write captions containing no location of any kind."
        )
    if len(images) > 1:
        stated = (
            f"These {len(images)} photographs are one carousel post, shown in this "
            f"order. Caption the set, not any single frame.\n{stated}"
        )
    blocks.append({"type": "text", "text": stated})
    return blocks


def draft_caption(
    image: Path | str | list[Path | str],
    place: str | None = None,
    source_for_date: Path | str | None = None,
    cfg: CaptionConfig | None = None,
    client=None,
) -> CaptionDraft:
    """Draft three captions for `image`, reading the date from `source_for_date`.

    `image` should be the processed 1080x1350 JPEG — it is small, already sRGB,
    and is what actually gets posted. The date comes from the raw file, which
    still has its EXIF. Pass a list for a carousel: one caption covers the whole
    post, so the model is shown every photo in it, in order.
    """
    cfg = cfg or load_config().caption
    images = [Path(p) for p in ([image] if isinstance(image, (str, Path)) else image)]
    image = images[0]
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

    messages = [{"role": "user", "content": _user_content(images, place)}]
    suffix = date_suffix(when)

    # Accepted lines survive across attempts, so one bad voice on the retry
    # can't discard two good ones from the first pass.
    accepted: dict[str, str] = {}

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
            payload = json.loads(raw)
            candidates = {v: str(payload[v]).strip() for v in VOICES}
        except (json.JSONDecodeError, KeyError, TypeError):
            problems = {v: "The response was not the expected JSON object." for v in VOICES}
        else:
            problems = {}
            for voice in VOICES:
                if voice in accepted:
                    continue
                problem = _validate(candidates[voice], cfg.max_chars)
                if problem is None:
                    accepted[voice] = candidates[voice].rstrip(".") + suffix
                else:
                    problems[voice] = problem

        if not problems:
            break

        if attempt == 0:
            correction = "\n".join(f"{v}: {p}" for v, p in problems.items())
            messages += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": f"{correction}\nRewrite those captions."},
            ]

    if not accepted:
        # Every voice failed style validation twice. An empty caption is honest;
        # the app badges it and Sjoerd writes one himself.
        draft.flags.append(FLAG_CAPTION_FAILED)
        return draft

    draft.options = {v: accepted[v] for v in VOICES if v in accepted}
    draft.caption = accepted.get(DEFAULT_VOICE) or next(iter(draft.options.values()))
    return draft
