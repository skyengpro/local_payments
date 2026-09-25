# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

import re
from collections.abc import Callable
from urllib.parse import urlencode, urlsplit

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, get_url

from local_payments import lifecycle as lc
from local_payments.gateway import Initiated, LocalPaymentGateway
from local_payments.lifecycle import ProviderResult
from local_payments.providers.mtn_momo import (
	Exchange,
	InitiationOutcome,
	InvalidRequest,
	MtnMomoClient,
	MtnMomoConfig,
)

SANDBOX_HOST = "sandbox.momodeveloper.mtn.com"
CALLBACK_PATH = "/api/method/local_payments.api.mtn_momo_callback"

INITIATION_STATUSES = {
	InitiationOutcome.ACCEPTED: lc.INITIATED,
	# The reference already exists at MTN, so its status can be queried.
	InitiationOutcome.DUPLICATE_REFERENCE: lc.INITIATED,
	# The request may have reached MTN: only a status query can tell.
	InitiationOutcome.UNKNOWN: lc.INITIATED,
	InitiationOutcome.REJECTED: lc.ERROR,
	InitiationOutcome.UNAUTHORIZED: lc.ERROR,
	InitiationOutcome.NOT_SENT: lc.ERROR,
}
# Outcomes where MTN holds the request.
TAKEN_IN = frozenset({InitiationOutcome.ACCEPTED, InitiationOutcome.DUPLICATE_REFERENCE})


class CacheTokenStore:
	"""Access tokens in the Redis cache, which drops each one at its expiry."""

	def get(self, key: str) -> str | None:
		return frappe.cache.get_value(key, expires=True)

	def set(self, key: str, token: str, ttl_seconds: int) -> None:
		frappe.cache.set_value(key, token, expires_in_sec=ttl_seconds)

	def delete(self, key: str) -> None:
		frappe.cache.delete_value(key)


class MTNMoMoSettings(LocalPaymentGateway, Document):
	"""One MTN MoMo Collection merchant contract.

	gateway_name is set only once: the payment gateway is created under that name and is never renamed.
	"""

	provider_name = "MTN MoMo"

	@property
	def token_cache_key(self) -> str:
		return f"local_payments:mtn_momo_token:{self.name}"

	def on_update(self):
		super().on_update()
		# Changed credentials must not keep using a token issued for the old ones.
		CacheTokenStore().delete(self.token_cache_key)

	def check_status(self, attempt_id: str, provider_data: dict) -> ProviderResult:
		# MTN needs nothing beyond the attempt_id, so provider_data is not read.
		return self.mtn_client().check_status(attempt_id)

	def initiate(self, attempt_id: str, session, msisdn: str) -> Initiated:
		exchanges = []
		try:
			initiation = self.mtn_client(on_exchange=exchanges.append).request_to_pay(
				attempt_id,
				session.amount,
				msisdn,
				external_id=session.name,
				payer_message=self.payer_message,
				payee_note=self.payee_note,
				callback_url=self.callback_url(attempt_id) if cint(self.send_callback) else None,
			)
		except InvalidRequest as refused:
			# Refused before any call, so there is nothing at MTN to query.
			self.log_refusal(attempt_id, session.name, str(refused))
			return Initiated(lc.ERROR)

		status = INITIATION_STATUSES[initiation.outcome]
		if status == lc.ERROR:
			detail = ", ".join(str(part) for part in (initiation.http_status, initiation.code) if part)
			self.log_refusal(attempt_id, session.name, f"{initiation.outcome} ({detail or 'no response'})")
		return Initiated(
			status, self.log_exchanges(attempt_id, session.name, exchanges, initiation.outcome in TAKEN_IN)
		)

	def callback_url(self, attempt_id: str) -> str:
		# The attempt_id, never the session token: this URL is public and the token opens the checkout page.
		return get_url(f"{CALLBACK_PATH}?{urlencode({'attempt': attempt_id})}")

	def log_refusal(self, attempt_id: str, session_name: str, detail: str) -> None:
		# The detail names the outcome and MTN's codes only, never the payer's number.
		frappe.log_error(
			title="MTN MoMo payment request refused",
			message=f"Attempt {attempt_id}: {detail}",
			reference_doctype="Local Payment",
			reference_name=session_name,
		)

	def log_exchanges(
		self, attempt_id: str, session_name: str, exchanges: list[Exchange], taken_in: bool
	) -> str | None:
		"""One Integration Request per attempt: the request as sent, then MTN's answer to each send."""
		if not exchanges:
			return None
		request = exchanges[0]
		log = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": self.provider_name,
				"request_description": "RequestToPay",
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
		# Not create_request_log(), which commits. The payer is a Guest, and only System Manager may create one.
		log.insert(ignore_permissions=True)
		return log.name

	def mtn_client(self, on_exchange: Callable[[Exchange], None] | None = None) -> MtnMomoClient:
		config = MtnMomoConfig(
			api_base_url=self.api_base_url,
			target_environment=self.target_environment,
			subscription_key=self.get_password("subscription_key"),
			api_user=self.api_user,
			api_key=self.get_password("api_key"),
			currency=self.currency,
			msisdn_prefix=self.msisdn_prefix,
			msisdn_national_length=cint(self.msisdn_national_length),
			sandbox=self.environment == "Sandbox",
		)
		return MtnMomoClient(config, CacheTokenStore(), self.token_cache_key, on_exchange=on_exchange)

	def validate(self):
		self.validate_gateway_name_unchanged()
		self.validate_api_base_url()
		self.validate_msisdn_format()
		self.set_pending_timeout()

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
		# The sandbox reads EUR back as the contract currency, so it must never point at production.
		if self.environment == "Sandbox" and url.hostname != SANDBOX_HOST:
			frappe.throw(_("A Sandbox gateway must use {0}.").format(f"https://{SANDBOX_HOST}"))

	def validate_msisdn_format(self):
		if self.msisdn_prefix and not re.fullmatch(r"[0-9]+", self.msisdn_prefix):
			frappe.throw(_("MSISDN Prefix must contain digits only, without + or 00."))
		if cint(self.msisdn_national_length) <= 0:
			frappe.throw(_("MSISDN National Length must be greater than zero."))

	def set_pending_timeout(self):
		# An Int column can't be empty, so a cleared field arrives as 0 and falls back to the default.
		timeout = cint(self.pending_timeout_minutes)
		if timeout < 0:
			frappe.throw(_("Pending Timeout (Minutes) cannot be negative."))
		if timeout == 0:
			self.pending_timeout_minutes = cint(self.meta.get_field("pending_timeout_minutes").default)
