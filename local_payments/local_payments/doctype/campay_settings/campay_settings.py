# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

import time
from collections.abc import Callable

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt

from local_payments import lifecycle as lc
from local_payments.gateway import Initiated, LocalPaymentGateway, OperatorChoice
from local_payments.lifecycle import ProviderResult
from local_payments.providers.campay import (
	BASE_URLS,
	CURRENCY,
	MSISDN_NATIONAL_LENGTH,
	MSISDN_PREFIX,
	CamPayClient,
	CamPayConfig,
	CamPayError,
	InitiationOutcome,
	webhook_signature_valid,
)
from local_payments.providers.common import Exchange, InvalidRequest

CALLBACK_PATH = "/api/method/local_payments.api.campay_callback"
# CamPay requires a description. This one names nobody, for a payment with no title.
FALLBACK_DESCRIPTION = "Payment"


class CamPaySettings(OperatorChoice, LocalPaymentGateway, Document):
	"""One CamPay application, for one or both operators.

	gateway_name is set only once: the payment gateway is created under that name and is never renamed.
	"""

	provider_name = "CamPay"
	initiation_name = "Collect"
	callback_path = CALLBACK_PATH
	# Fixed for Cameroon, so these are not fields.
	currency = CURRENCY
	msisdn_prefix = MSISDN_PREFIX
	msisdn_national_length = MSISDN_NATIONAL_LENGTH

	def validate_minimum_transaction_amount(self, currency, amount):
		if flt(amount) < cint(self.minimum_amount):
			frappe.throw(
				_("The minimum amount for this payment method is {0} {1}.").format(
					self.minimum_amount, CURRENCY
				)
			)
		if cint(self.maximum_amount) and flt(amount) > cint(self.maximum_amount):
			frappe.throw(
				_("The maximum amount for this payment method is {0} {1}.").format(
					self.maximum_amount, CURRENCY
				)
			)

	def check_status(self, attempt_id: str, provider_data: dict) -> ProviderResult:
		reference = frappe.db.get_value(
			"Local Payment Attempt",
			{"attempt_id": attempt_id, "parenttype": "Local Payment"},
			"provider_transaction_id",
		)
		if not reference:
			# The collect's answer never arrived. Pending lets the attempt reach its deadline.
			return ProviderResult(lc.PENDING, provider_status="No CamPay reference", no_reference=True)
		try:
			return self.campay_client().check_status(reference, attempt_id)
		except CamPayError as error:
			if error.refused:
				self.alert_refused(attempt_id)
			raise

	def verify_callback(
		self, body: dict, header: str | None, attempt_id: str, transaction_id: str | None
	) -> bool:
		"""True for a call CamPay signed with our webhook key, about this very attempt.

		The token names no transaction, so the parameters next to it only route the call, never decide it.
		"""
		webhook_key = self.get_password("webhook_key", raise_exception=False)
		if not webhook_signature_valid(body.get("signature"), webhook_key, time.time()):
			return False
		if body.get("external_reference") != attempt_id:
			return False
		# An attempt that never got its reference can't be compared: the token and the attempt id have to do.
		return not transaction_id or body.get("reference") == transaction_id

	def initiate(self, attempt_id: str, session, msisdn: str, provider_data: dict) -> Initiated:
		exchanges = []
		try:
			initiation = self.campay_client(on_exchange=exchanges.append).collect(
				attempt_id, session.amount, msisdn, self.description_for(session)
			)
		except InvalidRequest as refused:
			# Refused before any call, so there is nothing at CamPay to query.
			self.log_refusal(attempt_id, session.name, str(refused))
			return Initiated(lc.ERROR)

		status, alert = lc.INITIATED, None
		if initiation.outcome == InitiationOutcome.REJECTED:
			status = lc.ERROR
			# The payer can't cause a 401 or 403: the token or the application needs fixing.
			if initiation.credentials_refused:
				alert = "provider_refused"
			self.log_refusal(
				attempt_id,
				session.name,
				f"{initiation.outcome} ({initiation.http_status}, {initiation.code})",
			)
		return Initiated(
			status,
			self.log_exchanges(
				attempt_id, session.name, exchanges, initiation.outcome == InitiationOutcome.ACCEPTED
			),
			initiation.reference,
			alert,
		)

	def description_for(self, session) -> str:
		# str.replace, not format(): a stray brace typed in the settings must not break every payment.
		template = self.payment_description or "{title}"
		return template.replace("{title}", session.title or "").strip() or FALLBACK_DESCRIPTION

	def campay_client(self, on_exchange: Callable[[Exchange], None] | None = None) -> CamPayClient:
		config = CamPayConfig(api_base_url=self.api_base_url, access_token=self.get_password("access_token"))
		return CamPayClient(config, on_exchange=on_exchange)

	def validate(self):
		self.validate_gateway_name_unchanged()
		self.set_api_base_url()
		self.validate_api_base_url()
		self.validate_operators()
		self.set_pending_timeout()
		self.validate_amount_limits()
		self.set_webhook_url()
		self.validate_webhook_host()
		if self.environment == "Production":
			self.warn_if_no_alert_manager()

	def set_api_base_url(self):
		# Only a default follows the environment: an address typed by hand is kept.
		if self.api_base_url in BASE_URLS.values() and self.environment in BASE_URLS:
			self.api_base_url = BASE_URLS[self.environment]

	def validate_amount_limits(self):
		# An Int column can't be empty, so a cleared field arrives as 0 and falls back to the default.
		minimum, maximum = cint(self.minimum_amount), cint(self.maximum_amount)
		if minimum < 0:
			frappe.throw(_("Minimum Amount cannot be negative."))
		if minimum == 0:
			self.minimum_amount = minimum = cint(self.meta.get_field("minimum_amount").default)
		if maximum < 0:
			frappe.throw(_("Maximum Amount cannot be negative."))
		if maximum and maximum < minimum:
			frappe.throw(_("Maximum Amount must be 0, for no limit, or at least the Minimum Amount."))

	def set_webhook_url(self):
		base = self.site_callback_base()
		self.webhook_url = f"{base}{CALLBACK_PATH}" if base else None

	def validate_webhook_host(self):
		if (
			cint(self.enabled)
			and self.webhook_key
			and self.environment == "Production"
			and not self.site_callback_base()
		):
			frappe.throw(
				_(
					"CamPay webhooks need an https:// address. Set host_name in the site config to the site's public https:// address, or clear the Webhook Key."
				)
			)
