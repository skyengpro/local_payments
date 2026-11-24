# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

from collections.abc import Callable
from datetime import timedelta

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt

from local_payments import lifecycle as lc
from local_payments.gateway import Initiated, LocalPaymentGateway, OperatorChoice
from local_payments.lifecycle import ProviderResult
from local_payments.providers.common import Exchange, InvalidRequest
from local_payments.providers.nokash import (
	CURRENCY,
	MSISDN_NATIONAL_LENGTH,
	MSISDN_PREFIX,
	InitiationOutcome,
	NokashClient,
	NokashConfig,
	NokashData,
	NokashError,
	callback_signature_valid,
)

CALLBACK_PATH = "/api/method/local_payments.api.nokash_callback"


class NokashSettings(OperatorChoice, LocalPaymentGateway, Document):
	"""One Nokash merchant contract, for one or both operators.

	gateway_name is set only once: the payment gateway is created under that name and is never renamed.
	"""

	provider_name = "Nokash"
	initiation_name = "Payin"
	# Nokash asks for at most one status call every 30 seconds per transaction.
	min_check_interval = timedelta(seconds=30)
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

	def check_status(self, attempt_id: str, provider_data: dict) -> ProviderResult:
		transaction_id = frappe.db.get_value(
			"Local Payment Attempt",
			{"attempt_id": attempt_id, "parenttype": "Local Payment"},
			"provider_transaction_id",
		)
		if not transaction_id:
			# The payin's answer never arrived. Pending lets the attempt reach its deadline.
			return ProviderResult(lc.PENDING, provider_status="No Nokash transaction id", no_reference=True)
		try:
			return self.nokash_client().check_status(transaction_id, attempt_id)
		except NokashError as error:
			if error.refused:
				self.alert_refused(attempt_id)
			raise

	def verify_callback(
		self, body: dict, header: str | None, attempt_id: str, transaction_id: str | None
	) -> bool:
		"""True for a notification Nokash signed with our key, about this very attempt."""
		if not callback_signature_valid(body, header, self.get_password("i_space_key")):
			return False
		if body.get("orderId") != attempt_id:
			return False
		# An attempt that never got its id can't be compared: the signature and the order id have to do.
		return not transaction_id or body.get("id") == transaction_id

	def initiate(self, attempt_id: str, session, msisdn: str, provider_data: dict) -> Initiated:
		exchanges = []
		try:
			initiation = self.nokash_client(on_exchange=exchanges.append).payin(
				attempt_id,
				session.amount,
				msisdn,
				NokashData(**provider_data).operator,
				callback_url=self.callback_url(attempt_id) if cint(self.send_callback) else None,
			)
		except InvalidRequest as refused:
			# Refused before any call, so there is nothing at Nokash to query.
			self.log_refusal(attempt_id, session.name, str(refused))
			return Initiated(lc.ERROR)

		status, alert = lc.INITIATED, None
		if initiation.outcome == InitiationOutcome.REJECTED:
			# The number, amount and operator passed our checks, so this points at the keys or the settings.
			status, alert = lc.ERROR, "provider_refused"
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
			initiation.transaction_id,
			alert,
		)

	def nokash_client(self, on_exchange: Callable[[Exchange], None] | None = None) -> NokashClient:
		config = NokashConfig(
			api_base_url=self.api_base_url,
			i_space_key=self.get_password("i_space_key"),
			app_space_key=self.get_password("app_space_key"),
			integration_key=self.get_password("integration_key", raise_exception=False),
		)
		return NokashClient(config, on_exchange=on_exchange)

	def validate(self):
		self.validate_gateway_name_unchanged()
		self.validate_api_base_url()
		self.validate_operators()
		self.set_pending_timeout()
		self.set_minimum_amount()
		self.validate_callback_host()
		if self.environment == "Production":
			self.warn_if_no_alert_manager()

	def set_minimum_amount(self):
		# An Int column can't be empty, so a cleared field arrives as 0 and falls back to the default.
		minimum = cint(self.minimum_amount)
		if minimum < 0:
			frappe.throw(_("Minimum Amount cannot be negative."))
		if minimum == 0:
			self.minimum_amount = cint(self.meta.get_field("minimum_amount").default)
