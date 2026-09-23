"""Parse R2-only optional PLAN fields without changing the shared V5.1 wire parser."""

import json
import re


def strip_upstream_only(raw):
    """Return legacy PLAN text and explicit per-query upstream permissions."""
    if raw.lstrip().startswith("{"):
        data = json.loads(raw)
        flags = {}
        for row in data.get("queries", []):
            if "allow_upstream_only" in row:
                value = row.pop("allow_upstream_only")
                if not isinstance(value, bool):
                    raise ValueError("INVALID_UPSTREAM_ONLY_BOOLEAN")
                flags[row["id"]] = value
        return json.dumps(data), flags
    flags, rows, qid = {}, [], None
    for line in raw.splitlines():
        clean = line.strip()
        if re.fullmatch(r"Q[0-9]+", clean):
            qid = clean
        match = re.fullmatch(r"allow_upstream_only\s*:\s*(\S+)", clean)
        if match:
            if qid is None or qid in flags or match[1].lower() not in {"true", "false"}:
                raise ValueError("INVALID_OR_DUPLICATE_UPSTREAM_ONLY_FIELD")
            flags[qid] = match[1].lower() == "true"
        else:
            rows.append(line)
    return "\n".join(rows), flags
