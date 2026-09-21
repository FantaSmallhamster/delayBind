"""Count the serialized context with an explicit tokenizer; never estimate words."""

import json


class TokenCounter:
    def __init__(self, tokenizer=None, *, encoding_name: str = "cl100k_base"):
        if tokenizer is None:
            try:
                import tiktoken
            except ImportError as exc:
                raise RuntimeError("V5.2 requires tiktoken (install delaybind-v5[v52]) or an explicit tokenizer") from exc
            tokenizer = tiktoken.get_encoding(encoding_name)
        self.tokenizer = tokenizer
        self.identity = getattr(tokenizer, "name_or_path", getattr(tokenizer, "name", type(tokenizer).__name__))

    def count(self, text: str) -> int:
        try:
            return len(self.tokenizer.encode(text, disallowed_special=()))
        except TypeError:
            return len(self.tokenizer.encode(text))

    def serialized(self, value) -> int:
        return self.count(json.dumps(value, ensure_ascii=False, sort_keys=True))
