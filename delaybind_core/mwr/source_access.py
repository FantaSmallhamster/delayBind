"""Source capability boundary. No search, document fetch, or enumeration API."""
import re
from dataclasses import dataclass
from .common import digest, dump, ProtocolError, ResourceLimit
from .models import SourceSpan


def span_id(span):
    return "s-" + digest(span.model_dump())[:24]


def _prefix(text, limit, tokenizer):
    if len(tokenizer.encode(text)) <= limit:
        return len(text)
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(tokenizer.encode(text[:mid])) <= limit:
            lo = mid
        else:
            hi = mid - 1
    if lo == 0:
        raise ResourceLimit("one character exceeds source token budget")
    return lo


@dataclass(frozen=True)
class Anchor:
    doc_id: str
    start: int
    end: int
    source_hash: str
    title: str
    original_ref: str | None = None


class SourceAccess:
    def __init__(self, inp, tokenizer, *, manifest=None, neighbor_anchors=1, log=None):
        self._tokenizer = tokenizer
        self._documents = {}
        self._anchors = []
        self._by_doc = {}
        self._read_end = {}
        self._cursor = [0, 0]  # anchor index and character offset
        self.windows = 0
        self.neighbor_anchors = neighbor_anchors
        self.log = log or (lambda *args: None)
        self.lookup_tokens = 0
        self.on_meter = lambda: None
        self._state = None
        self._staged = {}
        self._window_refs = {}
        self._unique_spans = set()
        self._shown_ranges = {}
        if manifest is not None:
            groups = {}
            for e in manifest.entries:
                groups.setdefault(e.document_id, []).append(e)
            mapped = {}
            for entries in groups.values():
                entries = sorted(entries, key=lambda e: (e.sentence_id, e.char_start))
                text = "\n".join(e.text for e in entries)
                doc_id = self._document(entries[0].title, text)
                pos = 0
                for e in entries:
                    mapped[e.source_ref] = Anchor(doc_id, pos, pos + len(e.text), digest(text), e.title, e.source_ref)
                    pos += len(e.text) + 1
            self._anchors = [mapped[e.source_ref] for e in manifest.entries if e.text]
        elif inp.context is not None:
            text = inp.context
            headers = list(re.finditer(r"(?m)^Document\s+\d+:\s*\n", text))
            boundaries = sorted(set([0, len(text)] + [m.start() for m in headers]))
            for start, end in zip(boundaries, boundaries[1:]):
                raw = text[start:end]
                # Display numbering is not stable document identity.
                body = re.sub(r"^Document\s+\d+:\s*\n", "", raw, count=1)
                if body:
                    self._add_document("", body, None)
        else:
            for doc in inp.documents:
                self._add_document(doc.title, "\n".join(doc.sentences), doc.sentences)
        for a in self._anchors:
            self._by_doc.setdefault(a.doc_id, []).append(a)
        for anchors in self._by_doc.values():
            anchors.sort(key=lambda a: a.start)
        self.identity = digest([(a.doc_id, a.start, a.end, a.source_hash) for a in self._anchors])

    def _document(self, title, text):
        doc_id = "d-" + digest([title, text])[:24]
        self._documents[doc_id] = text
        return doc_id

    def _add_document(self, title, text, sentences):
        doc_id = self._document(title, text)
        if sentences is not None:
            start = 0
            for sentence in sentences:
                if sentence:
                    self._anchors.append(Anchor(doc_id, start, start + len(sentence), digest(text), title))
                start += len(sentence) + 1
        else:
            start = 0
            while start < len(text):
                n = _prefix(text[start:], 500, self._tokenizer)
                self._anchors.append(Anchor(doc_id, start, start + n, digest(text), title))
                start += n

    @property
    def exhausted(self):
        return self._cursor[0] >= len(self._anchors)

    def position(self):
        return list(self._cursor)

    def checkpoint(self):
        return {"identity": self.identity, "cursor": self.position(), "windows": self.windows,
                "read_end": self._read_end, "lookup_tokens": self.lookup_tokens,
                "unique_spans": sorted(self._unique_spans), "shown_ranges": self._shown_ranges}

    def restore(self, checkpoint):
        if checkpoint["identity"] != self.identity:
            raise ProtocolError("source content/order changed")
        self._cursor = list(checkpoint["cursor"])
        self.windows = checkpoint["windows"]
        self._read_end = dict(checkpoint["read_end"])
        self.lookup_tokens = checkpoint["lookup_tokens"]
        self._unique_spans = set(checkpoint["unique_spans"])
        self._shown_ranges = dict(checkpoint.get("shown_ranges", {}))

    def next_window(self, budget):
        if self.exhausted:
            return None
        self._window_refs = {}
        rows, used = [], 0
        while not self.exhausted and used < budget:
            i, offset = self._cursor
            anchor = self._anchors[i]
            start = anchor.start + offset
            text = self._documents[anchor.doc_id][start:anchor.end]
            total = len(self._tokenizer.encode(text))
            # Preserve official sentence/anchor boundaries whenever the complete
            # unit fits a fresh window. Only oversized units require fragments.
            if rows and offset == 0 and budget - used < total <= budget:
                break
            try:
                n = _prefix(text, budget - used, self._tokenizer)
            except ResourceLimit:
                if rows:
                    break
                raise
            end = start + n
            span = SourceSpan(doc_id=anchor.doc_id, start=start, end=end, source_hash=anchor.source_hash)
            ref = span_id(span)
            self._window_refs[ref] = span
            rows.append({"source_ref": ref, "title": anchor.title, "text": text[:n]})
            used += len(self._tokenizer.encode(text[:n]))
            # Per-anchor read coverage avoids interleaved-manifest gaps.
            key = f"{anchor.doc_id}:{anchor.start}"
            self._read_end[key] = max(self._read_end.get(key, anchor.start), end)
            self._cursor = [i + 1, 0] if end == anchor.end else [i, end - anchor.start]
        self.windows += 1
        return rows

    def proposal_spans(self, refs):
        if not refs or any(ref not in self._window_refs for ref in refs):
            raise ProtocolError("proposal source is empty, unread, or outside this window")
        return [self._window_refs[r] for r in dict.fromkeys(refs)]

    def authorize(self, state, staged=None):
        self._state, self._staged = state, staged or {}

    def commit(self, state):
        self.authorize(state)
        self._window_refs = {}

    def _validate_span(self, span):
        text = self._documents.get(span.doc_id)
        if text is None or digest(text) != span.source_hash or span.start >= span.end:
            raise ProtocolError("source hash/range mismatch")
        anchors = self._by_doc[span.doc_id]
        a = next((a for a in anchors if a.start <= span.start and span.end <= a.end), None)
        if a is None or span.end > self._read_end.get(f"{a.doc_id}:{a.start}", a.start):
            raise ProtocolError("source is not in the read prefix")
        return a

    def preview_for_evidence(self, evidence_ids, *, state_version):
        """Internal packing primitive; same authorization as lookup; no model access."""
        if self._state is None or self._state.version != state_version:
            raise ProtocolError("stale state version")
        pool = {**self._state.evidence, **self._staged}
        if any(eid not in pool for eid in evidence_ids):
            raise ProtocolError("evidence ID is not live or staged")
        spans = {}
        for eid in evidence_ids:
            for span in pool[eid].source_spans:
                anchor = self._validate_span(span)
                spans[span_id(span)] = (span, False)
                doc_anchors = self._by_doc[span.doc_id]
                i = doc_anchors.index(anchor)
                for other in doc_anchors[max(0, i-self.neighbor_anchors):i+self.neighbor_anchors+1]:
                    if other is anchor:
                        continue
                    end = min(other.end, self._read_end.get(f"{other.doc_id}:{other.start}", other.start))
                    if end > other.start:
                        ns = SourceSpan(doc_id=other.doc_id, start=other.start, end=end, source_hash=other.source_hash)
                        spans.setdefault(span_id(ns), (ns, True))
        merged = []
        for s, neighbor in sorted(spans.values(), key=lambda item: (item[0].doc_id, item[0].start, item[0].end)):
            if merged and merged[-1][0].doc_id == s.doc_id and s.start < merged[-1][0].end:
                previous, old_neighbor = merged.pop()
                merged.append((previous.model_copy(update={"end":max(previous.end,s.end)}), old_neighbor or neighbor))
            else:
                merged.append((s, neighbor))
        return [{"source_ref": span_id(s), "doc_id": s.doc_id, "start": s.start, "end": s.end,
                 "source_hash": s.source_hash, "text": self._documents[s.doc_id][s.start:s.end],
                 "neighborhood": neighbor} for s, neighbor in merged]

    def storage_metrics(self):
        unique_tokens = 0
        for doc_id, ranges in self._shown_ranges.items():
            merged = []
            for start, end in sorted(ranges):
                if merged and start <= merged[-1][1]:
                    merged[-1][1] = max(end, merged[-1][1])
                else:
                    merged.append([start,end])
            unique_tokens += sum(len(self._tokenizer.encode(self._documents[doc_id][start:end])) for start,end in merged)
        return {"raw_archive_bytes":sum(len(t.encode()) for t in self._documents.values()),
                "unique_source_tokens":unique_tokens,"cumulative_source_tokens":self.lookup_tokens}


    def lookup_for_evidence(self, evidence_ids, purpose, budget, state_version):
        if purpose not in {"BIND", "ANSWER"}:
            raise ProtocolError("lookup purpose must be BIND or ANSWER")
        rows = self.preview_for_evidence(evidence_ids, state_version=state_version)
        count = sum(len(self._tokenizer.encode(r["text"])) for r in rows)
        if count > budget:
            raise ResourceLimit("source lookup exceeds token budget")
        self.lookup_tokens += count
        self.on_meter()
        self._unique_spans.update(r["source_ref"] for r in rows)
        for row in rows:
            ranges = self._shown_ranges.setdefault(row["doc_id"], [])
            interval = [row["start"], row["end"]]
            if interval not in ranges: ranges.append(interval)
        self.log("SOURCE_LOOKUP", {"purpose": purpose, "evidence_ids": evidence_ids,
                 "state_version": state_version, "source_refs": [r["source_ref"] for r in rows],
                 "tokens": count, "cumulative_tokens": self.lookup_tokens,
                 "unique_source_spans": len(self._unique_spans)})
        return rows
