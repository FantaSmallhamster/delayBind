from delaybind_core import ManifestEntry, RawArchive, SQLiteEventStore, build_manifest, canonicalize_record


def test_flashrag_record_is_converted_and_manifest_is_deterministic():
    sample = canonicalize_record(
        {
            "id": "dev_0",
            "question": "Who is the director?",
            "golden_answers": ["Martin Lee"],
            "metadata": {
                "type": "compositional",
                "supporting_facts": {"title": ["Film A"], "sent_id": [0]},
                "context": {
                    "title": ["Film A"],
                    "content": [["Film A was directed by Martin Lee."]],
                },
            },
        },
        dataset_id="2wiki",
    )
    first = build_manifest(sample, order="original")
    second = build_manifest(sample, order="original")
    assert first.manifest_id == second.manifest_id
    assert first.entries[0].source_ref == "dev_0:d00000:s0"
    assert first.entries[0].text_sha256
    assert sample.supporting_facts == [("Film A", 0)]


def test_reverse_manifest_keeps_source_identity_but_changes_stream_position():
    sample = canonicalize_record(
        {
            "_id": "q1",
            "question": "q",
            "context": [["A", ["a1"]], ["B", ["b1"]]],
        }
    )
    manifest = build_manifest(sample, order="reverse")
    assert [entry.title for entry in manifest.entries] == ["B", "A"]
    assert manifest.entries[0].source_ref == "q1:d00001:s0"


def test_raw_neighborhood_does_not_cross_document_boundary():
    entries = [
        ManifestEntry(
            source_ref="q:d1:s0", dataset_id="d", sample_id="q", document_id="d1",
            title="A", sentence_id=0, text="fact in A", stream_position=0,
        ),
        ManifestEntry(
            source_ref="q:d2:s0", dataset_id="d", sample_id="q", document_id="d2",
            title="B", sentence_id=0, text="fact in B", stream_position=1,
        ),
    ]
    store = SQLiteEventStore()
    archive = RawArchive(store, "neighborhood-run")
    archive.append(entries)
    assert [entry.source_ref for entry in archive.fetch("q:d1:s0", neighborhood=1)] == ["q:d1:s0"]


def test_interleaved_manifest_round_robins_sentences_and_is_reproducible():
    sample = canonicalize_record(
        {
            "id": "q-interleave",
            "question": "q",
            "supporting_facts": [["A", 0], ["B", 0]],
            "context": [["A", ["a0", "a1"]], ["B", ["b0", "b1"]]],
        }
    )
    first = build_manifest(sample, order="interleaved")
    second = build_manifest(sample, order="interleaved")
    assert [entry.text for entry in first.entries] == ["a0", "b0", "a1", "b1"]
    assert first.manifest_id == second.manifest_id


def test_distant_manifest_puts_supporting_documents_around_distractor():
    sample = canonicalize_record(
        {
            "id": "q-distant",
            "question": "q",
            "supporting_facts": [["A", 0], ["C", 0]],
            "context": [["A", ["a"]], ["B", ["b"]], ["C", ["c"]]],
        }
    )
    manifest = build_manifest(sample, order="distant")
    assert [entry.title for entry in manifest.entries] == ["A", "B", "C"]


def test_expanded_context_combines_document_and_read_prefix_mentions():
    entries = [
        ManifestEntry(
            source_ref="q:d1:s0", dataset_id="d", sample_id="q", document_id="d1",
            title="A", sentence_id=0, text="Martin Lee directed A.", stream_position=0,
        ),
        ManifestEntry(
            source_ref="q:d1:s1", dataset_id="d", sample_id="q", document_id="d1",
            title="A", sentence_id=1, text="Another sentence.", stream_position=1,
        ),
        ManifestEntry(
            source_ref="q:d2:s0", dataset_id="d", sample_id="q", document_id="d2",
            title="B", sentence_id=0, text="Martin Lee was born in 1948.", stream_position=2,
        ),
    ]
    store = SQLiteEventStore()
    archive = RawArchive(store, "expanded-run")
    archive.append(entries)
    expanded = archive.expanded_context("q:d1:s0", mentions=["Martin Lee"], limit=10)
    assert [entry.source_ref for entry in expanded] == ["q:d1:s0", "q:d1:s1", "q:d2:s0"]
