"""Encode a JSONB value once, refusing what PostgreSQL would reject, without quoting the value."""

from __future__ import annotations

import json
import re

from psycopg.types.json import Jsonb

# json.dumps writes U+0000 as the escape \u0000, which JSONB rejects. An even run of backslashes
# before it (none included) means the escape is real, not the literal text "\u0000" in a string.
_NUL_ESCAPE = re.compile(r"(?<!\\)(?:\\\\)*\\u0000")


def encode_jsonb(value: object, *, what: str) -> Jsonb:
    """Serialize ``value`` once and wrap the text for psycopg.

    Raises:
        ValueError: ``value`` holds a NUL character, a non-finite float, a circular reference or
            something ``json`` cannot encode. The message names ``what`` and never the value.
    """
    try:
        text = json.dumps(value, allow_nan=False, ensure_ascii=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError(f"{what} is not JSON-serializable ({type(exc).__name__}).") from None
    if _NUL_ESCAPE.search(text):
        raise ValueError(f"{what} contains a NUL character, which PostgreSQL JSONB cannot store.")
    return Jsonb(value, dumps=lambda _obj: text)
