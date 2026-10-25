"""Business rules for refunds, as pure functions so they are trivially testable."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

REFUND_WINDOW = timedelta(days=30)
APPROVAL_THRESHOLD_CENTS = 100_00


class PolicyViolation(Exception):
    """A request the store policy forbids. The message is safe to show the model."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RefundableOrder:
    status: str
    placed_at: datetime
    total_cents: int
    refunded_cents: int

    @property
    def remaining_cents(self) -> int:
        return self.total_cents - self.refunded_cents


def check_refund(order: RefundableOrder, amount_cents: int, now: datetime) -> None:
    if order.status == "cancelled":
        raise PolicyViolation(
            "order_cancelled", "Cancelled orders are refunded automatically; no refund is needed."
        )
    if order.status == "processing":
        raise PolicyViolation(
            "not_shipped",
            "The order has not shipped yet; escalate to a human to cancel it instead.",
        )
    age = now - order.placed_at
    if age > REFUND_WINDOW:
        raise PolicyViolation(
            "refund_window_expired",
            f"The order was placed {age.days} days ago; refunds are only possible within "
            f"{REFUND_WINDOW.days} days.",
        )
    if amount_cents > order.remaining_cents:
        raise PolicyViolation(
            "exceeds_refundable_amount",
            f"At most {format_usd(order.remaining_cents)} can still be refunded on this order.",
        )


def needs_approval(amount_cents: int) -> bool:
    return amount_cents > APPROVAL_THRESHOLD_CENTS


def format_usd(cents: int) -> str:
    return f"${cents / 100:,.2f}"
