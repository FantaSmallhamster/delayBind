"""
V14 entity disambiguation detector.
Scans working memory text for potentially ambiguous entity pairs
(same surname, substring overlap) and generates a specific reminder.
"""
import re
from typing import List, Tuple

# Common words to filter out when extracting entities
_STOP_WORDS = {
    "the", "a", "an", "and", "or", "of", "in", "on", "at", "to", "for",
    "with", "by", "from", "as", "is", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "must", "shall", "can", "need", "dare",
    "ought", "used", "united", "kingdom", "states", "republic", "empire",
    "city", "town", "village", "district", "province", "state", "county",
    "region", "area", "part", "section", "zone", "belt", "corridor",
    "north", "south", "east", "west", "central", "upper", "lower",
    "new", "old", "great", "little", "big", "small", "high", "low",
    "first", "second", "third", "last", "next", "previous", "former",
    "latter", "same", "different", "other", "another", "such", "many",
    "much", "few", "little", "several", "various", "different",
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    "born", "died", "death", "birth", "age", "year", "years",
    "king", "queen", "prince", "princess", "emperor", "empress",
    "duke", "duchess", "count", "countess", "lord", "lady", "sir",
    "president", "prime", "minister", "governor", "mayor", "chairman",
    "film", "movie", "book", "novel", "song", "album", "play",
    "directed", "written", "produced", "starred", "released",
    "yes", "no", "none", "unknown", "mentioned", "memory", "working",
    "fact", "facts", "source", "sources", "evidence", "query",
    "result", "results", "output", "input", "data", "information",
}


def extract_entities(text: str) -> List[str]:
    """Extract capitalized multi-word phrases that might be entity names."""
    # Match sequences of capitalized words (2+ words)
    pattern = r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b'
    matches = re.findall(pattern, text)
    # Filter out stop words and very short phrases
    entities = []
    for m in matches:
        words = m.lower().split()
        if len(words) < 2:
            continue
        if any(w in _STOP_WORDS for w in words):
            # Keep if at least one content word
            content = [w for w in words if w not in _STOP_WORDS]
            if len(content) < 2:
                continue
        if m not in entities:
            entities.append(m)
    return entities


def find_ambiguous_pairs(entities: List[str]) -> List[Tuple[str, str]]:
    """Find pairs of entities that might be confused (same surname, substring)."""
    pairs = []
    for i, e1 in enumerate(entities):
        for e2 in entities[i+1:]:
            if e1 == e2:
                continue
            # Same surname (last word)
            s1 = e1.split()[-1].lower()
            s2 = e2.split()[-1].lower()
            if s1 == s2 and len(e1.split()) >= 2 and len(e2.split()) >= 2:
                pairs.append((e1, e2))
                continue
            # Substring overlap (one contains the other)
            if e1.lower() in e2.lower() or e2.lower() in e1.lower():
                pairs.append((e1, e2))
                continue
            # First name same (for people)
            f1 = e1.split()[0].lower()
            f2 = e2.split()[0].lower()
            if f1 == f2 and len(e1.split()) >= 2 and len(e2.split()) >= 2:
                pairs.append((e1, e2))
    return pairs


def generate_disambiguation_reminder(memory_text: str) -> str:
    """Generate a specific disambiguation reminder if ambiguous entities found."""
    entities = extract_entities(memory_text)
    if len(entities) < 2:
        return ""
    pairs = find_ambiguous_pairs(entities)
    if not pairs:
        return ""
    # Limit to at most 3 pairs to avoid overloading
    pairs = pairs[:3]
    pair_strs = [f'"{e1}" and "{e2}"' for e1, e2 in pairs]
    reminder = (
        f"\n\nDETECTED POTENTIALLY AMBIGUOUS ENTITIES in working memory: "
        f"{', '.join(pair_strs)}. "
        f"Before answering, verify which entity the question refers to by "
        f"checking distinguishing context (birth/death dates, nationality, "
        f"occupation, role). Do NOT transfer attributes between these entities."
    )
    return reminder


# Test
if __name__ == "__main__":
    test_memory = """
    F1 | D1@C0 | Alexander Hamilton was born in 1755 in the West Indies.
    F2 | D2@C0 | Hamilton (1998 film) is a Swedish action film directed by Harald Zwart.
    F3 | D3@C0 | Lewis Hamilton won the 2008 Formula One World Championship.
    F4 | D4@C0 | John Smith was born in London in 1900.
    F5 | D5@C0 | Jane Smith died in Paris in 1980.
    """
    entities = extract_entities(test_memory)
    print("Entities:", entities)
    pairs = find_ambiguous_pairs(entities)
    print("Ambiguous pairs:", pairs)
    reminder = generate_disambiguation_reminder(test_memory)
    print("Reminder:", reminder)
