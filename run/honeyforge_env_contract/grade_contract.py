"""The single grade-result contract shared by the shim and the QC harness.

A grader writes a ``grade.json`` dict.  The hosted platform turns that dict into
the reward scalar a model trains against through its ``Grade`` type — a
*weighted, total-weight-normalised* sum of the subscores, plus penalties,
clamped to ``[0, 1]``.

The reward scalar used to be re-derived independently in several places
(``shim.grade_problem`` built a ``Grade``; ``_fresh_grade._read_score``
and ``_runner._extract_score`` did an *unweighted* mean that dropped weights and
penalties).  They agree only as long as every grader emits a precomputed
top-level ``score`` — the moment one emits ``subscores`` with non-uniform
``weights`` or a separate ``penalties`` key, the QC verdict is computed against
a number the platform never pays out.

This module is that single source of truth:

* :func:`normalize` turns the open grader dict into a :class:`CanonicalGrade`,
  raising :class:`GradeContractError` on every ambiguous or out-of-range shape
  rather than silently coercing (no datatype with a representable-invalid
  configuration reaches a consumer).
* :func:`reward_scalar` reduces a :class:`CanonicalGrade` to the reward exactly
  as the platform's ``Grade.score`` would — ``test_grade_contract`` pins the
  two together against the real ``Grade``.

Every host-side grading lane (QC, the Inspect runtime, the public export's
runner) imports this module, so the reward an environment is certified against
is the reward it will train against.

Contract (matching ``shim.grade_problem``'s historical behaviour):

* A top-level ``"score"`` is authoritative.  Any ``"subscores"``/``"weights"``
  alongside it are informational metadata and do NOT enter the reward — this
  mirrors the shim, which collapses a ``score`` dict to a single
  ``{"score": x}`` subscore.
* Otherwise ``"subscores"`` (non-empty, numeric, in ``[0, 1]``) is reduced with
  ``"weights"`` if present (keys must match) or uniform weights if absent.
* ``"weights"`` must be non-negative — deliberately STRICTER than the vendored
  ``Grade``, which only asserts the *final* no-penalty score lands in
  ``[0, 1]``: a negative weight whose weighted average happens to stay in
  range would grade on the platform but raises here.  A negative weight is a grader
  bug worth surfacing at this boundary (the same grader crashes the real
  ``Grade`` the moment its reduction leaves the range), and ruling it out is
  what keeps the no-penalties reduction in ``[0, 1]`` without a clamp.  Every
  consumer inherits this strictness.
* ``"penalties"`` (optional, values in ``[-1, 0]``) are added after the weighted
  sum and the result is clamped to ``[0, 1]``.
* Anything else — neither ``score`` nor ``subscores``, empty subscores, bool or
  non-numeric values, out-of-range values, mismatched weight keys — raises.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import cast

__all__ = [
    "GRADE_ERROR_KEY",
    "CanonicalGrade",
    "GradeContractError",
    "GradeReading",
    "normalize",
    "read_grade",
    "reward_scalar",
]

# The key the canonical grade entry (``run_grader.py``) adds
# when the shipped grader raised instead of returning a grade. It rides inside
# the grade dict rather than replacing it, so the submission still gets the floor
# the platform pays -- :func:`normalize` ignores unknown keys, so the reward is exactly
# the 0.0 written. Every QC path reads the marker back and refuses to treat that
# floor as a score the grader chose. The entry duplicates this literal because it
# is stdlib-only in-container; ``test_run_grader`` pins the two together.
GRADE_ERROR_KEY = "grade_error"


class GradeContractError(ValueError):
    """A grader's ``grade.json`` dict does not satisfy the grade contract."""


@dataclass(frozen=True)
class CanonicalGrade:
    """A validated grade in the one shape the reward reducer accepts.

    ``subscores`` and ``weights`` always share the same non-empty key set with
    numeric values; ``weights`` sum to a positive total.  ``penalties`` is
    ``None`` or a dict of values in ``[-1, 0]``.  Constructing one of these is
    only possible through :func:`normalize`, so a ``CanonicalGrade`` cannot hold
    an invalid configuration.
    """

    subscores: dict[str, float]
    weights: dict[str, float]
    penalties: dict[str, float] | None


def _as_finite_float(value: object, path: str) -> float:
    # bool is an int subclass; a boolean score/weight/penalty is a bug, not a
    # 0.0/1.0 the grader meant — reject rather than silently coerce.
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise GradeContractError(f"{path} must be a number, got {value!r}")
    result = float(value)
    if not math.isfinite(result):
        raise GradeContractError(f"{path} must be finite, got {value!r}")
    return result


def _unit_interval(value: float, path: str) -> float:
    if not (0.0 <= value <= 1.0):
        raise GradeContractError(f"{path} must be in [0, 1], got {value!r}")
    return value


def _require_object(value: object, path: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise GradeContractError(f"{path} must be an object, got {value!r}")
    return cast("dict[str, object]", value)


def _normalize_penalties(grade: dict[str, object]) -> dict[str, float] | None:
    if "penalties" not in grade:
        return None
    raw = _require_object(grade["penalties"], "penalties")
    penalties: dict[str, float] = {}
    for key, val in raw.items():
        path = f"penalties[{key!r}]"
        amount = _as_finite_float(val, path)
        if not (-1.0 <= amount <= 0.0):
            raise GradeContractError(f"{path} must be in [-1, 0], got {val!r}")
        penalties[str(key)] = amount
    return penalties


def _normalize_subscores(raw: dict[str, object]) -> dict[str, float]:
    if not raw:
        raise GradeContractError("subscores must be a non-empty object")
    subscores: dict[str, float] = {}
    for key, val in raw.items():
        path = f"subscores[{key!r}]"
        subscores[str(key)] = _unit_interval(_as_finite_float(val, path), path)
    return subscores


def _normalize_weights(grade: dict[str, object], subscores: dict[str, float]) -> dict[str, float]:
    if "weights" not in grade:
        return dict.fromkeys(subscores, 1.0 / len(subscores))
    raw = _require_object(grade["weights"], "weights")
    weights = {str(key): _as_finite_float(val, f"weights[{key!r}]") for key, val in raw.items()}
    if weights.keys() != subscores.keys():
        raise GradeContractError(
            f"weights keys {sorted(weights)} do not match subscores keys {sorted(subscores)}"
        )
    # Every subscore is already validated into [0, 1] (_normalize_subscores), so
    # a weighted average with every weight non-negative can never itself leave
    # [0, 1] -- this is what keeps reward_scalar's no-penalties branch in
    # contract without needing to clamp it. The platform's Grade.score has no
    # equivalent guard: it *asserts* the unclamped score lands in [0, 1] and
    # raises otherwise, so a grader that emits a negative weight would crash
    # deep in the platform's real grading rather than fail here at the boundary. A
    # weight is a relative-importance value; no legitimate grader in this repo
    # emits a negative one (verified by search), so rejecting here converts a
    # would-be platform-side crash into a loud, immediate GradeContractError.
    negative = {key: val for key, val in weights.items() if val < 0.0}
    if negative:
        raise GradeContractError(f"weights must be non-negative, got {negative!r}")
    total = sum(weights.values())
    if total <= 0.0:
        raise GradeContractError(f"weights must sum to a positive total, got {total!r}")
    return weights


def normalize(grade: object) -> CanonicalGrade:
    """Validate and canonicalise a grader's ``grade.json`` dict.

    Raises :class:`GradeContractError` on any shape that is ambiguous or that
    the platform's ``Grade`` would reject, so the failure surfaces at the
    grade boundary instead of as a wrong reward downstream.
    """
    grade = _require_object(grade, "grade")
    penalties = _normalize_penalties(grade)

    if "score" in grade:
        # Top-level score is authoritative; subscores/weights alongside it are
        # informational only (matches shim.grade_problem).
        score = _unit_interval(_as_finite_float(grade["score"], "score"), "score")
        return CanonicalGrade(
            subscores={"score": score}, weights={"score": 1.0}, penalties=penalties
        )

    if "subscores" not in grade:
        raise GradeContractError("grade must contain either 'score' or 'subscores'")

    subscores = _normalize_subscores(_require_object(grade["subscores"], "subscores"))
    weights = _normalize_weights(grade, subscores)
    return CanonicalGrade(subscores=subscores, weights=weights, penalties=penalties)


def reward_scalar(grade: CanonicalGrade) -> float:
    """Reduce a :class:`CanonicalGrade` to the reward scalar the platform pays out.

    Identical to the platform's ``Grade.score``: weighted sum normalised by the
    total weight, plus penalties, clamped to ``[0, 1]`` when penalties apply.
    Kept stdlib-only (this package has no dependencies) and pinned to the real
    ``Grade`` by ``test_grade_contract``'s parity corpus.
    """
    total_weight = sum(grade.weights.values())
    score = sum(grade.subscores[key] * grade.weights[key] for key in grade.subscores) / total_weight
    if grade.penalties is not None:
        score += sum(grade.penalties.values())
        score = max(0.0, min(1.0, score))
    return score


def reward_from_grade_dict(grade: object) -> float:
    """Convenience: :func:`normalize` then :func:`reward_scalar`."""
    return reward_scalar(normalize(grade))


@dataclass(frozen=True)
class GradeReading:
    """What one grader run said, as every QC path reads it.

    ``score`` is None iff the grader did not produce a grade QC can pay: its
    output was not a grade dict, the dict failed the contract, or it carries
    ``GRADE_ERROR_KEY`` -- the grade entry floored a raise so production would
    pay zero rather than discard the rollout, and that floor is the entry's
    number, not the grader's.  ``grade_dict`` keeps whatever parsed, for
    diagnostics, whether or not it was payable.
    """

    score: float | None
    grade_dict: dict[str, object] | None


def read_grade(parsed: dict[str, object] | None) -> GradeReading:
    """Reduce a parsed grade dict to the reward, or to no reward at all.

    The one reading every lane takes of the grade file a grade entry recorded
    (``grade_invocation.GRADE_FILE``), so every lane's grade of one submission
    is the same number or the same ``None``.
    """
    if parsed is None or GRADE_ERROR_KEY in parsed:
        return GradeReading(score=None, grade_dict=parsed)
    try:
        return GradeReading(score=reward_from_grade_dict(parsed), grade_dict=parsed)
    except GradeContractError:
        return GradeReading(score=None, grade_dict=parsed)
