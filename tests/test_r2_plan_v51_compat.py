"""V5.1 PLAN authority at the R2 member-graph boundary."""

import pytest

from delaybind_core.fact_protocol import parse_plan as parse_v51_plan
from delaybind_core.plan_goal_r2 import parse_v51_member_plan
from delaybind_core.plan_validation import ensure_valid_plan


def test_v51_comparison_plan_is_not_auto_cleaned():
    raw = """Q1
query: Who directed Film A?
output: ?director_a
depends_on: NONE

Q2
query: When was ?director_a born?
output: ?birth_a
depends_on: Q1

Q3
query: Who directed Film B?
output: ?director_b
depends_on: NONE

Q4
query: When was ?director_b born?
output: ?birth_b
depends_on: Q3

Q5
query: Which film has the director who was born later, Film A or Film B?
output: ?later_film
depends_on: Q2, Q4"""
    assert len(ensure_valid_plan(parse_v51_plan(raw)).queries) == 5
    with pytest.raises(ValueError, match="PLAN_DEPENDENCY_TEMPLATE_MISMATCH:Q5"):
        parse_v51_member_plan(raw, "Which film has the director who was born later, Film A or Film B?")


def test_structurally_compatible_computation_query_is_preserved_not_removed():
    raw = """Q1
query: Which films did A direct?
output: ?films
depends_on: NONE

Q2
query: How many films are in ?films?
output: ?count
depends_on: Q1"""
    adapted = parse_v51_member_plan(raw, "How many films did A direct?")
    assert [query.id for query in adapted.queries] == ["Q1", "Q2"]
    assert adapted.queries[1].inputs == {"?films": "Q1"}


def test_v51_structure_validation_still_rejects_dependency_cycles():
    raw = """Q1
query: Who is ?parent's child?
output: ?child
depends_on: Q2

Q2
query: Who is ?child's parent?
output: ?parent
depends_on: Q1"""
    with pytest.raises(ValueError, match="dependency cycle"):
        parse_v51_member_plan(raw, "Who is the parent of the child?")


def test_v51_non_computation_extra_dependency_is_not_silently_discarded():
    raw = """Q1
query: Who directed Film A?
output: ?director
depends_on: NONE

Q2
query: Where is Film B set?
output: ?place
depends_on: Q1"""
    assert len(ensure_valid_plan(parse_v51_plan(raw)).queries) == 2
    with pytest.raises(ValueError, match="PLAN_DEPENDENCY_TEMPLATE_MISMATCH:Q2") as error:
        parse_v51_member_plan(raw, "Where is Film B set?")
    message = str(error.value)
    assert "query variables and producers: NONE" in message
    assert "declared depends_on: Q1" in message
    assert "expected depends_on: NONE" in message
    assert "unused dependencies: Q1" in message


def test_v51_indirect_ancestor_dependency_gets_actionable_repair_feedback():
    raw = """Q1
query: Who directed Film A?
output: ?director
depends_on: NONE

Q2
query: When did ?director begin directing?
output: ?start
depends_on: Q1

Q3
query: Where was ?director born?
output: ?place
depends_on: Q2"""
    assert len(ensure_valid_plan(parse_v51_plan(raw)).queries) == 3
    with pytest.raises(ValueError, match="PLAN_DEPENDENCY_TEMPLATE_MISMATCH:Q3") as error:
        parse_v51_member_plan(raw, "Where was the director of Film A born?")
    message = str(error.value)
    assert "query variables and producers: ?director from Q1" in message
    assert "declared depends_on: Q2" in message
    assert "expected depends_on: Q1" in message
    assert "unused dependencies: Q2" in message
    assert "missing direct dependencies: Q1" in message
    assert "Set depends_on to Q1." in message


def test_v51_indirect_ancestor_dependency_must_also_declare_direct_input():
    raw = """Q1
query: Who directed Film A?
output: ?director
depends_on: NONE

Q2
query: When did ?director begin directing?
output: ?start
depends_on: Q1

Q3
query: Where did ?director live after ?start?
output: ?place
depends_on: Q2"""
    assert len(ensure_valid_plan(parse_v51_plan(raw)).queries) == 3
    with pytest.raises(ValueError, match="PLAN_DEPENDENCY_TEMPLATE_MISMATCH:Q3") as error:
        parse_v51_member_plan(raw, "Where did the director of Film A live after starting to direct?")
    message = str(error.value)
    assert "declared depends_on: Q2" in message
    assert "expected depends_on: Q1,Q2" in message
    assert "unused dependencies: NONE" in message
    assert "missing direct dependencies: Q1" in message


def test_v51_ids_are_mapped_to_internal_member_query_ids():
    raw = """Director
query: Who directed Film A?
output: ?director
depends_on: NONE

Birth
query: Where was ?director born?
output: ?place
depends_on: Director"""
    assert len(ensure_valid_plan(parse_v51_plan(raw)).queries) == 2
    adapted = parse_v51_member_plan(raw, "Where was the director of Film A born?")
    assert [query.id for query in adapted.queries] == ["Q1", "Q2"]
    assert adapted.queries[1].inputs == {"?director": "Q1"}


def test_v51_json_import_remains_accepted_at_plan_boundary():
    raw = """{"schema_version":"v2","plan_id":"legacy","queries":[{"id":"Q1","template":"Who directed Film A?","output":"?director","depends_on":[]}]}"""
    adapted = parse_v51_member_plan(raw, "Who directed Film A?")
    assert adapted.queries[0].template == "Who directed Film A?"
