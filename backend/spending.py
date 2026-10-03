"""Spending: budget reservations, the one authority for what model calls cost.

Every model call is admitted by `reserve`, inside the transaction that admits it:
the estimate is written as an open reservation against the paying conversation's
budget and the project's, and the call may go out only if, for each budget,
settled spend plus open reservations plus the new estimate fits. `settle` then
replaces the reservation by the call's reported cost, or by its estimate (marked
estimated) when no usage arrived; `release` gives back a reservation whose call
was never dispatched. Each happens at most once, by the reservation's id: a
trigger refuses any change to a row that is no longer open, and the functions
here only touch open rows, so a retry, a duplicate or a late usage report never
charges twice.

Rows outlive their run and conversation (see migration 0001 and the deletion
service): money spent stays in the project's total until the project is deleted.
A settlement for a row that no longer exists changes nothing and creates nothing.

Every function takes an open connection inside a write (or read) transaction.
"""

import math

from backend.db import new_id, utc_now

_DECIMALS = 9  # amounts are kept to a billionth of a dollar


class BudgetExceeded(Exception):
    """A reservation did not fit a budget. Nothing was written."""

    def __init__(self, budget: str, limit_usd: float, spent_usd: float, estimate_usd: float):
        super().__init__(f"the {budget} budget of ${limit_usd:.2f} has ${max(limit_usd - spent_usd, 0):.4f} left,"
                         f" and this step is estimated at ${estimate_usd:.4f}")
        self.budget = budget
        self.limit_usd = limit_usd
        self.spent_usd = spent_usd
        self.estimate_usd = estimate_usd


def amount(value) -> float | None:
    """A cost as a non-negative finite float rounded to the kept precision, or None
    for anything else (missing, negative, NaN, infinite, not a number)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return round(float(value), _DECIMALS)


def spent(conn, *, project_id: str | None = None, conversation_id: str | None = None) -> float:
    """Settled spend plus open reservations, for a project or a paying conversation."""
    column, value = ("project_id", project_id) if conversation_id is None else ("paying_conversation_id", conversation_id)
    (total,) = conn.execute(
        "SELECT coalesce(sum(CASE status WHEN 'settled' THEN settled_usd WHEN 'open' THEN estimate_usd ELSE 0 END), 0)"
        f" FROM budget_reservations WHERE {column} = ?",
        (value,),
    ).fetchone()
    return round(total, _DECIMALS)


def reserve(conn, *, run_id: str, step_seq: int, project_id: str, paying_conversation_id: str | None,
            estimate_usd: float, project_budget_usd: float | None, conversation_budget_usd: float | None) -> str:
    """Write an open reservation for one model call and return its id.

    A budget of None is not checked; background runs pay no conversation. Raises
    BudgetExceeded, writing nothing, when the estimate does not fit a budget, and
    ValueError for an estimate that is not a non-negative number.
    """
    estimate = amount(estimate_usd)
    if estimate is None:
        raise ValueError("a reservation needs a non-negative estimate")
    checks = [("project", project_budget_usd, {"project_id": project_id})]
    if paying_conversation_id is not None:
        checks.append(("conversation", conversation_budget_usd, {"conversation_id": paying_conversation_id}))
    for budget, limit, scope in checks:
        if limit is None:
            continue
        already = spent(conn, **scope)
        if already + estimate > limit + 10 ** -_DECIMALS:
            raise BudgetExceeded(budget, limit, already, estimate)
    reservation_id = new_id()
    conn.execute(
        "INSERT INTO budget_reservations (id, run_id, step_seq, paying_conversation_id, project_id, estimate_usd)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (reservation_id, run_id, step_seq, paying_conversation_id, project_id, estimate),
    )
    return reservation_id


def settle(conn, reservation_id: str, reported_usd=None, estimated_usd=None) -> bool:
    """Settle an open reservation: at the reported cost when it is a valid amount;
    otherwise, marked estimated, at estimated_usd (an estimate from reported token
    counts) when valid, else at the reservation's own estimate. Returns whether this
    call settled it; a reservation already settled or released, or gone with its
    project, is left as it is."""
    reported, estimated = amount(reported_usd), amount(estimated_usd)
    cursor = conn.execute(
        "UPDATE budget_reservations SET status = 'settled', settled_usd = coalesce(?, ?, estimate_usd),"
        " basis = ?, settled_at = ? WHERE id = ? AND status = 'open'",
        (reported, estimated, "reported" if reported is not None else "estimated", utc_now(), reservation_id),
    )
    return cursor.rowcount == 1


def release(conn, reservation_id: str) -> bool:
    """Release an open reservation whose call was never dispatched. Returns whether it did."""
    cursor = conn.execute(
        "UPDATE budget_reservations SET status = 'released', settled_at = ? WHERE id = ? AND status = 'open'",
        (utc_now(), reservation_id),
    )
    return cursor.rowcount == 1


def run_cost(conn, run_id: str) -> float:
    """The settled cost of a run's reservations, counting open ones at their estimate."""
    (total,) = conn.execute(
        "SELECT coalesce(sum(CASE status WHEN 'settled' THEN settled_usd WHEN 'open' THEN estimate_usd ELSE 0 END), 0)"
        " FROM budget_reservations WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    return round(total, _DECIMALS)


def settle_left_open(conn) -> int:
    """After a crash: settle every reservation still open at its estimate, marked
    estimated, since its call may have gone out. Returns how many it settled.
    Called at startup, before any run of this process reserves."""
    cursor = conn.execute(
        "UPDATE budget_reservations SET status = 'settled', settled_usd = estimate_usd, basis = 'estimated',"
        " settled_at = ? WHERE status = 'open'",
        (utc_now(),),
    )
    return cursor.rowcount
