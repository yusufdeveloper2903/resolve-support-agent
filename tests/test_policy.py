from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from resolve.policy import PolicyViolation, RefundableOrder, check_refund, needs_approval

NOW = datetime(2026, 10, 25, tzinfo=UTC)


def order(
    status: str = "delivered", age_days: int = 5, total: int = 10_000, refunded: int = 0
) -> RefundableOrder:
    return RefundableOrder(status, NOW - timedelta(days=age_days), total, refunded)


def test_refund_within_policy_passes() -> None:
    check_refund(order(), 10_000, NOW)


@pytest.mark.parametrize(
    ("candidate", "amount", "code"),
    [
        (order(age_days=31), 100, "refund_window_expired"),
        (order(refunded=9_000), 1_001, "exceeds_refundable_amount"),
        (order(status="cancelled"), 100, "order_cancelled"),
        (order(status="processing"), 100, "not_shipped"),
    ],
)
def test_refund_violations(candidate: RefundableOrder, amount: int, code: str) -> None:
    with pytest.raises(PolicyViolation) as exc:
        check_refund(candidate, amount, NOW)
    assert exc.value.code == code


def test_window_boundary_is_inclusive() -> None:
    check_refund(order(age_days=30), 100, NOW)


@pytest.mark.parametrize(("cents", "expected"), [(10_000, False), (10_001, True)])
def test_approval_threshold(cents: int, expected: bool) -> None:
    assert needs_approval(cents) is expected
