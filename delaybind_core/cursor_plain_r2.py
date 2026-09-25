"""Exact sequential token chunks for fact-only R2 UPDATE input."""

from dataclasses import dataclass

from .schema_v52 import digest


PLAIN_CHUNK_VERSION = "baseline-token-chunks-v1"


@dataclass(frozen=True)
class PlainTokenChunk:
    chunk_index: int
    token_start: int
    token_end: int
    chunk_text: str


class PlainTokenChunkCursor:
    def __init__(self, context, tokenizer, *, chunk_size, state=None):
        if context is None or tokenizer is None:
            raise ValueError("PLAIN_CHUNKS_REQUIRE_CONTEXT_AND_TOKENIZER")
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if not callable(getattr(tokenizer, "encode", None)) or not callable(getattr(tokenizer, "decode", None)):
            raise ValueError("PLAIN_CHUNKS_REQUIRE_TOKENIZER_ENCODE_DECODE")
        self.tokenizer = tokenizer
        self.chunk_size = chunk_size
        stripped_context = context.strip()
        self.tokens = tokenizer.encode(stripped_context)
        self.tokenizer_identity = getattr(tokenizer, "name_or_path", getattr(tokenizer, "name", type(tokenizer).__name__))
        self.source_version = digest([PLAIN_CHUNK_VERSION, stripped_context, chunk_size,
                                      self.tokenizer_identity, self.tokens])
        self.position = 0
        self.window_index = 0
        if state:
            if state.get("source_version") != self.source_version or state.get("mode") != PLAIN_CHUNK_VERSION:
                raise ValueError("CURSOR_SOURCE_OR_MODE_CHANGED")
            self.position = state["position"]
            self.window_index = state["window_index"]
            if (not 0 <= self.position <= len(self.tokens)
                    or self.position != min(self.window_index * chunk_size, len(self.tokens))
                    or self.window_index != (self.position + chunk_size - 1) // chunk_size):
                raise ValueError("CURSOR_STATE_INVALID")

    @property
    def exhausted(self):
        return self.position >= len(self.tokens)

    def state(self):
        return dict(mode=PLAIN_CHUNK_VERSION, source_version=self.source_version,
                    tokenizer=self.tokenizer_identity, chunk_size=self.chunk_size,
                    position=self.position, window_index=self.window_index,
                    total_tokens=len(self.tokens))

    def next_chunk(self):
        if self.exhausted:
            return None
        start = self.position
        end = min(start + self.chunk_size, len(self.tokens))
        chunk = PlainTokenChunk(self.window_index, start, end,
                                self.tokenizer.decode(self.tokens[start:end]))
        self.position = end
        self.window_index += 1
        return chunk
