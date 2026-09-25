# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

"""The frappe/payments contract shared by every Settings doctype of this app.

A Settings doctype inherits `LocalPaymentGateway`, sets `provider_name`, implements `check_status` and
`initiate`, and has the fields `gateway_name`, `enabled`, `currency`, `msisdn_prefix`,
`msisdn_national_length` and `pending_timeout_minutes`. Nothing provider-specific belongs in this file.
"""

from dataclasses import dataclass
from math import isfinite
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.utils import call_hook_method, cint, flt, get_url
from payments.utils import create_payment_gateway

from local_payments.lifecycle import ProviderResult
from local_payments.providers.msisdn import normalize_msisdn

CHECKOUT_PAGE = "local_payment_checkout"

# ISO 4217 currencies with no minor unit. Frappe's Currency records can't be used for this:
# XAF is stored there with 100 fraction units.
ZERO_DECIMAL_CURRENCIES = frozenset(
	{"BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG", "RWF", "UGX", "UYI", "VND", "VUV"}
	| {"XAF", "XOF", "XPF"}
)

# Consumer fields copied onto the session. Everything else only lives in request_data.
SESSION_FIELDS = ("title", "description", "payer_name", "payer_email", "redirect_to")


@dataclass(frozen=True)
class Initiated:
	"""What starting an attempt did: the attempt's status, and the Integration Request of the exchange."""

	status: str
	integration_request: str | None = None


def validate_amount_for_currency(amount, currency: str) -> None:
	"""Reject amounts that aren't finite and positive, and fractions in a currency with no minor unit."""
	amount = flt(amount)
	if not isfinite(amount) or amount <= 0:
		frappe.throw(_("Amount must be strictly positive."))
	if currency in ZERO_DECIMAL_CURRENCIES and amount != int(amount):
		frappe.throw(_("{0} has no minor unit, so the amount must be a whole number.").format(currency))


class LocalPaymentGateway:
	"""Mixin for Settings doctypes. Put it before `Document` in the base classes.

	A subclass that defines `on_update` must call `super().on_update()`.
	"""

	provider_name: str

	@property
	def payment_gateway_name(self) -> str:
		return f"{self.provider_name}-{self.gateway_name}"

	def on_update(self):
		create_payment_gateway(self.payment_gateway_name, settings=self.doctype, controller=self.gateway_name)
		if cint(self.enabled):
			call_hook_method("payment_gateway_enabled", gateway=self.payment_gateway_name)

	def check_status(self, attempt_id: str, provider_data: dict) -> ProviderResult:
		"""Ask the provider about one attempt. Each provider's Settings doctype implements this."""
		raise NotImplementedError

	def initiate(self, attempt_id: str, session, msisdn: str) -> Initiated:
		"""Send the payment request for an attempt already saved as Initiated.

		Each provider's Settings doctype implements this. `msisdn` comes from `payer_msisdn()`.
		"""
		raise NotImplementedError

	def payer_msisdn(self, number) -> str:
		"""The payer's number as providers expect it. Raises a ValidationError the page shows under the field."""
		prefix, length = self.msisdn_prefix, cint(self.msisdn_national_length)
		msisdn = normalize_msisdn(number, prefix, length)
		if not msisdn:
			frappe.throw(
				_("Enter your mobile money number: {0} digits, with or without +{1}.").format(length, prefix)
			)
		return msisdn

	def validate_transaction_currency(self, currency):
		if currency != self.currency:
			frappe.throw(
				_(
					"This payment method only accepts {0}, not {1}. Please select another payment method."
				).format(self.currency, currency)
			)

	def get_payment_url(self, **kwargs):
		"""Create (or reuse) a Local Payment session and return its checkout URL.

		Never contacts the provider: the payer starts the attempt from the checkout page.
		"""
		if not cint(self.enabled):
			frappe.throw(_("The payment gateway {0} is disabled.").format(self.payment_gateway_name))

		amount, currency = flt(kwargs.get("amount")), kwargs.get("currency")
		self.validate_transaction_currency(currency)
		validate_amount_for_currency(amount, currency)

		token = self._find_open_session(kwargs, amount, currency) or self._create_session(
			kwargs, amount, currency
		)
		return get_url(f"/{CHECKOUT_PAGE}?{urlencode({'token': token})}")

	def _find_open_session(self, kwargs, amount, currency) -> str | None:
		return frappe.db.get_value(
			"Local Payment",
			{
				"status": "Open",
				"reference_doctype": kwargs.get("reference_doctype"),
				"reference_docname": kwargs.get("reference_docname"),
				"payment_gateway": self.payment_gateway_name,
				"amount": amount,
				"currency": currency,
			},
			"token",
		)

	def _create_session(self, kwargs, amount, currency) -> str:
		session = frappe.get_doc(
			{
				"doctype": "Local Payment",
				"payment_gateway": self.payment_gateway_name,
				"reference_doctype": kwargs.get("reference_doctype"),
				"reference_docname": kwargs.get("reference_docname"),
				"amount": amount,
				"currency": currency,
				"request_data": frappe.as_json(kwargs),
				**{field: kwargs.get(field) for field in SESSION_FIELDS},
			}
		)
		# The consumer may be a Guest or a user without access to Local Payment.
		session.insert(ignore_permissions=True)
		return session.token
