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

SAVEPOINT = "lp_authorize"


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
		_alert_managers(session_name, attempt_id, alert)
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
		frappe.db.commit()
		return session.authorization == AUTH_DONE

	frappe.db.savepoint(SAVEPOINT)
	try:
		redirect = _call_consumer(session, attempt)
	except Exception as exc:
		_record_authorization_failure(session, exc)
		return False

	session.authorization = AUTH_DONE
	session.authorization_error = None
	if isinstance(redirect, str):
		session.success_redirect = redirect
	session.save(ignore_permissions=True)
	frappe.db.commit()
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
	too_soon = row.last_checked_on and now - get_datetime(row.last_checked_on) < MIN_CHECK_INTERVAL
	if row.status not in CHECKABLE_STATES or too_soon:
		frappe.db.commit()
		return None

	row.last_checked_on = now
	row.check_count = (row.check_count or 0) + 1
	session.save(ignore_permissions=True)
	frappe.db.commit()
	return session.name, session.payment_gateway, frappe.parse_json(row.provider_data or "{}")


def _record(
	session_name: str, attempt_id: str, result: ProviderResult
) -> tuple[Resolution, str | None] | None:
	now = now_datetime()
	session = frappe.get_doc("Local Payment", session_name, for_update=True)
	row = _attempt_row(session, attempt_id)
	if row.status not in CHECKABLE_STATES:
		# Another trigger settled this attempt while we were waiting for the provider.
		frappe.db.commit()
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
	row.next_check_on = None if lc.is_final_attempt_status(row.status) else now + MIN_CHECK_INTERVAL
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

	session.save(ignore_permissions=True)
	frappe.db.commit()

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
	session.save(ignore_permissions=True)
	frappe.log_error(
		title=f"Local Payment authorization failed: {session.name}",
		message=traceback,
		reference_doctype="Local Payment",
		reference_name=session.name,
	)
	frappe.db.commit()


def _alert_managers(session_name: str, attempt_id: str, reason: str) -> None:
	"""Tell every Local Payments Manager. Must never break reconciliation."""
	subjects = {
		"duplicate": _("Duplicate payment on {0} (attempt {1}). Check whether a refund is due."),
		"amount_mismatch": _("Amount or currency mismatch on {0} (attempt {1}). The session stays open."),
	}
	try:
		for user in get_users_with_role(MANAGER_ROLE):
			frappe.get_doc(
				{
					"doctype": "Notification Log",
					"type": "Alert",
					"for_user": user,
					"subject": subjects[reason].format(session_name, attempt_id),
					"document_type": "Local Payment",
					"document_name": session_name,
				}
			).insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		frappe.log_error(title=f"Local Payment alert failed: {session_name}")
