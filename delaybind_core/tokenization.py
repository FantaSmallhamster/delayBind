"""Load an exported model tokenizer without downloading model weights."""

from pathlib import Path


class TokenizerJSON:
    def __init__(self, path: str | Path):
        from tokenizers import Tokenizer

        self.tokenizer = Tokenizer.from_file(str(path))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()

    def encode(self, text: str) -> list[int]:
        return self.tokenizer.encode(text).ids

    def decode(self, tokens: list[int]) -> str:
        # Keep the original text's special-token strings, as HF decode does
        # by default in the upstream ReMemR1 input pipeline.
        return self.tokenizer.decode(tokens, skip_special_tokens=False)
