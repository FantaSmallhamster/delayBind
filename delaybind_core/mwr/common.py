import hashlib
import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from . import SCHEMA_VERSION, PROMPT_VERSION


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256((value if isinstance(value, str) else dump(value)).encode()).hexdigest()


class ResourceLimit(RuntimeError):
    pass


class ProtocolError(ValueError):
    pass


def tokenizer_fingerprint(tokenizer):
    if tokenizer is None:
        raise ValueError("MWR requires the actual model tokenizer; pass tokenizer_path")
    if hasattr(tokenizer, "fingerprint"):
        return tokenizer.fingerprint
    if hasattr(tokenizer, "tokenizer") and hasattr(tokenizer.tokenizer, "to_str"):
        return digest(tokenizer.tokenizer.to_str())
    # Deterministic test adapters must declare an identity; never silently use chars.
    raise ValueError("tokenizer must expose fingerprint or tokenizer.to_str()")


def run_contract(config, question, documents, client, reader, tokenizer, plan=None):
    root = Path(__file__).resolve().parents[1]
    source_hash = digest({str(p.relative_to(root)): digest(p.read_text())
                          for p in sorted(root.rglob("*.py"))})
    def api(c):
        cfg = getattr(c, "config", None)
        if cfg is None:
            return {"test_adapter": type(c).__qualname__}
        raw = asdict(cfg) if is_dataclass(cfg) else dict(cfg)
        return {k: v for k, v in raw.items() if k != "api_key"}
    return {"schema_version": SCHEMA_VERSION, "prompt_version": PROMPT_VERSION,
            "source_hash": source_hash, "config": asdict(config),
            "input_hash": digest([question, documents]), "initial_plan": plan,
            "api": api(client), "reader_api": api(reader or client),
            "tokenizer_hash": tokenizer_fingerprint(tokenizer),
            "cache_policy": "current-live-input-only; versioned request namespace"}
