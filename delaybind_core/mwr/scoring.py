"""Primary normalization without task-specific date/title/alias rewriting."""
import re
from collections import Counter


def normalize(value):
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " | ".join(sorted(normalize(v) for v in value))
    text = re.sub(r"[^\w\s]", " ", str(value).casefold())
    return " ".join(re.sub(r"\b(a|an|the)\b", " ", text).split())


def score(prediction, gold):
    p = normalize(prediction)
    answers = [normalize(g) for g in gold]
    exact = bool(p) and p in answers
    best = 0.0
    for answer in answers:
        pt, gt = p.split(), answer.split()
        shared = sum((Counter(pt) & Counter(gt)).values())
        best = max(best, 2 * shared / (len(pt) + len(gt)) if pt and gt else 0.0)
    return {"answer_exact": exact, "answer_token_f1": best, "metric_contract": "mwr-unified-v1"}
