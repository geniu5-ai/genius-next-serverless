"""Humanized WhatsApp reply splitting.

Short replies stay whole; long replies are split at thought boundaries
(paragraphs and sentences), never mid-sentence.  A reply that fits in a
single natural block is returned as one message — a human does not chop
"ok!" into pieces.  Blocks are capped by count and by target character
size, so a turn delivers at most ``max_blocks`` natural messages.
"""

from __future__ import annotations

import re

_SENTENCE_BOUNDARY = r"(?<=[.!?…])\s+"

# Common Portuguese abbreviations whose trailing "." is not a sentence end
# (e.g. "Fala com a Dra." must not split before "Ana chega amanhã.").  Not
# meant to be exhaustive -- a missed abbreviation just means an occasional
# extra split, which is harmless; the goal is "natural", not perfect.
_ABBREVIATIONS = {
    "dr", "dra", "sr", "sra", "srs", "sras", "prof", "profa",
    "eng", "engo", "enga", "exmo", "exma", "ltda", "etc", "obs",
}
_TRAILING_ABBREVIATION = re.compile(r"(\w+)\.$")


def _ends_in_abbreviation(sentence: str) -> bool:
    """True when ``sentence`` ends in a known abbreviation, not a real stop."""
    match = _TRAILING_ABBREVIATION.search(sentence)
    return bool(match) and match.group(1).lower() in _ABBREVIATIONS


def _split_sentences(paragraph: str) -> list[str]:
    """Split on sentence boundaries, then re-merge false splits caused by
    an abbreviation's period (see ``_ABBREVIATIONS``)."""
    raw = [s.strip() for s in re.split(_SENTENCE_BOUNDARY, paragraph) if s.strip()]
    merged: list[str] = []
    for sentence in raw:
        if merged and _ends_in_abbreviation(merged[-1]):
            merged[-1] = f"{merged[-1]} {sentence}"
        else:
            merged.append(sentence)
    return merged


def split_human_reply(
    text: str,
    *,
    max_blocks: int = 3,
    target_chars: int = 220,
) -> list[str]:
    """Split one reply into 1..max_blocks natural WhatsApp-sized messages.

    Args:
        text: The full reply drafted for the customer.
        max_blocks: Hard ceiling of returned messages (1..8).
        target_chars: Preferred block size; blocks are allowed to exceed it
            when a single sentence is longer than the target.

    Returns:
        Non-empty, trimmed messages; ``[]`` only when ``text`` is blank.

    Rules (think human, not arithmetic):
        - blank input -> []
        - text <= target_chars -> one block
        - split on paragraph breaks first, then on sentence boundaries
        - never split inside a sentence
        - never return an empty or whitespace-only block
        - if the natural split exceeds ``max_blocks``, merge the trailing
          blocks (join with a space) until the count fits — never cut a
          sentence to force a smaller count
    """
    # Normalize line endings and trim
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []

    # Short single-paragraph reply fits in one block
    if "\n" not in text and len(text) <= target_chars:
        return [text]

    # Split into paragraphs on one or more newlines
    paragraphs = [p.strip() for p in re.split(r"\n+", text) if p.strip()]

    # Build candidate blocks from paragraphs
    blocks: list[str] = []
    for para in paragraphs:
        if len(para) <= target_chars:
            blocks.append(para)
            continue

        # Split paragraph into sentences, re-merging abbreviation false-splits
        sentences = _split_sentences(para)

        # Greedily group sentences into blocks not exceeding target_chars
        current = ""
        for sent in sentences:
            # If a single sentence is longer than target_chars, it becomes its own block
            if len(sent) > target_chars:
                # Flush any accumulated current block first
                if current:
                    blocks.append(current)
                    current = ""
                blocks.append(sent)
                continue

            # Try to add this sentence to the current block
            candidate = f"{current} {sent}".strip() if current else sent
            if len(candidate) <= target_chars:
                current = candidate
            else:
                # Current block is full; start a new one with this sentence
                if current:
                    blocks.append(current)
                current = sent

        # Flush any remaining current block
        if current:
            blocks.append(current)

    # If we have more blocks than allowed, merge trailing blocks from the end
    while len(blocks) > max_blocks:
        # Merge the last two blocks (join with a space)
        merged = f"{blocks[-2]} {blocks[-1]}".strip()
        # Replace the last two with the merged block
        blocks = blocks[:-2] + [merged]

    # Ensure all blocks are non-empty and trimmed
    return [b.strip() for b in blocks if b.strip()]
