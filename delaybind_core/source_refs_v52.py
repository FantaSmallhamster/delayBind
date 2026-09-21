"""Exact source permissions for a particular model context, not archive membership."""

import re


class SentenceRefResolver:
    def __init__(self, visible_sources):
        self.visible_sources = set(visible_sources)

    def resolve(self, refs) -> list[str]:
        if not refs or len(refs) != len(set(refs)):
            raise ValueError("EMPTY_OR_DUPLICATE_SOURCE_REFS")
        for ref in refs:
            if not re.fullmatch(r"D[0-9]+(?::(?:S[0-9]+|H[0-9]+)(?::P[1-9][0-9]*)?|@C[0-9]+)", ref):
                raise ValueError(f"INVALID_SENTENCE_REF:{ref}")
            if ref not in self.visible_sources:
                raise ValueError(f"SOURCE_NOT_VISIBLE:{ref}")
        return list(refs)
