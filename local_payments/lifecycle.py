# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

"""Pure state machine for Local Payment sessions and attempts.

Standard library only: no frappe import (scripts/check_pure_core.py enforces it in CI), no database
access, no clock. See docs/ARCHITECTURE.md, "Lifecycle".
"""

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

# Attempt states
INITIATED = "Initiated"
PENDING = "Pending"
SUCCEEDED = "Succeeded"
FAILED = "Failed"
EXPIRED = "Expired"
UNRESOLVED = "Unresolved"
ERROR = "Error"

# Session states
OPEN = "Open"
PAID = "Paid"
VOID = "Void"

ATTEMPT_TRANSITIONS: dict[str, frozenset[str]] = {
	INITIATED: frozenset({PENDING, SUCCEEDED, FAILED, EXPIRED, UNRESOLVED}),
	PENDING: frozenset({SUCCEEDED, FAILED, EXPIRED, UNRESOLVED}),
	UNRESOLVED: frozenset({SUCCEEDED, FAILED, EXPIRED}),
	SUCCEEDED: frozenset(),
	FAILED: frozenset(),
	EXPIRED: frozenset(),
	ERROR: frozenset(),
}

SESSION_TRANSITIONS: dict[str, frozenset[str]] = {
	OPEN: frozenset({PAID, VOID}),
	PAID: frozenset(),
	VOID: frozenset(),
}

# These block a new attempt. Unresolved does not: the payer can retry while it is still being polled.
ATTEMPT_IN_FLIGHT_STATES = frozenset({INITIATED, PENDING})

# What a provider check can report. Initiated, Unresolved and Error are decided locally.
PROVIDER_STATUSES = frozenset({PENDING, SUCCEEDED, FAILED, EXPIRED})


class LifecycleError(ValueError):
	"""Base class for every refusal raised by this module."""


class IllegalTransition(LifecycleError):
	def __init__(self, kind: str, current: str | None, new: str):
		self.kind = kind
		self.current = current
		self.new = new
		super().__init__(f"Illegal {kind} transition: {current or '(none)'} -> {new}")


class AttemptInProgress(LifecycleError):
	"""The session already has an Initiated or Pending attempt."""


@dataclass(frozen=True)
class ProviderResult:
	"""What a provider status check returns, normalised by providers/."""

	status: str
	amount: Decimal | int | float | str | None = None
	currency: str | None = None
	transaction_id: str | None = None
	# The provider's own words, e.g. "FAILED: NOT_ENOUGH_FUNDS", kept for support.
	provider_status: str | None = None

	def __post_init__(self):
		if self.status not in PROVIDER_STATUSES:
			raise LifecycleError(
				f"Unknown provider status {self.status!r}, expected one of {sorted(PROVIDER_STATUSES)}"
			)


@dataclass(frozen=True)
class Resolution:
	"""Outcome of applying a ProviderResult to a session and one of its attempts."""

	attempt_status: str
	session_status: str
	duplicate: bool = False
	amount_mismatch: bool = False

	@property
	def session_paid(self) -> bool:
		"""True only when this result is the one that moves the session to Paid."""
		return self.session_status == PAID and not self.duplicate


def _check(kind: str, table: dict[str, frozenset[str]], current: str, new: str) -> None:
	if new not in table.get(current, frozenset()):
		raise IllegalTransition(kind, current, new)


def check_attempt_transition(current: str, new: str) -> None:
	"""Raise IllegalTransition unless an attempt may go from `current` to `new`."""
	_check("attempt", ATTEMPT_TRANSITIONS, current, new)


def check_session_transition(current: str, new: str) -> None:
	"""Raise IllegalTransition unless a session may go from `current` to `new`."""
	_check("session", SESSION_TRANSITIONS, current, new)


def is_final_attempt_status(status: str) -> bool:
	return status in ATTEMPT_TRANSITIONS and not ATTEMPT_TRANSITIONS[status]


def ensure_can_start_attempt(attempt_statuses: Iterable[str]) -> None:
	"""Raise AttemptInProgress if any attempt is still Initiated or Pending."""
	if any(status in ATTEMPT_IN_FLIGHT_STATES for status in attempt_statuses):
		raise AttemptInProgress("This session already has an attempt in progress (Initiated or Pending).")


def amounts_match(session_amount, confirmed_amount) -> bool:
	"""Exact comparison, never rounded. A missing confirmed amount never matches."""
	if confirmed_amount is None:
		return False
	try:
		return Decimal(str(session_amount)) == Decimal(str(confirmed_amount))
	except (InvalidOperation, ValueError):
		return False


def currencies_match(session_currency: str | None, confirmed_currency: str | None) -> bool:
	if not session_currency or not confirmed_currency:
		return False
	return session_currency.strip().upper() == confirmed_currency.strip().upper()


def resolve(
	*,
	session_status: str,
	session_amount,
	session_currency: str | None,
	attempt_status: str,
	result: ProviderResult,
	past_deadline: bool = False,
) -> Resolution:
	"""Decide what a provider result does to an attempt and its session.

	`past_deadline` says the attempt's local deadline has passed. A Pending answer then moves an
	Initiated or Pending attempt to Unresolved. Raises IllegalTransition if the attempt cannot move to
	the reported status. A Pending answer that changes nothing (still Pending, or still Unresolved) is
	a no-op.
	"""
	if result.status == PENDING:
		if attempt_status in (INITIATED, PENDING) and past_deadline:
			return Resolution(attempt_status=UNRESOLVED, session_status=session_status)
		if attempt_status in (PENDING, UNRESOLVED):
			return Resolution(attempt_status=attempt_status, session_status=session_status)

	check_attempt_transition(attempt_status, result.status)

	if result.status != SUCCEEDED:
		return Resolution(attempt_status=result.status, session_status=session_status)

	if session_status == PAID:
		return Resolution(attempt_status=SUCCEEDED, session_status=PAID, duplicate=True)

	if session_status != OPEN:
		# Void can't become Paid. The provider did confirm the payment, so the attempt says Succeeded.
		return Resolution(attempt_status=SUCCEEDED, session_status=session_status)

	if not (
		amounts_match(session_amount, result.amount) and currencies_match(session_currency, result.currency)
	):
		return Resolution(attempt_status=SUCCEEDED, session_status=OPEN, amount_mismatch=True)

	check_session_transition(OPEN, PAID)
	return Resolution(attempt_status=SUCCEEDED, session_status=PAID)
