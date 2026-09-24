# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

"""Turns a provider status check into a state change on the session, then authorizes the consumer.

Every trigger (page polling, callback, scheduler) ends up in `reconcile()`. It is the only place that
decides an attempt's outcome, and it records only what the provider's own answer says (D3).

Transactions (D4). This module commits on purpose, so call it from a page, callback or job, never from
inside a consumer's transaction:

1. claim: stamp `last_checked_on` and commit, so a concurrent trigger sees the interval and backs off.
2. record: apply the provider's answer and commit. The session is `Paid` from here on, whatever
   happens to the consumer's callback.
3. authorize: under a row lock on the session, run `on_payment_authorized`. Its effects and
   `authorization = Done` commit together. On error its effects are undone and `Failed` is recorded.

Paths that change nothing roll back instead of committing, only to release the row lock. That drops any
write the caller left pending, which is one more reason to call this module with a clean transaction.
"""

from datetime import timedelta
from typing import Protocol

import frappe
from frappe import _
from frappe.utils import get_datetime, now_datetime
from frappe.utils.user import get_users_with_role

from local_payments import lifecycle as lc
from local_payments.lifecycle import ProviderResult, Resolution

MANAGER_ROLE = "Local Payments Manager"
MIN_CHECK_INTERVAL = timedelta(seconds=10)

# Attempts still worth asking the provider about. Unresolved ones can still succeed late.
CHECKABLE_STATES = (lc.INITIATED, lc.PENDING, lc.UNRESOLVED)

AUTH_PENDING = "Pending"
AUTH_DONE = "Done"
AUTH_FAILED = "Failed"

# When the scheduler may look at something again. reconcile() owns these dates: it writes next_check_on
# and authorization_next_retry_on every time it records an outcome, so every scheduler query is a plain
# comparison on one indexed column (ARCHITECTURE, "Scheduled tasks").
#
# An Unresolved attempt is polled at a decreasing frequency: (age since its deadline, interval).
UNRESOLVED_POLL_LADDER = (
	(timedelta(hours=6), timedelta(hours=1)),
	(timedelta(hours=24), timedelta(hours=4)),
)
UNRESOLVED_POLL_FALLBACK = timedelta(hours=12)
# After this, polling stops and the daily job alerts the managers.
UNRESOLVED_ALERT_AFTER = timedelta(hours=72)

# A Pending authorization is normally still running. Give it time before the scheduler steps in.
PENDING_AUTHORIZATION_GRACE = timedelta(minutes=5)
# Each failure waits twice as long as the one before, up to the cap.
AUTHORIZATION_RETRY_BASE = timedelta(hours=1)
AUTHORIZATION_RETRY_CAP = timedelta(hours=24)
MAX_AUTHORIZATION_TRIES = 5

SAVEPOINT = "lp_authorize"


def check_interval_elapsed(last_checked_on, now=None) -> bool:
	"""True when enough time has passed to ask the provider about this attempt again."""
	if not last_checked_on:
		return True
	return (now or now_datetime()) - get_datetime(last_checked_on) >= MIN_CHECK_INTERVAL


def unresolved_poll_interval(age: timedelta) -> timedelta:
	"""How long to wait before querying an Unresolved attempt again, given how long it has been Unresolved."""
	for limit, interval in UNRESOLVED_POLL_LADDER:
		if age < limit:
			return interval
	return UNRESOLVED_POLL_FALLBACK


def authorization_retry_delay(authorization: str, tries: int) -> timedelta:
	"""How long to wait before running the consumer's callback again."""
	if authorization == AUTH_PENDING:
		return PENDING_AUTHORIZATION_GRACE
	return min(AUTHORIZATION_RETRY_BASE * 2 ** max(tries - 1, 0), AUTHORIZATION_RETRY_CAP)


def _next_attempt_check(now, row) -> object:
	"""When the scheduler should ask the provider about this attempt again. None once it is settled."""
	if lc.is_final_attempt_status(row.status):
		return None
	if row.status != lc.UNRESOLVED:
		return now + MIN_CHECK_INTERVAL
	age = now - get_datetime(row.expires_on) if row.expires_on else timedelta()
	return now + unresolved_poll_interval(age)


def _next_authorization_retry(now, session) -> object:
	"""When the scheduler may retry the authorization. None once it is Done or has given up."""
	if session.authorization not in (AUTH_PENDING, AUTH_FAILED):
		return None
	if (session.authorization_tries or 0) >= MAX_AUTHORIZATION_TRIES:
		return None
	return now + authorization_retry_delay(session.authorization, session.authorization_tries or 0)


class StatusProvider(Protocol):
	"""What reconcile needs from a provider. Settings doctypes implement `check_status`."""

	def check_status(self, attempt_id: str, provider_data: dict) -> ProviderResult: ...


def reconcile(attempt_id: str, provider: StatusProvider | None = None) -> Resolution | None:
	"""Check one attempt with its provider and apply the outcome.

	Returns what was decided, including "no change" when the provider still says Pending. Returns None
	when the provider was not asked or its answer was not applied: unknown attempt, attempt already
	settled, checked too recently, or settled by another trigger while the provider was answering.
	`provider` defaults to the Settings document of the session's gateway.
	"""
	claimed = _claim(attempt_id)
	if not claimed:
		return None
	session_name, gateway, provider_data = claimed

	provider = provider or _provider_for(gateway)
	result = provider.check_status(attempt_id, provider_data)

	recorded = _record(session_name, attempt_id, result)
	if not recorded:
		return None
	resolution, alert = recorded

	if alert:
		alert_managers(session_name, attempt_id, alert)
	if resolution.session_paid:
		authorize(session_name)
	return resolution


def authorize(session_name: str) -> bool:
	"""Run the consumer's `on_payment_authorized` for a Paid session. True if it is now Done.

	Also used to retry a Failed authorization. Does nothing if the session is not Paid or is already Done.
	"""
	session = frappe.get_doc("Local Payment", session_name, for_update=True)
	attempt = _paying_attempt(session)
	if session.status != lc.PAID or session.authorization == AUTH_DONE or not attempt:
		frappe.db.rollback()  # nothing to keep, release the row lock
		return session.authorization == AUTH_DONE

	frappe.db.savepoint(SAVEPOINT)
	try:
		redirect = _call_consumer(session, attempt)
	except Exception as exc:
		_record_authorization_failure(session, exc)
		return False

	session.authorization = AUTH_DONE
	session.authorization_error = None
	session.authorization_next_retry_on = None
	if isinstance(redirect, str):
		session.success_redirect = redirect
	session.save(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- effects and Done commit together (D4)
	return True


def _claim(attempt_id: str) -> tuple[str, str, dict] | None:
	now = now_datetime()
	session_name = frappe.db.get_value(
		"Local Payment Attempt", {"attempt_id": attempt_id, "parenttype": "Local Payment"}, "parent"
	)
	if not session_name:
		return None

	session = frappe.get_doc("Local Payment", session_name, for_update=True)
	row = _attempt_row(session, attempt_id)
	if row.status not in CHECKABLE_STATES or not check_interval_elapsed(row.last_checked_on, now):
		frappe.db.rollback()  # nothing to keep, release the row lock
		return None

	row.last_checked_on = now
	row.check_count = (row.check_count or 0) + 1
	session.save(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- release the lock before the provider call
	return session.name, session.payment_gateway, frappe.parse_json(row.provider_data or "{}")


def _record(
	session_name: str, attempt_id: str, result: ProviderResult
) -> tuple[Resolution, str | None] | None:
	now = now_datetime()
	session = frappe.get_doc("Local Payment", session_name, for_update=True)
	row = _attempt_row(session, attempt_id)
	if row.status not in CHECKABLE_STATES:
		# Another trigger settled this attempt while we were waiting for the provider.
		frappe.db.rollback()  # nothing to keep, release the row lock
		return None

	resolution = lc.resolve(
		session_status=session.status,
		session_amount=session.amount,
		session_currency=session.currency,
		attempt_status=row.status,
		result=result,
		past_deadline=bool(row.expires_on) and now >= get_datetime(row.expires_on),
	)

	row.status = resolution.attempt_status
	row.duplicate = int(resolution.duplicate)
	row.amount_mismatch = int(resolution.amount_mismatch)
	row.next_check_on = _next_attempt_check(now, row)
	if result.provider_status:
		row.provider_status = result.provider_status
	if result.status == lc.SUCCEEDED:
		row.confirmed_amount = result.amount
		row.confirmed_currency = result.currency
		row.provider_transaction_id = result.transaction_id

	if resolution.session_paid:
		session.status = lc.PAID
		session.paid_amount = result.amount
		session.paid_on = now
		session.provider_transaction_id = result.transaction_id
		session.authorization = AUTH_PENDING
		session.authorization_next_retry_on = _next_authorization_retry(now, session)

	session.save(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- Paid must be durable before the consumer (D4)

	alert = "duplicate" if resolution.duplicate else "amount_mismatch" if resolution.amount_mismatch else None
	return resolution, alert


def _paying_attempt(session):
	return next(
		(
			row
			for row in session.attempts
			if row.status == lc.SUCCEEDED and not row.duplicate and not row.amount_mismatch
		),
		None,
	)


def _attempt_row(session, attempt_id: str):
	return next(row for row in session.attempts if row.attempt_id == attempt_id)


def _provider_for(gateway: str) -> StatusProvider:
	from payments.utils import get_payment_gateway_controller

	return get_payment_gateway_controller(gateway)


def _call_consumer(session, attempt):
	reference = frappe.get_doc(session.reference_doctype, session.reference_docname)
	data = frappe.parse_json(session.request_data or "{}")
	data.update(
		payment_gateway=session.payment_gateway,
		provider_transaction_id=attempt.provider_transaction_id,
		order_id=attempt.attempt_id,
	)
	previous = frappe.flags.data
	frappe.flags.data = frappe._dict(data)
	try:
		return reference.run_method("on_payment_authorized", "Completed")
	finally:
		frappe.flags.data = previous


def _record_authorization_failure(session, exc: Exception) -> None:
	"""Undo the consumer's effects, keep the session Paid, and note the failure."""
	traceback = frappe.get_traceback()
	try:
		# Keeps the row lock taken in authorize(), so a concurrent retry can't slip in.
		frappe.db.rollback(save_point=SAVEPOINT)
	except Exception:
		# The consumer committed and dropped our savepoint. A full rollback is all that is left,
		# and the session has to be locked again.
		frappe.db.rollback()
		session = frappe.get_doc("Local Payment", session.name, for_update=True)

	session.authorization = AUTH_FAILED
	session.authorization_error = str(exc)
	session.authorization_tries = (session.authorization_tries or 0) + 1
	session.authorization_next_retry_on = _next_authorization_retry(now_datetime(), session)
	session.save(ignore_permissions=True)
	frappe.log_error(
		title=f"Local Payment authorization failed: {session.name}",
		message=traceback,
		reference_doctype="Local Payment",
		reference_name=session.name,
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- Failed must survive whatever the caller does next


def alert_managers(session_name: str, detail: str | int, reason: str) -> None:
	"""Tell every Local Payments Manager. Must never break its caller.

	`detail` is the attempt id, or the number of tries for `authorization_exhausted`. Alerts that must be
	sent only once are guarded by the caller through a flag column, not by looking at past notifications.
	"""
	subjects = {
		"duplicate": _("Duplicate payment on {0} (attempt {1}). Check whether a refund is due."),
		"amount_mismatch": _("Amount or currency mismatch on {0} (attempt {1}). The session stays open."),
		"unresolved_timeout": _("Attempt {1} on {0} is unresolved after 72 hours. Ask the provider."),
		"authorization_exhausted": _("Payment on {0} is received but its authorization failed {1} times."),
	}
	subject = subjects[reason].format(session_name, detail)
	try:
		for user in get_users_with_role(MANAGER_ROLE):
			frappe.get_doc(
				{
					"doctype": "Notification Log",
					"type": "Alert",
					"for_user": user,
					"subject": subject,
					"document_type": "Local Payment",
					"document_name": session_name,
				}
			).insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		frappe.log_error(title=f"Local Payment alert failed: {session_name}")
