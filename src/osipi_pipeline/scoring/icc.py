"""Intraclass correlation for a targets x sessions layout.

The proposal names ICC alongside RMSE, bias and CoV. The other three have one
uncontested definition each; ICC has six, and choosing among them is a
scientific decision about what the challenge is measuring, not an
implementation detail:

* **Model 1** (one-way random). Each target is rated by a *different*,
  randomly chosen set of sessions. Session effects cannot be separated from
  error, so a systematic offset between sessions is absorbed into the
  disagreement.
* **Model 2** (two-way random, absolute agreement). The same sessions apply to
  every target and are themselves a sample of possible sessions. A systematic
  offset between sessions counts against agreement, and the result generalises
  to sessions not measured.
* **Model 3** (two-way mixed, consistency). The same sessions apply to every
  target and are the only ones of interest. A systematic offset is treated as
  a fixed property of the session and does not count against agreement, so
  model 3 is never lower than model 2 on the same data.

and, cutting across those, **single-measure** (``_1``) versus
**average-measure** (``_k``): whether the reliability quoted is that of one
measurement or of the mean of all ``k``.

So this module implements all six and **chooses none**. The model comes from
``challenges.<id>.grouped_statistics.icc.model`` and the default is
:data:`MODEL_NONE`, under which nothing is computed and the pipeline goes on
reporting ICC as unavailable exactly as before. That keeps the decision with
the challenge leads while removing the code as a blocker: when they answer,
enabling it is a one-line configuration change rather than a development task.

Formulas follow Shrout & Fleiss (1979) with the McGraw & Wong (1996)
correction to the model-2 denominator, from the two-way ANOVA of an
``n`` targets x ``k`` sessions table with one observation per cell::

    MSR = k * SUM_i (row_i - grand)^2 / (n - 1)            between targets
    MSC = n * SUM_j (col_j - grand)^2 / (k - 1)            between sessions
    MSE = SUM_ij (x_ij - row_i - col_j + grand)^2 / ((n-1)(k-1))   residual
    MSW = SUM_ij (x_ij - row_i)^2 / (n * (k - 1))          within target

Conventions shared with the rest of the scoring library: values are numbers,
never formatted strings; an unavailable result carries a status and a reason
rather than a zero; and a coefficient is a ratio at rest and a percentage only
at presentation time.

Confidence intervals are exact F-based intervals at a configurable level. They
need an inverse F quantile, and the pipeline does not depend on SciPy, so one
is computed here from the regularised incomplete beta function. Set
``confidence_level`` to ``null`` to omit intervals entirely.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Sequence

#: No model chosen: ICC is not computed. Used when configuration is absent
#: or ICC has explicitly been disabled.
MODEL_NONE = "none"

MODEL_1_1 = "icc1_1"
MODEL_2_1 = "icc2_1"
MODEL_3_1 = "icc3_1"
MODEL_1_K = "icc1_k"
MODEL_2_K = "icc2_k"
MODEL_3_K = "icc3_k"

#: Every model this module can compute, in the order they are conventionally
#: presented. ``MODEL_NONE`` is deliberately not a member: it is the absence of
#: a choice, not one of the choices.
MODELS: tuple[str, ...] = (
    MODEL_1_1, MODEL_2_1, MODEL_3_1, MODEL_1_K, MODEL_2_K, MODEL_3_K,
)

#: What each model assumes, carried into exports so a number is never read
#: without the assumption behind it.
MODEL_DESCRIPTIONS: dict[str, str] = {
    MODEL_1_1: "ICC(1,1): one-way random, single measurement. Each target may "
               "be measured by a different set of sessions.",
    MODEL_2_1: "ICC(2,1): two-way random, absolute agreement, single "
               "measurement. Systematic session differences count against "
               "agreement and the result generalises to other sessions.",
    MODEL_3_1: "ICC(3,1): two-way mixed, consistency, single measurement. "
               "Systematic session differences are treated as fixed and do "
               "not count against agreement.",
    MODEL_1_K: "ICC(1,k): one-way random, reliability of the mean of k "
               "measurements.",
    MODEL_2_K: "ICC(2,k): two-way random, absolute agreement, reliability of "
               "the mean of k measurements.",
    MODEL_3_K: "ICC(3,k): two-way mixed, consistency, reliability of the mean "
               "of k measurements.",
}

STATUS_AVAILABLE = "available"
STATUS_NOT_CONFIGURED = "not_configured"
STATUS_TOO_FEW_TARGETS = "too_few_targets"
STATUS_TOO_FEW_SESSIONS = "too_few_sessions"
STATUS_INCOMPLETE_TABLE = "incomplete_table"
STATUS_NO_VARIANCE = "no_variance"

STATUS_LABELS = {
    STATUS_AVAILABLE: "Available",
    STATUS_NOT_CONFIGURED: "Not configured",
    STATUS_TOO_FEW_TARGETS: "Not enough participants with matched sessions",
    STATUS_TOO_FEW_SESSIONS: "Not enough sessions",
    STATUS_INCOMPLETE_TABLE: "Incomplete participant/session table",
    STATUS_NO_VARIANCE: "Not enough variation to calculate ICC",
    "no_groups": "Not enough compatible repeated scans",
}

REASON_NOT_CONFIGURED = (
    "No ICC model is configured for this challenge. Set "
    "grouped_statistics.icc.model in validation_rules.yaml once the challenge "
    "leads have chosen one."
)

#: ICC needs at least two targets and two sessions; with fewer there is no
#: between-target variance to partition.
MIN_TARGETS = 2
MIN_SESSIONS = 2

#: Below this, the mean squares are identical to within floating-point noise
#: and the ratio is meaningless rather than merely extreme.
_VARIANCE_TOLERANCE = 1e-12

#: An estimate this close to 1 is perfect agreement for reporting purposes.
#: Looser than the variance tolerance on purpose: the interval formulas divide
#: by (1 - ICC), which loses all precision long before that value underflows.
#: Real repeated scans of the same synthetic phantom reach this.
_PERFECT_AGREEMENT_TOLERANCE = 1e-9

DEFAULT_CONFIDENCE_LEVEL = 0.95

METHODOLOGY: dict[str, str] = {
    "source": "scan-level ROI medians, unless the challenge configures otherwise",
    "layout": "targets (participants) x sessions (repeats or sites), one "
              "observation per cell",
    "anova": "two-way ANOVA mean squares MSR, MSC, MSE, MSW",
    "reference": "Shrout & Fleiss (1979); McGraw & Wong (1996)",
    "confidence_interval": "exact F-based interval at the configured level",
    "incomplete_targets": "a target missing any session is excluded from the "
                          "table and counted, never imputed",
    "model_choice": "configuration; each selected model is reported separately",
    "status": "conventions subject to confirmation by OSIPI",
}


# ── ANOVA ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AnovaTable:
    """Mean squares of the targets x sessions layout, with its dimensions."""

    targets: int
    sessions: int
    grand_mean: float
    msr: float
    msc: float
    mse: float
    msw: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def anova_table(matrix: Sequence[Sequence[float]]) -> AnovaTable:
    """Two-way mean squares for a complete ``n x k`` table.

    ``matrix`` is one row per target, one column per session. Raises
    ``ValueError`` for a ragged or undersized table; callers that build tables
    from real submissions should use :func:`compute_icc`, which reports those
    conditions as a status instead.
    """
    rows = [[float(value) for value in row] for row in matrix]
    n = len(rows)
    if n < MIN_TARGETS:
        raise ValueError(f"ICC needs at least {MIN_TARGETS} targets, got {n}")
    k = len(rows[0])
    if k < MIN_SESSIONS:
        raise ValueError(f"ICC needs at least {MIN_SESSIONS} sessions, got {k}")
    if any(len(row) != k for row in rows):
        raise ValueError("ICC needs one observation per target and session")

    grand = sum(sum(row) for row in rows) / (n * k)
    row_means = [sum(row) / k for row in rows]
    col_means = [sum(rows[i][j] for i in range(n)) / n for j in range(k)]

    msr = k * sum((mean - grand) ** 2 for mean in row_means) / (n - 1)
    msc = n * sum((mean - grand) ** 2 for mean in col_means) / (k - 1)
    residual = sum(
        (rows[i][j] - row_means[i] - col_means[j] + grand) ** 2
        for i in range(n) for j in range(k)
    )
    mse = residual / ((n - 1) * (k - 1))
    within = sum(
        (rows[i][j] - row_means[i]) ** 2 for i in range(n) for j in range(k)
    )
    msw = within / (n * (k - 1))
    return AnovaTable(
        targets=n, sessions=k, grand_mean=grand,
        msr=msr, msc=msc, mse=mse, msw=msw,
    )


def _point_estimate(table: AnovaTable, model: str) -> float | None:
    """The ICC itself. ``None`` when the denominator vanishes."""
    n, k = table.targets, table.sessions
    msr, msc, mse, msw = table.msr, table.msc, table.mse, table.msw

    if model == MODEL_1_1:
        denominator = msr + (k - 1) * msw
        return None if abs(denominator) <= _VARIANCE_TOLERANCE else (msr - msw) / denominator
    if model == MODEL_1_K:
        return None if abs(msr) <= _VARIANCE_TOLERANCE else (msr - msw) / msr
    if model == MODEL_2_1:
        denominator = msr + (k - 1) * mse + k * (msc - mse) / n
        return None if abs(denominator) <= _VARIANCE_TOLERANCE else (msr - mse) / denominator
    if model == MODEL_2_K:
        denominator = msr + (msc - mse) / n
        return None if abs(denominator) <= _VARIANCE_TOLERANCE else (msr - mse) / denominator
    if model == MODEL_3_1:
        denominator = msr + (k - 1) * mse
        return None if abs(denominator) <= _VARIANCE_TOLERANCE else (msr - mse) / denominator
    if model == MODEL_3_K:
        return None if abs(msr) <= _VARIANCE_TOLERANCE else (msr - mse) / msr
    raise ValueError(f"unknown ICC model: {model!r}")


# ── Inverse F, without SciPy ───────────────────────────────────────────────


def _log_beta(a: float, b: float) -> float:
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta (Lentz's method)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    return h


def regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    """``I_x(a, b)``, the regularised incomplete beta function."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(
        a * math.log(x) + b * math.log(1.0 - x) - _log_beta(a, b)
    ) / a
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x)
    return 1.0 - math.exp(
        b * math.log(1.0 - x) + a * math.log(x) - _log_beta(b, a)
    ) / b * _betacf(b, a, 1.0 - x)


def f_cdf(value: float, d1: float, d2: float) -> float:
    """P(F <= value) for the F distribution with ``d1``, ``d2`` degrees of freedom."""
    if value <= 0.0:
        return 0.0
    x = (d1 * value) / (d1 * value + d2)
    return regularized_incomplete_beta(d1 / 2.0, d2 / 2.0, x)


def f_quantile(probability: float, d1: float, d2: float) -> float:
    """Inverse F CDF by bracketing and bisection.

    Bisection rather than Newton: the CDF is monotone, the brackets are cheap
    to widen, and a derivative-free method cannot be thrown off by the flat
    tails where an ICC interval endpoint often sits. A few dozen iterations of
    a halving interval reach far more precision than the input medians carry.
    """
    if not 0.0 < probability < 1.0:
        raise ValueError("probability must lie strictly between 0 and 1")
    low, high = 0.0, 1.0
    for _ in range(200):
        if f_cdf(high, d1, d2) >= probability:
            break
        low = high
        high *= 2.0
    else:  # pragma: no cover - unreachable for finite probabilities
        return high
    for _ in range(200):
        middle = (low + high) / 2.0
        if f_cdf(middle, d1, d2) < probability:
            low = middle
        else:
            high = middle
        if high - low < 1e-12 * max(1.0, high):
            break
    return (low + high) / 2.0


def _confidence_interval(
    table: AnovaTable, model: str, level: float,
) -> tuple[float | None, float | None]:
    """Exact F-based interval for ``model`` at ``level``.

    Models 1 and 3 have closed forms. Model 2's interval follows McGraw & Wong
    and is computed on the single-measure scale, then converted for the
    average-measure form, which is the same interval expressed per mean of k.
    """
    n, k = table.targets, table.sessions
    msr, msc, mse, msw = table.msr, table.msc, table.mse, table.msw
    alpha = 1.0 - level
    lower_p, upper_p = 1.0 - alpha / 2.0, alpha / 2.0

    def single_to_average(bound: float | None) -> float | None:
        """k*rho / (1 + (k-1)*rho), the Spearman-Brown step."""
        if bound is None:
            return None
        denominator = 1.0 + (k - 1) * bound
        if abs(denominator) <= _VARIANCE_TOLERANCE:
            return None
        return k * bound / denominator

    # Perfect agreement first, for every model, so all six agree about it.
    #
    # Real repeated scans of the same synthetic phantom agree to within
    # floating-point noise, driving the residual mean square to ~1e-26 and the
    # estimate to 1. The models used to diverge here: model 2 bailed out of its
    # (1 - ICC) division while model 3's closed form still produced bounds, so
    # two descriptions of the same perfect agreement disagreed about whether an
    # interval existed.
    #
    # They now agree that there is none. Pinning the interval at [1, 1] would
    # be the other way to make them consistent, and it is the wrong one: an
    # interval of zero width claims infinite precision from a handful of
    # scans. No residual variance means no interval, which is the same rule
    # this pipeline applies everywhere else, unavailable is not a value.
    estimate = _point_estimate(table, model)
    if estimate is not None and abs(1.0 - estimate) <= _PERFECT_AGREEMENT_TOLERANCE:
        return None, None

    try:
        if model in (MODEL_1_1, MODEL_1_K):
            if abs(msw) <= _VARIANCE_TOLERANCE:
                return None, None
            ratio = msr / msw
            f_lower = ratio / f_quantile(lower_p, n - 1, n * (k - 1))
            f_upper = ratio / f_quantile(upper_p, n - 1, n * (k - 1))
            low = (f_lower - 1.0) / (f_lower + (k - 1))
            high = (f_upper - 1.0) / (f_upper + (k - 1))
            if model == MODEL_1_K:
                return single_to_average(low), single_to_average(high)
            return low, high

        if model in (MODEL_3_1, MODEL_3_K):
            if abs(mse) <= _VARIANCE_TOLERANCE:
                return None, None
            ratio = msr / mse
            f_lower = ratio / f_quantile(lower_p, n - 1, (n - 1) * (k - 1))
            f_upper = ratio / f_quantile(upper_p, n - 1, (n - 1) * (k - 1))
            low = (f_lower - 1.0) / (f_lower + (k - 1))
            high = (f_upper - 1.0) / (f_upper + (k - 1))
            if model == MODEL_3_K:
                return single_to_average(low), single_to_average(high)
            return low, high

        # Model 2, absolute agreement: McGraw & Wong (1996), Table 7, ICC(A,1).
        # The interval is not symmetric and does not share the closed form the
        # other two models have, because MSC enters the denominator: the
        # degrees of freedom for the residual are themselves estimated
        # (Satterthwaite) from the point estimate.
        if abs(mse) <= _VARIANCE_TOLERANCE:
            return None, None
        point = _point_estimate(table, MODEL_2_1)
        # (1 - point) divides the Satterthwaite terms below. Perfect agreement
        # is already handled above; this guards the exact-1 case reached by a
        # different route.
        if point is None or abs(1.0 - point) <= _VARIANCE_TOLERANCE:
            return None, None

        a = k * point / (n * (1.0 - point))
        b = 1.0 + k * point * (n - 1) / (n * (1.0 - point))
        v_denominator = (
            (a * msc) ** 2 / (k - 1) + (b * mse) ** 2 / ((n - 1) * (k - 1))
        )
        if abs(v_denominator) <= _VARIANCE_TOLERANCE:
            return None, None
        v = (a * msc + b * mse) ** 2 / v_denominator

        # Two different F quantiles: the degrees of freedom swap between the
        # bounds, which is what makes the interval asymmetric.
        f_low = f_quantile(lower_p, n - 1, v)
        f_high = f_quantile(lower_p, v, n - 1)
        common = k * msc + (k * n - k - n) * mse

        low_denominator = f_low * common + n * msr
        low = (
            n * (msr - f_low * mse) / low_denominator
            if abs(low_denominator) > _VARIANCE_TOLERANCE else None
        )
        high_denominator = common + n * f_high * msr
        high = (
            n * (f_high * msr - mse) / high_denominator
            if abs(high_denominator) > _VARIANCE_TOLERANCE else None
        )
        if model == MODEL_2_K:
            return single_to_average(low), single_to_average(high)
        return low, high
    except (ValueError, OverflowError, ZeroDivisionError):
        return None, None


# ── Result ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class IccResult:
    """One ICC for one map type, ROI and grouping axis."""

    model: str
    model_description: str
    axis: str | None = None
    challenge: str | None = None
    dataset: str | None = None
    roi_id: str | None = None
    roi_label: str | None = None
    map_type: str | None = None
    units: str | None = None
    #: Identity held fixed across the whole table, e.g. {"site": "1"}.
    held_fixed: dict[str, str | None] = field(default_factory=dict)
    target_count: int = 0
    session_count: int = 0
    #: Targets dropped because they lacked an observation for some session.
    excluded_target_count: int = 0
    value: float | None = None
    confidence_level: float | None = None
    confidence_low: float | None = None
    confidence_high: float | None = None
    status: str = STATUS_AVAILABLE
    unavailable_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


CSV_COLUMNS: tuple[str, ...] = (
    "axis", "challenge", "dataset", "roi_id", "roi_label", "map_type",
    "model", "target_count", "session_count", "excluded_target_count",
    "icc", "confidence_level", "confidence_low", "confidence_high",
    "units", "status", "unavailable_reason",
)


#: Export column name -> dataclass field, where the two differ. The column is
#: `icc` because that is what a reader of the table is looking for; the field
#: is `value` because the dataclass already says what statistic it holds.
_COLUMN_FIELDS: dict[str, str] = {"icc": "value"}


def csv_row(result: IccResult) -> list[Any]:
    """One export row. Numbers stay numbers; an unavailable ICC stays blank."""
    payload = result.to_dict()
    row: list[Any] = []
    for column in CSV_COLUMNS:
        value = payload.get(_COLUMN_FIELDS.get(column, column))
        row.append("" if value is None else value)
    return row


def _unavailable(status: str, model: str, **base: Any) -> IccResult:
    return IccResult(
        model=model,
        model_description=MODEL_DESCRIPTIONS.get(model, ""),
        status=status, unavailable_reason=status, **base,
    )


def compute_icc(
    matrix: Sequence[Sequence[float]],
    *,
    model: str,
    confidence_level: float | None = DEFAULT_CONFIDENCE_LEVEL,
    **identity: Any,
) -> IccResult:
    """ICC for one complete ``targets x sessions`` table.

    Every condition that makes an ICC undefined, too few targets, too few
    sessions, a ragged table, no variance to partition, is reported as a
    status rather than raised or silently returned as zero.
    """
    if model == MODEL_NONE:
        return _unavailable(STATUS_NOT_CONFIGURED, model, **identity)
    if model not in MODELS:
        raise ValueError(
            f"unknown ICC model {model!r}; expected one of "
            f"{MODEL_NONE!r} or {', '.join(MODELS)}"
        )

    rows = [list(row) for row in matrix]
    session_count = len(rows[0]) if rows else 0
    base = dict(identity, target_count=len(rows), session_count=session_count)

    if len(rows) < MIN_TARGETS:
        return _unavailable(STATUS_TOO_FEW_TARGETS, model, **base)
    if session_count < MIN_SESSIONS:
        return _unavailable(STATUS_TOO_FEW_SESSIONS, model, **base)
    if any(len(row) != session_count for row in rows):
        return _unavailable(STATUS_INCOMPLETE_TABLE, model, **base)

    table = anova_table(rows)
    value = _point_estimate(table, model)
    if value is None:
        return _unavailable(STATUS_NO_VARIANCE, model, **base)

    low = high = None
    if confidence_level is not None:
        low, high = _confidence_interval(table, model, float(confidence_level))

    return IccResult(
        model=model,
        model_description=MODEL_DESCRIPTIONS[model],
        value=value,
        confidence_level=confidence_level,
        confidence_low=low,
        confidence_high=high,
        status=STATUS_AVAILABLE,
        **base,
    )


# ── Building tables from per-scan rows ─────────────────────────────────────


def _field(row: Any, name: str) -> Any:
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def build_table(
    rows: Iterable[Any], *, session_field: str, source: str,
) -> tuple[list[list[float]], list[str], int]:
    """A complete targets x sessions matrix from per-scan ROI rows.

    Targets are participants and sessions are ``session_field`` (``repeat`` or
    ``site``). A participant missing any session is **excluded**, not imputed:
    the ANOVA above assumes one observation per cell, and filling a gap with a
    mean would manufacture agreement the data never showed. The number dropped
    is returned so a reviewer sees the cost rather than a quietly smaller n.
    """
    by_target: dict[str, dict[str, float]] = {}
    sessions: set[str] = set()
    for row in rows:
        participant = _field(row, "participant")
        session = _field(row, session_field)
        if participant is None or session is None:
            continue
        value = _field(row, source)
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(number):
            continue
        sessions.add(str(session))
        by_target.setdefault(str(participant), {})[str(session)] = number

    ordered_sessions = sorted(sessions)
    matrix: list[list[float]] = []
    excluded = 0
    for target in sorted(by_target):
        cells = by_target[target]
        if any(session not in cells for session in ordered_sessions):
            excluded += 1
            continue
        matrix.append([cells[session] for session in ordered_sessions])
    return matrix, ordered_sessions, excluded


#: Which per-scan field plays the "session" role for each grouping axis.
AXIS_SESSION_FIELD: dict[str, str] = {
    "inter_repeat": "repeat",
    "inter_site": "site",
}


def compute_icc_for_rows(
    roi_rows: Iterable[Any],
    *,
    model: str = MODEL_NONE,
    axes: Sequence[str] = ("inter_repeat",),
    source: str = "roi_median",
    confidence_level: float | None = DEFAULT_CONFIDENCE_LEVEL,
) -> list[IccResult]:
    """ICC per axis, map type and ROI, from the rows the ROI layer produced.

    Only ``inter_repeat`` and ``inter_site`` are meaningful here: participants
    are the targets ICC measures agreement *over*, so an ``inter_participant``
    axis has no session dimension left and is skipped rather than faked.

    Returns an empty list when no model is configured, so a caller can treat
    "not configured" as "nothing to report" without inspecting statuses.
    """
    if model == MODEL_NONE:
        return []

    rows = [row for row in roi_rows if _field(row, "roi_id")]
    results: list[IccResult] = []

    for axis in axes:
        session_field = AXIS_SESSION_FIELD.get(axis)
        if session_field is None:
            continue

        # One table per (dataset, ROI, map type), with the *other* session-like
        # field held fixed so it cannot confound the axis under test.
        held_field = "site" if session_field == "repeat" else "repeat"
        groups: dict[tuple, list[Any]] = {}
        for row in rows:
            key = (
                _field(row, "dataset"), _field(row, "roi_id"),
                _field(row, "map_type"), _field(row, held_field),
            )
            groups.setdefault(key, []).append(row)

        for key, members in sorted(groups.items(), key=lambda item: str(item[0])):
            dataset, roi_id, map_type, held_value = key
            first = members[0]
            matrix, sessions, excluded = build_table(
                members, session_field=session_field, source=source,
            )
            identity = dict(
                axis=axis,
                challenge=_field(first, "challenge"),
                dataset=dataset,
                roi_id=str(roi_id),
                roi_label=str(_field(first, "roi_label") or roi_id),
                map_type=map_type,
                units=_field(first, "units"),
                held_fixed={held_field: held_value},
                excluded_target_count=excluded,
            )
            if not matrix:
                results.append(_unavailable(
                    STATUS_TOO_FEW_TARGETS, model,
                    target_count=0, session_count=len(sessions), **identity,
                ))
                continue
            results.append(compute_icc(
                matrix, model=model, confidence_level=confidence_level, **identity,
            ))
    return results
