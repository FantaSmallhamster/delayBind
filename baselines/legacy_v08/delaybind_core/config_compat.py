"""Reject removed mechanisms before configuration or model clients are built."""

from typing import Any
import warnings


def without_evidence_rescue(config: dict[str, Any]) -> dict[str, Any]:
    """Copy a legacy config, rejecting enabled rescue and warning once if disabled.

    Check both supported layouts, even when one would otherwise be ignored by
    the entry point. Only the JSON boolean false is safe to discard.
    """
    result = dict(config)
    runner = config.get("runner")
    if isinstance(runner, dict):
        result["runner"] = dict(runner)
    locations = [result]
    if isinstance(result.get("runner"), dict):
        locations.append(result["runner"])
    present = [item for item in locations if "enable_evidence_rescue" in item]
    for item in present:
        if item["enable_evidence_rescue"] is not False:
            raise ValueError(
                "enable_evidence_rescue has been removed in S2; enabled or non-boolean "
                "legacy values cannot run. Use the S1 snapshot for rescue experiments "
                "or the current stage configuration for the active implementation."
            )
    if present:
        warnings.warn(
            "enable_evidence_rescue=false is obsolete in S2 and has been discarded; "
            "remove the field from your configuration.",
            FutureWarning,
            stacklevel=2,
        )
        for item in present:
            del item["enable_evidence_rescue"]
    return result


def without_removed_runner_options(config: dict[str, Any]) -> dict[str, Any]:
    """Reject removed fallback and discard obsolete options without changing rereads."""
    result = without_evidence_rescue(config)
    locations = [result]
    if isinstance(result.get("runner"), dict):
        locations.append(result["runner"])
    fallback = [item for item in locations if "enable_raw_archive_fallback" in item]
    for item in fallback:
        if item["enable_raw_archive_fallback"] is not False:
            raise ValueError(
                "enable_raw_archive_fallback has been removed in S4; enabled or non-boolean "
                "legacy values cannot run. Use the S3 snapshot for fallback experiments "
                "or configs/staged_refactor/s4_no_raw_fallback.json for S4."
            )
    if fallback:
        warnings.warn(
            "enable_raw_archive_fallback=false is obsolete in S4 and has been discarded; "
            "remove the field from your configuration.",
            FutureWarning,
            stacklevel=2,
        )
        for item in fallback:
            del item["enable_raw_archive_fallback"]
    present = [item for item in locations if "verify_source_neighborhood" in item]
    if present:
        warnings.warn(
            "verify_source_neighborhood was removed with low-level VERIFY in S3; "
            "MEMORY_VERIFY and ANSWER source neighborhoods are unchanged.",
            FutureWarning,
            stacklevel=2,
        )
        for item in present:
            del item["verify_source_neighborhood"]
    return result
