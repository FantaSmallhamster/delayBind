"""Conservative text matching shared by UPDATE and VERIFY provenance gates."""

from __future__ import annotations

import re
import unicodedata
from typing import Any


def normalized_span(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).split())


def contains_normalized_span(text: Any, span: Any) -> bool:
    normalized_text = normalized_span(text)
    normalized_candidate = normalized_span(span)
    return bool(normalized_candidate) and normalized_candidate in normalized_text
