from dataclasses import dataclass


@dataclass(frozen=True)
class MWRConfig:
    max_live_state_tokens: int = 4096
    max_prompt_tokens: int = 24000
    max_staging_tokens: int = 4096
    max_proposals: int = 64
    max_candidate_tokens: int = 4096
    max_candidates: int = 24
    max_events_per_window: int = 16
    max_lookup_tokens: int = 8192
    max_reread_tokens: int = 65536
    answer_source_tokens: int = 8192
    output_tokens: int = 2048
    neighbor_anchors: int = 1
    dormant_fraction: float = 0.3
    unrouted_fraction: float = 0.1
    retention_policy: str = "demand"
    show_dormant_demands: bool = True
    allow_unrouted: bool = True
    dependency_recall: bool = True
    include_relation_hints: bool = False

    def __post_init__(self):
        for name, value in vars(self).items():
            if name.startswith("max_") or name in {"output_tokens", "answer_source_tokens"}:
                if type(value) is not int or value <= 0:
                    raise ValueError(f"{name} must be a positive integer")
        if type(self.neighbor_anchors) is not int or not 0 <= self.neighbor_anchors <= 2:
            raise ValueError("neighbor_anchors must be 0, 1, or 2")
        if not 0 <= self.dormant_fraction <= 1 or not 0 <= self.unrouted_fraction <= 1 or self.dormant_fraction + self.unrouted_fraction > 1:
            raise ValueError("invalid retention fractions")
        if self.retention_policy not in {"demand", "current_only", "uniform", "fifo"}:
            raise ValueError("unknown retention policy")
        for name in ("show_dormant_demands", "allow_unrouted", "dependency_recall", "include_relation_hints"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be boolean")
