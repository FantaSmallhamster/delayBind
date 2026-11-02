"""Small open relation ontology used to type query-graph endpoints."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RelationSignature:
    subject_type: str
    object_type: str


_SIGNATURES: dict[str, RelationSignature] = {
    "child": RelationSignature("PERSON", "PERSON"),
    "country": RelationSignature("ENTITY", "COUNTRY"),
    "country of citizenship": RelationSignature("PERSON", "NATIONALITY"),
    "date of birth": RelationSignature("PERSON", "DATE"),
    "date of death": RelationSignature("PERSON", "DATE"),
    "director": RelationSignature("FILM", "PERSON"),
    "father": RelationSignature("PERSON", "PERSON"),
    "mother": RelationSignature("PERSON", "PERSON"),
    "performer": RelationSignature("CREATIVE_WORK", "AGENT"),
    "place of birth": RelationSignature("PERSON", "LOCATION"),
    "place of death": RelationSignature("PERSON", "LOCATION"),
    "publication date": RelationSignature("CREATIVE_WORK", "DATE"),
}


def relation_signature(relation: str) -> RelationSignature:
    """Return a conservative signature, falling back to untyped entities."""
    key = " ".join(relation.casefold().split())
    return _SIGNATURES.get(key, RelationSignature("ENTITY", "ENTITY"))
