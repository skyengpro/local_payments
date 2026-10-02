# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

"""Settles ERPNext Payment Requests paid through this app's gateways, and closes their sessions on cancel.

Nothing here imports erpnext, so the module loads on a site without it. Its doc_events then never fire.
"""

from collections.abc import Iterable
from contextlib import contextmanager

import frappe
from frappe import _

from local_payments import lifecycle as lc
from local_payments import reconcile as rc
from local_payments.gateway import SETTINGS_DOCTYPES

SKIP = "skip"
SETTLE = "settle"
REFUSE = "refuse"

# What frappe/payments gateways pass to on_payment_authorized when the money is in.
PAID_STATUSES = ("Authorized", "Completed")

# What frappe.set_user() replaces on frappe.local, apart from the cache.
USER_STATE = (
	"session",
	"form_dict",
	"role_permissions",
	"new_doc_templates",
	"user_perms",
	"jenv_restricted",
	"jenv_unrestricted",
)


def settlement_action(is_ours: bool, status: str, pr_docstatus: int, pr_status: str) -> str:
	"""What the hook does with a Payment Request, read under lock: skip it, settle it, or refuse."""
	if not is_ours or status not in PAID_STATUSES or pr_status == "Paid":
		return SKIP
	if pr_docstatus != 1:
		return REFUSE
	return SETTLE


def unbooked_sessions(sessions: Iterable) -> list[str]:
	"""The sessions holding a payment that is not yet booked in ERPNext."""
	return [s.name for s in sessions if s.status == lc.PAID and s.authorization != rc.AUTH_DONE]


def on_payment_authorized(doc, method, status):
	"""Settle the Payment Request once one of our gateways is paid. Runs after the controller's method, if any."""
	is_ours = _is_ours(doc)
	# Leaves other gateways' requests unlocked.
	if settlement_action(is_ours, status, doc.docstatus, doc.status) == SKIP:
		return
	# Only a payment the provider confirmed to the merchant settles the request.
	if not frappe.db.exists("Local Payment", {**_sessions_of(doc), "status": lc.PAID}):
		return

	# Read again under lock: the request in memory may be stale, or already settled by another trigger.
	request = frappe.get_doc("Payment Request", doc.name, for_update=True)
	action = settlement_action(is_ours, status, request.docstatus, request.status)
	if action == SKIP:
		return
	if action == REFUSE:
		frappe.throw(
			_("Payment Request {0} is not submitted, so it can't be marked as paid.").format(doc.name)
		)

	# ERPNext checks the user's rights on accounts and on the referenced document, and the payer is a Guest.
	with as_administrator():
		request.set_as_paid()


def void_open_sessions(doc, method=None):
	"""Close the sessions of a cancelled Payment Request. Refuse the cancel while a payment is not booked."""
	if not _is_ours(doc):
		return
	names = frappe.get_all("Local Payment", _sessions_of(doc), pluck="name")
	if not names:
		return
	# By name, so only these rows lock. No wait: an authorization holds its session, then wants our request.
	try:
		sessions = frappe.db.get_values(
			"Local Payment",
			{"name": ["in", names]},
			["name", "status", "authorization"],
			as_dict=True,
			for_update=True,
			wait=False,
		)
	except frappe.QueryTimeoutError:
		frappe.throw(_("A payment for this request is being processed. Try again in a moment."))

	if unbooked := unbooked_sessions(sessions):
		frappe.throw(
			_(
				"A payment was received for this request and is not booked yet. Retry its authorization from {0} before cancelling."
			).format(unbooked[0])
		)

	lc.check_session_transition(lc.OPEN, lc.VOID)
	for row in sessions:
		if row.status == lc.OPEN:
			session = frappe.get_doc("Local Payment", row.name)
			session.status = lc.VOID
			session.save(ignore_permissions=True)


@contextmanager
def as_administrator():
	"""Run the block as Administrator, then put the caller's user and request state back as they were."""
	saved = {name: getattr(frappe.local, name, None) for name in USER_STATE}
	# ERPNext's create_payment_entry() sets this flag and never resets it.
	ignore_account_permission = frappe.flags.ignore_account_permission
	# set_user() writes into the session object, so give it a copy and keep the original untouched.
	frappe.local.session = frappe._dict(frappe.local.session)
	try:
		frappe.set_user("Administrator")
		yield
	finally:
		for name, value in saved.items():
			setattr(frappe.local, name, value)
		frappe.flags.ignore_account_permission = ignore_account_permission
		# Start empty: the caller's cache may be stale, and Administrator's must not outlive the block.
		frappe.local.cache = {}


def _is_ours(request) -> bool:
	gateway = request.payment_gateway
	return bool(gateway) and frappe.get_cached_value("Payment Gateway", gateway, "gateway_settings") in (
		SETTINGS_DOCTYPES
	)


def _sessions_of(request) -> dict:
	return {"reference_doctype": "Payment Request", "reference_docname": request.name}
