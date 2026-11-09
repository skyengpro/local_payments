# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

from collections.abc import Callable
from dataclasses import asdict

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from local_payments import lifecycle as lc
from local_payments.gateway import MTN_MOMO_ICON, Initiated, LocalPaymentGateway
from local_payments.lifecycle import ProviderResult
from local_payments.providers.common import Exchange, InvalidRequest
from local_payments.providers.nokash import (
	CURRENCY,
	MSISDN_NATIONAL_LENGTH,
	MSISDN_PREFIX,
	MTN_MOMO,
	ORANGE_MONEY,
	InitiationOutcome,
	NokashClient,
	NokashConfig,
	NokashData,
)

OPERATOR_FIELDS = {MTN_MOMO: "accept_mtn_momo", ORANGE_MONEY: "accept_orange_money"}
OPERATOR_ICONS = {
	MTN_MOMO: MTN_MOMO_ICON,
	ORANGE_MONEY: "/assets/local_payments/images/operators/orange_money.svg",
}


class NokashSettings(LocalPaymentGateway, Document):
	"""One Nokash merchant contract, for one or both operators.

	gateway_name is set only once: the payment gateway is created under that name and is never renamed.
	"""

	provider_name = "Nokash"
	initiation_name = "Payin"
	# Fixed for Cameroon, so these are not fields.
	currency = CURRENCY
	msisdn_prefix = MSISDN_PREFIX
	msisdn_national_length = MSISDN_NATIONAL_LENGTH

	def check_status(self, attempt_id: str, provider_data: dict) -> ProviderResult:
		transaction_id = frappe.db.get_value(
			"Local Payment Attempt",
			{"attempt_id": attempt_id, "parenttype": "Local Payment"},
			"provider_transaction_id",
		)
		if not transaction_id:
			# The payin's answer never arrived. Pending lets the attempt reach its deadline.
			return ProviderResult(lc.PENDING, provider_status="No Nokash transaction id", no_reference=True)
		return self.nokash_client().check_status(transaction_id, attempt_id)

	def initiate(self, attempt_id: str, session, msisdn: str, provider_data: dict) -> Initiated:
		exchanges = []
		try:
			initiation = self.nokash_client(on_exchange=exchanges.append).payin(
				attempt_id, session.amount, msisdn, NokashData(**provider_data).operator
			)
		except InvalidRequest as refused:
			# Refused before any call, so there is nothing at Nokash to query.
			self.log_refusal(attempt_id, session.name, str(refused))
			return Initiated(lc.ERROR)

		status = lc.INITIATED
		if initiation.outcome == InitiationOutcome.REJECTED:
			status = lc.ERROR
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
		)

	def payer_operators(self) -> list[dict]:
		labels = {MTN_MOMO: _("MTN Mobile Money"), ORANGE_MONEY: _("Orange Money")}
		offered = self.offered_operators()
		# A single operator leaves nothing to choose.
		if len(offered) < 2:
			return []
		return [{"value": op, "label": labels[op], "icon": OPERATOR_ICONS[op]} for op in offered]

	def payer_icon(self) -> str | None:
		offered = self.offered_operators()
		return OPERATOR_ICONS[offered[0]] if len(offered) == 1 else None

	def attempt_data(self, operator: str | None) -> dict:
		offered = self.offered_operators()
		if not operator and len(offered) == 1:
			operator = offered[0]
		if operator not in offered:
			frappe.throw(_("Choose your mobile money operator."))
		return asdict(NokashData(operator))

	def offered_operators(self) -> list[str]:
		return [operator for operator, field in OPERATOR_FIELDS.items() if cint(self.get(field))]

	def nokash_client(self, on_exchange: Callable[[Exchange], None] | None = None) -> NokashClient:
		config = NokashConfig(
			api_base_url=self.api_base_url,
			i_space_key=self.get_password("i_space_key"),
			app_space_key=self.get_password("app_space_key"),
		)
		return NokashClient(config, on_exchange=on_exchange)

	def validate(self):
		self.validate_gateway_name_unchanged()
		self.validate_api_base_url()
		self.validate_operators()
		self.set_pending_timeout()
		self.warn_if_no_alert_manager()

	def validate_operators(self):
		if not self.offered_operators():
			frappe.throw(_("Accept at least one operator."))
