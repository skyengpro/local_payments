# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

"""Guest endpoints: the payer's browser (`start_attempt`, `get_status`) and MTN's callback.

A payer holds a token and nothing else: every lookup starts from it, and the session's sequential
name never leaves this module. `Local Payment` is readable by managers only, so these reads go
through `frappe.db`, which checks no permission.
"""

import re
import uuid
from datetime import timedelta
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.model.base_document import get_controller
from frappe.rate_limiter import rate_limit
from frappe.utils import cint, get_url, now_datetime
from payments.utils import get_payment_gateway_controller

from local_payments import lifecycle as lc
from local_payments import reconcile as rc
from local_payments.gateway import Initiated

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

ATTEMPT_FIELDS = ("attempt_id", "status", "last_checked_on", "check_count", "next_check_on")

# Served by frappe/payments, which reads the reference document from the query string.
DEFAULT_SUCCESS_PAGE = "/payment-success"

# A consumer's exit URL is a path on this site or an absolute http(s) address. Anything else, a
# `javascript:` link above all, is dropped in favour of the default page.
SAFE_URL = re.compile(r"\A(/|https?://)")

# The page polls every 5 seconds, so one payer needs about 12 calls a minute. The per-address budget
# leaves room for several payers behind one connection.
POLLS_PER_TOKEN = 30
POLLS_PER_ADDRESS = 120

# Each start can push a request to the payer's phone. Counted per token whatever the address, so a
# link shared around cannot flood one phone, and per address across tokens. The count includes
# mistyped numbers, hence room for a few typos per token.
STARTS_WINDOW = 600
STARTS_PER_TOKEN = 10
STARTS_PER_ADDRESS = 30

# MTN sends one callback per attempt, from a few addresses for every attempt of the site, so the
# per-address budget is wide.
CALLBACKS_PER_ADDRESS = 600

# attempt_id is a UUID v4, sent to MTN as X-Reference-Id.
ATTEMPT_ID_PATTERN = re.compile(r"\A[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")


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


# Guest for the same reason as get_status. Opening the page never starts a payment; this POST does (D2).
@frappe.whitelist(allow_guest=True, methods=["POST"])  # nosemgrep: guest-whitelisted-method
@rate_limit(key="token", limit=STARTS_PER_TOKEN, seconds=STARTS_WINDOW, ip_based=False)
@rate_limit(limit=STARTS_PER_ADDRESS, seconds=STARTS_WINDOW)
def start_attempt(token: str, msisdn: str) -> dict:
	"""Record a new attempt for this number, then send the payment request to the provider.

	The number is checked before anything is written or sent. Returns what `get_status` returns.
	"""
	session = _session_or_404(token)
	# Checked again under the row lock. Here it spares a closed session the gateway lookup.
	_ensure_open(session.status)
	gateway = get_payment_gateway_controller(session.payment_gateway)
	if not cint(gateway.enabled):
		frappe.throw(_("This payment method is not available at the moment."))
	msisdn = gateway.payer_msisdn(msisdn)

	attempt_id = _open_attempt(session.name, msisdn, cint(gateway.pending_timeout_minutes))
	try:
		started = gateway.initiate(attempt_id, session, msisdn)
	except Exception:
		# The request may have left, so the outcome is unknown, as after a timeout: keep the attempt
		# Initiated and let status checks start now.
		frappe.db.rollback()
		frappe.log_error(
			title="Local Payment initiation failed",
			# Without context: the frames' variables hold the payer's number.
			message=frappe.get_traceback(),
			reference_doctype="Local Payment",
			reference_name=session.name,
		)
		started = Initiated(lc.INITIATED)
	_record_start(session.name, attempt_id, started)

	session = _session_or_404(token)
	return payer_status(session, current_attempt(session.name))


def _open_attempt(session_name: str, msisdn: str, timeout_minutes: int) -> str:
	"""Save a new Initiated attempt and commit it, so its id is on record before the provider hears of it."""
	session = frappe.get_doc("Local Payment", session_name, for_update=True)
	_ensure_open(session.status)
	try:
		lc.ensure_can_start_attempt(row.status for row in session.attempts)
	except lc.AttemptInProgress:
		frappe.throw(_("A payment request is already waiting for your approval on your phone."))

	now = now_datetime()
	attempt_id = str(uuid.uuid4())
	session.append(
		"attempts",
		{
			"attempt_id": attempt_id,
			"status": lc.INITIATED,
			"payer_msisdn": msisdn,
			"started_on": now,
			"expires_on": now + timedelta(minutes=timeout_minutes),
			# No status check before the provider has seen the request. _record_start brings it forward.
			"next_check_on": now + rc.INITIATION_WINDOW,
		},
	)
	session.save(ignore_permissions=True)
	# If the provider call times out or the worker dies, the attempt must still be there to query. The
	# commit also releases the row lock before the call.
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- attempt_id on record before the provider call
	return attempt_id


def _ensure_open(status: str) -> None:
	if status != lc.OPEN:
		frappe.throw(_("This payment can no longer be made from this page."))


def _record_start(session_name: str, attempt_id: str, started: Initiated) -> None:
	session = frappe.get_doc("Local Payment", session_name, for_update=True)
	row = rc._attempt_row(session, attempt_id)
	row.integration_request = started.integration_request
	# Once the initiation window is over, a status check may have moved the attempt on already.
	if row.status == lc.INITIATED:
		if started.status == lc.ERROR:
			lc.check_attempt_transition(row.status, lc.ERROR)
			row.status = lc.ERROR
			row.next_check_on = None
		else:
			# The provider has the request: status checks may start.
			row.next_check_on = now_datetime()
	session.save(ignore_permissions=True)


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


# MTN calls back once, unauthenticated. Its body is never read (D3): the call only queues reconcile().
# Per attempt, the job's deduplication and reconcile's minimum interval bound the work.
@frappe.whitelist(allow_guest=True, methods=["PUT", "POST"])  # nosemgrep: guest-whitelisted-method
@rate_limit(limit=CALLBACKS_PER_ADDRESS, seconds=60)
def mtn_momo_callback() -> None:
	"""Queue a status check for a known attempt still in play. Every other call gets the same empty answer."""
	# Frappe builds form_dict from the JSON body alone when there is one, so read the query string.
	attempt = frappe.request.args.get("attempt")
	if not attempt or not ATTEMPT_ID_PATTERN.match(attempt):
		return
	status = frappe.db.get_value(
		"Local Payment Attempt", {"attempt_id": attempt, "parenttype": "Local Payment"}, "status"
	)
	if status in rc.CHECKABLE_STATES:
		# reconcile() commits, so it runs in a job, not inside this request. One queued job per attempt.
		frappe.enqueue(
			"local_payments.reconcile.reconcile",
			attempt_id=attempt,
			job_id=f"local_payments:reconcile:{attempt}",
			deduplicate=True,
		)


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
		and not rc.before_first_check(attempt.check_count, attempt.next_check_on)
	)
