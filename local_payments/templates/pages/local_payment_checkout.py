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
	context.no_cache = 1
	context.update(
		token=frappe.form_dict.token,
		session_title=session.title,
		amount=_display_amount(session),
		currency=session.currency,
		provider=api.provider_name(session.payment_gateway),
		status=session.status,
		redirect_url=api.exit_url(session) if session.status == lc.PAID else None,
		waiting=bool(attempt and attempt.status in rc.CHECKABLE_STATES),
	)


def _display_amount(session) -> str:
	"""The amount as the currency writes it. Currencies without a minor unit show no decimals."""
	precision = 0 if session.currency in ZERO_DECIMAL_CURRENCIES else None
	return fmt_money(session.amount, precision, currency=session.currency)
