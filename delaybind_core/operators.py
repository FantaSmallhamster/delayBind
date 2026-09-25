"""Small, deterministic operator registry used by V5 evidence execution."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable


class OperatorError(ValueError):
    pass


def _date_value(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value)
    for fmt in (
        "%Y-%m-%d", "%Y/%m/%d", "%Y-%m", "%Y",
        "%B %d, %Y", "%d %B %Y", "%b %d, %Y", "%d %b %Y",
    ):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.date()
        except ValueError:
            continue
    raise OperatorError(f"not a supported date/year value: {value!r}")


def _number_value(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise OperatorError(f"not a numeric value: {value!r}") from exc


def earlier(left: Any, right: Any) -> bool:
    return _date_value(left) < _date_value(right)


def later(left: Any, right: Any) -> bool:
    return _date_value(left) > _date_value(right)


def younger(left_birth: Any, right_birth: Any) -> bool:
    """Return whether the left person is younger (born later)."""
    return later(left_birth, right_birth)


def older(left_birth: Any, right_birth: Any) -> bool:
    return earlier(left_birth, right_birth)


def compare(left: Any, right: Any, comparison: str = "EQ") -> bool:
    """Compare two scalar values using a small, explicit operator vocabulary."""
    op = comparison.upper()
    if op in {"EQ", "EQUAL", "=", "=="}:
        return _hashable(left) == _hashable(right)
    if op in {"NE", "NEQ", "!=", "NOT_EQUAL"}:
        return _hashable(left) != _hashable(right)
    if op in {"LT", "<", "EARLIER", "BEFORE"}:
        try:
            return _number_value(left) < _number_value(right)
        except OperatorError:
            return earlier(left, right)
    if op in {"GT", ">", "LATER", "AFTER"}:
        try:
            return _number_value(left) > _number_value(right)
        except OperatorError:
            return later(left, right)
    if op in {"LE", "<=", "LTE"}:
        return compare(left, right, "LT") or compare(left, right, "EQ")
    if op in {"GE", ">=", "GTE"}:
        return compare(left, right, "GT") or compare(left, right, "EQ")
    raise OperatorError(f"unsupported comparison: {comparison}")


def count(values: list[Any]) -> int:
    return len({_hashable(value) for value in values})


def intersection(left: list[Any], right: list[Any]) -> list[Any]:
    right_keys = {_hashable(value) for value in right}
    result: list[Any] = []
    seen: set[Any] = set()
    for value in left:
        key = _hashable(value)
        if key in right_keys and key not in seen:
            result.append(value)
            seen.add(key)
    return result


def union(left: list[Any], right: list[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[Any] = set()
    for value in [*left, *right]:
        key = _hashable(value)
        if key not in seen:
            result.append(value)
            seen.add(key)
    return result


def _ordered_value(value: Any) -> tuple[int, Any]:
    try:
        return 0, _number_value(value)
    except OperatorError:
        pass
    try:
        return 1, _date_value(value)
    except OperatorError:
        return 2, str(value).casefold()


def argmin(values: list[Any], candidates: list[Any]) -> Any:
    if not values or len(values) != len(candidates):
        raise OperatorError("ARGMIN requires equally sized non-empty values and candidates")
    return candidates[min(range(len(values)), key=lambda index: _ordered_value(values[index]))]


def argmax(values: list[Any], candidates: list[Any]) -> Any:
    if not values or len(values) != len(candidates):
        raise OperatorError("ARGMAX requires equally sized non-empty values and candidates")
    return candidates[max(range(len(values)), key=lambda index: _ordered_value(values[index]))]


def _hashable(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((key, _hashable(item)) for key, item in value.items()))
    if isinstance(value, list):
        return tuple(_hashable(item) for item in value)
    return value


OPERATORS: dict[str, Callable[..., Any]] = {
    "EARLIER": earlier,
    "LATER": later,
    "YOUNGER": younger,
    "OLDER": older,
    "COMPARE": compare,
    "COUNT": count,
    "INTERSECTION": intersection,
    "UNION": union,
    "ARGMIN": argmin,
    "ARGMAX": argmax,
}


def execute_operator(operator_type: str, *args: Any) -> Any:
    try:
        operator = OPERATORS[operator_type.upper()]
    except KeyError as exc:
        raise OperatorError(f"unsupported operator: {operator_type}") from exc
    return operator(*args)
