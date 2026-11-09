# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

"""The page a payer opens from the link the consumer sent them.

Opening it only reads the session: no attempt is created and no status changes, however many times
the link is opened.
"""

import frappe
from frappe.utils import fmt_money

from local_payments import api
from local_payments import lifecycle as lc
from local_payments import reconcile as rc
from local_payments.gateway import ZERO_DECIMAL_CURRENCIES

no_cache = 1


def get_context(context):
	session = api.find_session(frappe.form_dict.get("token"))
	if not session:
		# An unknown or malformed token gets the site's 404, not an error page.
		raise frappe.PageDoesNotExistError

	attempt = api.current_attempt(session.name)
	# Only an Open session waits: on a Void one, polling would reload the page forever.
	waiting = session.status == lc.OPEN and bool(attempt and attempt.status in rc.CHECKABLE_STATES)
	context.update(
		token=frappe.form_dict.token,
		session_title=_display_title(session),
		amount=_display_amount(session),
		currency=session.currency,
		provider=api.provider_name(session.payment_gateway),
		status=session.status,
		redirect_url=api.exit_url(session) if session.status == lc.PAID else None,
		waiting=waiting,
		# The form stays hidden while waiting, and the page reloads when that ends.
		operators=api.payer_operators(session) if session.status == lc.OPEN and not waiting else [],
		# Any non-empty value replaces the "Powered by" credit in the site footer.
		footer_powered=" ",
	)


def _display_title(session) -> str | None:
	"""The title without the record id some consumers append to it, e.g. a course slug."""
	return session.title and session.title.removesuffix(f" {session.reference_docname}")


def _display_amount(session) -> str:
	"""The amount as the currency writes it. Currencies without a minor unit show no decimals."""
	precision = 0 if session.currency in ZERO_DECIMAL_CURRENCIES else None
	return fmt_money(session.amount, precision, currency=session.currency)
