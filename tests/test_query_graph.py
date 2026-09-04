from delaybind_core.query_graph import compile_open_query_graph
from delaybind_core.schema import QueryNodeKind, QueryPlan


def test_plan_compiles_shared_variables_into_open_query_graph_topology():
    plan = QueryPlan(
        plan_id="cindy-chain",
        patterns=[
            {"id": "teacher", "subject": "Cindy", "relation": "teacher", "object": "?teacher"},
            {"id": "mother", "subject": "?teacher", "relation": "mother", "object": "?answer"},
        ],
        answer_contract={"target": "?answer", "type": "ENTITY"},
    )
    graph = compile_open_query_graph(plan)
    teacher = next(node for node in graph.nodes if node.symbol == "?teacher")
    answer = next(node for node in graph.nodes if node.symbol == "?answer")
    assert teacher.kind == QueryNodeKind.VARIABLE
    assert answer.kind == QueryNodeKind.ANSWER
    first, second = graph.edges
    assert first.object_node_id == second.subject_node_id
    assert graph.answer_node_id == answer.id


def test_target_free_chain_infers_terminal_answer_node():
    plan = QueryPlan(
        plan_id="target-free-chain",
        patterns=[
            {"id": "teacher", "subject": "Cindy", "relation": "teacher", "object": "?teacher"},
            {"id": "mother", "subject": "?teacher", "relation": "mother", "object": "?mother"},
        ],
        answer_contract={"target": None, "type": "ENTITY"},
    )
    graph = compile_open_query_graph(plan)
    answer = next(node for node in graph.nodes if node.id == graph.answer_node_id)
    assert answer.symbol == "?mother"
    assert answer.kind == QueryNodeKind.ANSWER
