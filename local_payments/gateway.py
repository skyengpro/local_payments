# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

"""The frappe/payments contract shared by every Settings doctype of this app.

A Settings doctype inherits `LocalPaymentGateway`, sets `provider_name` and `initiation_name`, implements
`check_status` and `initiate`, and has the attributes `gateway_name`, `enabled`, `currency`, `msisdn_prefix`,
`msisdn_national_length` and `pending_timeout_minutes`. One the provider can call back also sets `callback_path`
and has `environment` and `send_callback`. Nothing provider-specific belongs in this file.
"""

from dataclasses import dataclass
from math import isfinite
from urllib.parse import urlencode, urlsplit

import frappe
from frappe import _
from frappe.utils import call_hook_method, cint, flt, get_url
from frappe.utils.user import get_users_with_role
from payments.utils import create_payment_gateway

from local_payments import reconcile as rc
from local_payments.lifecycle import ProviderResult
from local_payments.providers.common import Exchange
from local_payments.providers.msisdn import normalize_msisdn

CHECKOUT_PAGE = "local_payment_checkout"
MTN_MOMO_ICON = "/assets/local_payments/images/operators/mtn_momo.png"

# The Settings doctypes of this app's gateways. A Payment Gateway is ours if its gateway_settings is one of them.
SETTINGS_DOCTYPES = ("MTN MoMo Settings", "Orange Money Settings", "Nokash Settings")

# ISO 4217 currencies with no minor unit. Frappe's Currency records can't be used for this:
# XAF is stored there with 100 fraction units.
ZERO_DECIMAL_CURRENCIES = frozenset(
	{"BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG", "RWF", "UGX", "UYI", "VND", "VUV"}
	| {"XAF", "XOF", "XPF"}
)

# Consumer fields copied onto the session. Everything else only lives in request_data.
SESSION_FIELDS = ("title", "description", "payer_name", "payer_email", "redirect_to")

NETWORK_LOG_SAVEPOINT = "lp_network_log"


@dataclass(frozen=True)
class Initiated:
	"""What starting an attempt did: its status, the exchange's Integration Request, the provider's id."""

	status: str
	integration_request: str | None = None
	transaction_id: str | None = None


def log_error(title: str, session_name: str | None = None, detail: str | None = None) -> None:
	"""Log the current exception without its frames' variables, which can hold the payer's number.

	Queued in Redis and inserted later: Frappe rolls back a failed request, and every GET request.
	"""
	traceback = frappe.get_traceback()
	frappe.log_error(
		title=title,
		message=f"{detail}\n\n{traceback}" if detail else traceback,
		reference_doctype="Local Payment",
		reference_name=session_name,
		defer_insert=True,
	)


def callback_base(host_name: str | None, production: bool) -> str | None:
	"""The address a provider can call back, from the site's host_name. None if there is none it can use.

	Production needs an https:// address. get_url() is not used: without host_name it falls back to the
	site name over http, and it adds the dev server's port.
	"""
	host_name = (host_name or "").strip()
	# No scheme means http, as in Frappe.
	url = urlsplit(host_name if "://" in host_name else f"http://{host_name}")
	try:
		port = url.port
	except ValueError:
		return None
	if url.scheme not in ("https", "http") or not url.hostname:
		return None
	if production and url.scheme != "https":
		return None
	# Host and port only: credentials in host_name would reach the provider and the Integration Request.
	return f"{url.scheme}://{url.hostname}" + (f":{port}" if port else "")


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
	# How the network log names the initiation call.
	initiation_name: str
	# Where the provider calls back, for a gateway that can be called back.
	callback_path: str

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

	def initiate(self, attempt_id: str, session, msisdn: str, provider_data: dict) -> Initiated:
		"""Send the payment request for an attempt already saved as Initiated.

		Each provider's Settings doctype implements this. `msisdn` comes from `payer_msisdn()`, and
		`provider_data` from `attempt_data()`.
		"""
		raise NotImplementedError

	def payer_operators(self) -> list[dict]:
		"""The operators the payer picks from on the page, as {value, label, icon}. Empty when there is no choice."""
		return []

	def payer_icon(self) -> str | None:
		"""The logo above the number field when the payer has no operator to pick."""
		return None

	def attempt_data(self, operator: str | None) -> dict:
		"""What to keep in a new attempt's provider_data. Raises a ValidationError for a refused operator."""
		return {}

	def payer_msisdn(self, number) -> str:
		"""The payer's number as providers expect it. Raises a ValidationError the page shows under the field."""
		prefix, length = self.msisdn_prefix, cint(self.msisdn_national_length)
		msisdn = normalize_msisdn(number, prefix, length)
		if not msisdn:
			frappe.throw(
				_("Enter your mobile money number: {0} digits, with or without +{1}.").format(length, prefix)
			)
		return msisdn

	def callback_url(self, attempt_id: str) -> str | None:
		"""None when the site has no address the provider can call. Status checks then find the outcome on their own."""
		# Never from the request's Host header, which the payer chooses.
		base = self.site_callback_base()
		if not base:
			return None
		# The attempt_id, never the session token: this URL is public and the token opens the checkout page.
		return f"{base}{self.callback_path}?{urlencode({'attempt': attempt_id})}"

	def site_callback_base(self) -> str | None:
		return callback_base(frappe.conf.host_name or frappe.conf.hostname, self.environment == "Production")

	def validate_callback_host(self):
		if (
			cint(self.enabled)
			and cint(self.send_callback)
			and self.environment == "Production"
			and not self.site_callback_base()
		):
			frappe.throw(
				_(
					"{0} callbacks need an https:// address. Set host_name in the site config to the site's public https:// address, or uncheck Send Callback."
				).format(self.provider_name)
			)

	def validate_gateway_name_unchanged(self):
		# Without this, Frappe would silently put the document name back into gateway_name.
		if not self.is_new() and self.gateway_name != self.name:
			frappe.throw(
				_("Gateway Name cannot be changed once saved."), exc=frappe.CannotChangeConstantError
			)

	def validate_api_base_url(self):
		# validate() runs before Frappe's mandatory check, which reports a missing value itself.
		if not self.api_base_url:
			return
		self.api_base_url = self.api_base_url.strip()
		# Merchant credentials are sent to this URL.
		url = urlsplit(self.api_base_url)
		if url.scheme != "https" or not url.hostname:
			frappe.throw(_("API Base URL must be an https:// address."))

	def set_pending_timeout(self):
		# An Int column can't be empty, so a cleared field arrives as 0 and falls back to the default.
		timeout = cint(self.pending_timeout_minutes)
		if timeout < 0:
			frappe.throw(_("Pending Timeout (Minutes) cannot be negative."))
		if timeout == 0:
			self.pending_timeout_minutes = cint(self.meta.get_field("pending_timeout_minutes").default)

	def warn_if_no_alert_manager(self):
		if not cint(self.enabled) or get_users_with_role(rc.MANAGER_ROLE):
			return
		frappe.msgprint(
			_(
				"No active user has the {0} role. Payment alerts will go to System Managers, or to Administrator if there are none. Assign the role to at least one person."
			).format(rc.MANAGER_ROLE),
			title=_("Role not assigned"),
			indicator="orange",
		)

	def log_refusal(self, attempt_id: str, session_name: str, detail: str) -> None:
		# The detail names the outcome and the provider's codes only, never the payer's number.
		frappe.log_error(
			title=f"{self.provider_name} payment request refused",
			message=f"Attempt {attempt_id}: {detail}",
			reference_doctype="Local Payment",
			reference_name=session_name,
		)

	def log_exchanges(
		self, attempt_id: str, session_name: str, exchanges: list[Exchange], taken_in: bool
	) -> str | None:
		"""One Integration Request per attempt: the request as sent, then the answer to each send.

		A log that can't be saved is reported and skipped: it must not cost what the provider answered.
		"""
		if not exchanges:
			return None
		request = exchanges[0]
		log = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": self.provider_name,
				"request_description": self.initiation_name,
				"request_id": attempt_id,
				"reference_doctype": "Local Payment",
				"reference_docname": session_name,
				"url": request.url,
				"request_headers": frappe.as_json(request.request_headers),
				"data": frappe.as_json(request.request_body),
				"output": frappe.as_json(
					[
						{"status_code": sent.status_code, "body": sent.response_body, "error": sent.error}
						for sent in exchanges
					]
				),
				"status": "Completed" if taken_in else "Failed",
			}
		)
		frappe.db.savepoint(NETWORK_LOG_SAVEPOINT)
		try:
			# Not create_request_log(), which commits. The payer is a Guest, and only System Manager may create one.
			log.insert(ignore_permissions=True)
		except Exception:
			frappe.db.rollback(save_point=NETWORK_LOG_SAVEPOINT)
			log_error(f"{self.provider_name} network log failed", session_name)
			return None
		return log.name

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
		rc.save_state(session)
		return session.token
