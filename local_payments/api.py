# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

"""Endpoints the payer's browser reaches, and the read model the checkout page shares with them.

A payer holds a token and nothing else: every lookup starts from it, and the session's sequential
name never leaves this module. `Local Payment` is readable by managers only, so these reads go
through `frappe.db`, which checks no permission.
"""

import re
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.model.base_document import get_controller
from frappe.rate_limiter import rate_limit
from frappe.utils import get_url

from local_payments import lifecycle as lc
from local_payments import reconcile as rc

# The session's `before_insert` fills the token with frappe.generate_hash(length=32), which is hex.
TOKEN_PATTERN = re.compile(r"\A[0-9a-f]{32}\Z")

SESSION_FIELDS = (
	"name",
	"status",
	"title",
	"amount",
	"currency",
	"payment_gateway",
	"success_redirect",
	"redirect_to",
	"reference_doctype",
	"reference_docname",
)

ATTEMPT_FIELDS = ("attempt_id", "status", "last_checked_on")

# Served by frappe/payments, which reads the reference document from the query string.
DEFAULT_SUCCESS_PAGE = "/payment-success"

# A consumer's exit URL is a path on this site or an absolute http(s) address. Anything else, a
# `javascript:` link above all, is dropped in favour of the default page.
SAFE_URL = re.compile(r"\A(/|https?://)")

# The page polls every 5 seconds, so one payer needs about 12 calls a minute. The per-address budget
# leaves room for several payers behind one connection.
POLLS_PER_TOKEN = 30
POLLS_PER_ADDRESS = 120


def find_session(token) -> frappe._dict | None:
	"""The session this token opens, or None if the token is unknown or the wrong shape."""
	if not isinstance(token, str) or not TOKEN_PATTERN.match(token):
		return None
	return frappe.db.get_value("Local Payment", {"token": token}, SESSION_FIELDS, as_dict=True)


def current_attempt(session_name: str) -> frappe._dict | None:
	"""The session's latest attempt, whatever its status. None until the payer starts one."""
	rows = frappe.get_all(
		"Local Payment Attempt",
		filters={"parent": session_name, "parenttype": "Local Payment"},
		fields=ATTEMPT_FIELDS,
		order_by="idx desc",
		limit=1,
	)
	return rows[0] if rows else None


def provider_name(gateway: str) -> str:
	"""The provider's display name, read off the controller class of the gateway's settings doctype.

	Reading the class leaves the settings document, and its passwords, unopened.
	"""
	settings_doctype = frappe.db.get_value("Payment Gateway", gateway, "gateway_settings")
	if not settings_doctype:
		return gateway
	return getattr(get_controller(settings_doctype), "provider_name", None) or gateway


def exit_url(session) -> str:
	"""Where a paid session sends the payer: what the consumer asked for, or the payments app's page."""
	for asked in (session.success_redirect, session.redirect_to):
		if asked and SAFE_URL.match(asked.strip()):
			return asked.strip()
	query = urlencode({"doctype": session.reference_doctype, "docname": session.reference_docname})
	return get_url(f"{DEFAULT_SUCCESS_PAGE}?{query}")


def payer_status(session, attempt) -> dict:
	"""What the payer is allowed to know about a session."""
	return {
		"status": session.status,
		"attempt_status": attempt.status if attempt else None,
		"redirect_url": exit_url(session) if session.status == lc.PAID else None,
	}


# The payer has no account, so this is guest by design: it only answers for a valid token.
# One rate limit bucket per token, so one payer's tabs cannot spend another payer's budget, and one per
# address, which is what bounds a caller trying a new token on every request.
@frappe.whitelist(allow_guest=True, methods=["GET"])  # nosemgrep: guest-whitelisted-method
@rate_limit(key="token", limit=POLLS_PER_TOKEN, seconds=60)
@rate_limit(limit=POLLS_PER_ADDRESS, seconds=60)
def get_status(token: str) -> dict:
	"""Where this payment stands, and where to send the payer once it is paid.

	Asks the provider through `reconcile()` when an attempt is still running and the minimum interval
	between two checks has passed, so however many tabs poll, one call goes out per interval.
	"""
	session = _session_or_404(token)
	attempt = current_attempt(session.name)

	if _worth_checking(attempt):
		_check_with_provider(attempt.attempt_id, session.name)
		# The check commits, so read both rows again.
		session = _session_or_404(token)
		attempt = current_attempt(session.name)

	return payer_status(session, attempt)


def _check_with_provider(attempt_id: str, session_name: str) -> None:
	"""Reconcile the attempt, but never fail the poll over it: the payer gets the last known state."""
	try:
		rc.reconcile(attempt_id)
	except Exception:
		frappe.db.rollback()
		# Frappe rolls a GET request back on its way out, so the log has to be inserted out of band.
		frappe.log_error(
			title="Local Payment status check failed",
			reference_doctype="Local Payment",
			reference_name=session_name,
			defer_insert=True,
		)


def _session_or_404(token) -> frappe._dict:
	session = find_session(token)
	if not session:
		# A malformed token and an unknown one get the same answer.
		frappe.throw(_("This payment page is no longer available."), frappe.DoesNotExistError)
	return session


def _worth_checking(attempt) -> bool:
	return bool(
		attempt
		and attempt.status in rc.CHECKABLE_STATES
		and rc.check_interval_elapsed(attempt.last_checked_on)
	)
