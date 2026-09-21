"""Deterministic core for the V5 delayed-binding evidence runtime."""

from .archive import FutureSourceAccessError, RawArchive
from .cursor import ReadCursor, ReadWindow, TextReadCursor, TextTokenizer
from .agents import HighLevelAgent, LowLevelAgent
from .fact_protocol import FactEvent, FactUpdate, BindingProposal, parse_plan, parse_update
from .working_memory import FactNode, FactUse, BindingLink
from .data import CanonicalDocument, CanonicalSample, build_manifest, canonicalize_record, load_records
from .direct import DirectFullContextError, run_direct_full_context
from .evaluation import ExperimentConfig, ExperimentHarness, run_experiment
from .manifest import Manifest, ManifestEntry
from .metrics import answer_exact, answer_token_f1, score_result, summarize_results
from .oracle import OraclePlanError, compile_oracle_plan, compile_oracle_plans, load_oracle_plans
from .operators import OperatorError, execute_operator
from .plan_validation import PlanIssue, PlanValidationError, ensure_valid_plan, validate_plan
from .query_graph import compile_open_query_graph
from .runtime import EvidenceRuntime, RuntimeResult, RuntimeState
from .subqueries import SubqueryRuntime, SubqueryState
from .runner import ModelBudgetExceeded, RunnerConfig, V5Runner
from .schema import (
    Action,
    AnswerResponse,
    Claim,
    EdgeFillResponse,
    EvidenceAssertion,
    EvidencePack,
    EvidenceKind,
    Modality,
    Polarity,
    QueryPlan,
    GraphQueryPlan,
    Subquery,
    SubqueryStatus,
    parse_query_plan,
    OpenQueryGraph,
    QueryEdge,
    QueryEdgeStatus,
    QueryNode,
    QueryNodeKind,
    QueryOperatorNode,
    QuestionContract,
    RelationSpec,
    RuntimeEvent,
    RuntimeStatus,
    TripleEvent,
    UpdateResponse,
    VerifyDecision,
    VerifyResult,
    VerifyStatus,
)
from .storage import SQLiteEventStore
from .archive import SentenceArchive
from .cursor_v52 import TextReadCursor as TextReadCursorV52
from .subqueries_v52 import SubqueryRuntime as SubqueryRuntimeV52
from .schema_v52 import QueryPlanV3, BindingRecord, EvidencePackV52, MemoryContext, RawEvidence

from .runtime_r2 import RuntimeR2
from .schema_r2 import StateR2, MemoryResponseR2, UpdateResponseR2

__all__ = [
    "RuntimeR2", "StateR2", "MemoryResponseR2", "UpdateResponseR2",
    "SentenceArchive", "TextReadCursorV52", "SubqueryRuntimeV52", "QueryPlanV3",
    "BindingRecord", "EvidencePackV52", "MemoryContext", "RawEvidence",
    "Action",
    "HighLevelAgent",
    "LowLevelAgent",
    "TextReadCursor",
    "TextTokenizer",
    "FactEvent",
    "FactUpdate",
    "BindingProposal",
    "FactNode",
    "FactUse",
    "BindingLink",
    "parse_plan",
    "parse_update",
    "AnswerResponse",
    "Claim",
    "EdgeFillResponse",
    "CanonicalDocument",
    "CanonicalSample",
    "EvidenceAssertion",
    "EvidenceKind",
    "EvidencePack",
    "EvidenceRuntime",
    "ExperimentConfig",
    "ExperimentHarness",
    "FutureSourceAccessError",
    "DirectFullContextError",
    "Manifest",
    "ManifestEntry",
    "Modality",
    "ModelBudgetExceeded",
    "OperatorError",
    "OraclePlanError",
    "PlanIssue",
    "PlanValidationError",
    "Polarity",
    "QueryPlan",
    "GraphQueryPlan",
    "Subquery",
    "SubqueryStatus",
    "SubqueryRuntime",
    "SubqueryState",
    "parse_query_plan",
    "OpenQueryGraph",
    "QueryEdge",
    "QueryEdgeStatus",
    "QueryNode",
    "QueryNodeKind",
    "QueryOperatorNode",
    "QuestionContract",
    "RawArchive",
    "ReadCursor",
    "ReadWindow",
    "RelationSpec",
    "RuntimeEvent",
    "RuntimeResult",
    "RuntimeState",
    "RuntimeStatus",
    "RunnerConfig",
    "SQLiteEventStore",
    "TripleEvent",
    "UpdateResponse",
    "VerifyDecision",
    "VerifyResult",
    "VerifyStatus",
    "V5Runner",
    "build_manifest",
    "compile_oracle_plan",
    "compile_oracle_plans",
    "canonicalize_record",
    "compile_open_query_graph",
    "load_records",
    "load_oracle_plans",
    "run_direct_full_context",
    "run_experiment",
    "answer_exact",
    "answer_token_f1",
    "score_result",
    "summarize_results",
    "execute_operator",
    "ensure_valid_plan",
    "validate_plan",
]
