# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

"""Nokash client: payin request and status query.

No frappe import. The caller passes the contract's keys, and turns an `Initiation` into an attempt
status itself.
"""

import hashlib
import hmac
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

import requests

from local_payments import lifecycle as lc
from local_payments.lifecycle import ProviderResult
from local_payments.providers.common import (
	DEFAULT_TIMEOUT,
	RESPONSE_LOG_MAX_LENGTH,
	Exchange,
	InvalidRequest,
	as_text,
	describe,
	json_body,
	whole_amount,
)
from local_payments.providers.msisdn import normalize_msisdn

PAYIN_PATH = "/lapas-on-trans/trans/api-payin-request/407"
STATUS_PATH = "/lapas-on-trans/trans/310/status-request"

# Nokash answers 403 Unauthorized, body "Unauthorized", to an authenticated call that does not carry
# this header. The value identifies the integrator while the account is in integration mode.
INTEGRATION_HEADER = "X-NK-Integration"

# The offer covers Cameroon only, and the status answer names no currency.
CURRENCY = "XAF"
COUNTRY = "CM"
PAYMENT_TYPE = "CM_MOBILEMONEY"
MSISDN_PREFIX = "237"
MSISDN_NATIONAL_LENGTH = 9

MTN_MOMO = "MTN_MOMO"
ORANGE_MONEY = "ORANGE_MONEY"

REQUEST_OK = "REQUEST_OK"
REQUEST_BAD_INFOS = "REQUEST_BAD_INFOS"

STATUSES = {
	"PENDING": lc.PENDING,
	"SUCCESS": lc.SUCCEEDED,
	"FAILED": lc.FAILED,
	"CANCELED": lc.FAILED,
	"TIMEOUT": lc.EXPIRED,
}


class NokashError(Exception):
	"""A status query that got no usable answer: the attempt's state is still unknown."""

	def __init__(self, http_status: int | None = None, code: str | None = None):
		self.http_status = http_status
		self.code = code
		detail = ", ".join(str(part) for part in (http_status, code) if part)
		super().__init__("Nokash status query failed" + (f" ({detail})" if detail else ""))


class InitiationOutcome(StrEnum):
	ACCEPTED = "accepted"  # REQUEST_OK with a transaction id
	REJECTED = "rejected"  # REQUEST_BAD_INFOS: nothing was created
	UNKNOWN = "unknown"  # timeout, 5xx or anything else: the request may exist


@dataclass(frozen=True)
class Initiation:
	outcome: InitiationOutcome
	transaction_id: str | None = None
	http_status: int | None = None
	code: str | None = None


@dataclass(frozen=True)
class NokashConfig:
	api_base_url: str
	i_space_key: str = field(repr=False)
	app_space_key: str = field(repr=False)
	# Empty once Nokash recognises the caller by its address instead.
	integration_key: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class NokashData:
	"""What this provider keeps in an attempt's provider_data."""

	operator: str


class _Unreadable(Exception):
	pass


class NokashClient:
	def __init__(
		self,
		config: NokashConfig,
		http: requests.Session | None = None,
		on_exchange: Callable[[Exchange], None] | None = None,
	):
		self.config = config
		self.http = http or requests.Session()
		# Told about the payin, never about a status query.
		self.on_exchange = on_exchange

	def payin(
		self, attempt_id: str, amount, msisdn: str, operator: str, callback_url: str | None = None
	) -> Initiation:
		"""Ask Nokash to push a payment request to the payer's phone.

		`attempt_id` goes out as order_id. The answer only gives the transaction id: a status check decides
		the outcome. Raises InvalidRequest, before any call, for a bad number or amount.
		"""
		# The signature and the body use these same values.
		amount, phone = whole_amount(amount), payer_number(msisdn)

		# Everything sent except the keys, which is what may be logged.
		loggable = {
			"payment_type": PAYMENT_TYPE,
			"country": COUNTRY,
			"payment_method": operator,
			"order_id": attempt_id,
			"amount": amount,
			"user_data": {"user_phone": phone},
		}
		if callback_url:
			loggable["callback_url"] = callback_url

		url = self._url(PAYIN_PATH)
		# Frappe masks traceback variables whose name contains "secret", so this one never reaches a log.
		signature_secret = sign(attempt_id, amount, phone, self.config.app_space_key, self.config.i_space_key)
		try:
			response = self.http.request(
				"POST",
				url,
				headers=self._headers({"hmac-signature": signature_secret}),
				json={
					"i_space_key": self.config.i_space_key,
					"app_space_key": self.config.app_space_key,
					**loggable,
				},
				timeout=DEFAULT_TIMEOUT,
				# A 307 would send the keys to whatever address it names.
				allow_redirects=False,
			)
		except requests.RequestException as exc:
			self._report(Exchange("POST", url, {}, loggable, error=type(exc).__name__))
			return Initiation(InitiationOutcome.UNKNOWN)

		self._report(
			Exchange(
				"POST",
				url,
				{},
				loggable,
				response.status_code,
				self._without_secrets(response.text, signature_secret)[:RESPONSE_LOG_MAX_LENGTH] or None,
			)
		)
		return read_payin(response)

	def check_status(self, transaction_id: str, attempt_id: str) -> ProviderResult:
		"""Ask Nokash where a transaction stands. Raises NokashError when it gives no usable answer."""
		try:
			response = self.http.request(
				"POST",
				self._url(STATUS_PATH),
				headers=self._headers({"Content-Type": "application/json"}),
				params={"transaction_id": transaction_id},
				timeout=DEFAULT_TIMEOUT,
				allow_redirects=False,
			)
		except requests.RequestException:
			raise NokashError()

		try:
			return read_status(response, transaction_id, attempt_id)
		except _Unreadable as unreadable:
			# Raised here, not in read_status, so the logged traceback leaves out the payer's data.
			raise NokashError(response.status_code, str(unreadable) or None) from None

	def _headers(self, headers: dict) -> dict:
		"""The call's own headers, plus the integration one while the contract needs it."""
		if self.config.integration_key:
			return {**headers, INTEGRATION_HEADER: self.config.integration_key}
		return headers

	def _without_secrets(self, text: str, signature_secret: str) -> str:
		"""Nokash's answer, with anything it repeated from the request masked."""
		secrets = (
			self.config.i_space_key,
			self.config.app_space_key,
			self.config.integration_key,
			signature_secret,
		)
		for secret in secrets:
			if secret:
				text = text.replace(secret, "***")
		return text

	def _report(self, exchange: Exchange) -> None:
		if self.on_exchange:
			self.on_exchange(exchange)

	def _url(self, path: str) -> str:
		return self.config.api_base_url.rstrip("/") + path


def sign(order_id: str, amount: int, user_phone: str, app_space_key: str, i_space_key: str) -> str:
	"""The payin's hmac-signature header."""
	signed = f"{order_id}:{amount}:{user_phone}:{app_space_key}"
	return hmac.new(i_space_key.encode(), signed.encode(), hashlib.sha256).hexdigest()


def payer_number(msisdn: str) -> str:
	"""The number as Nokash expects it: 237 then the 9-digit national number."""
	normalized = normalize_msisdn(msisdn, MSISDN_PREFIX, MSISDN_NATIONAL_LENGTH)
	if not normalized:
		# The number itself stays out of the message: it is personal data.
		raise InvalidRequest(f"The phone number must have {MSISDN_NATIONAL_LENGTH} digits.")
	return normalized


def envelope(response) -> tuple[str | None, dict]:
	"""The request-level status of an answer and its data, empty unless it is an object."""
	body = json_body(response)
	data = body.get("data")
	return as_text(body.get("status")), data if isinstance(data, dict) else {}


def read_payin(response) -> Initiation:
	answer, data = envelope(response)
	status = response.status_code
	if status >= 500:
		return Initiation(InitiationOutcome.UNKNOWN, http_status=status, code=answer)
	if answer == REQUEST_BAD_INFOS:
		return Initiation(InitiationOutcome.REJECTED, http_status=status, code=answer)

	transaction_id = data.get("id")
	if answer == REQUEST_OK and transaction_id and isinstance(transaction_id, str):
		return Initiation(InitiationOutcome.ACCEPTED, transaction_id, status, answer)
	return Initiation(InitiationOutcome.UNKNOWN, http_status=status, code=answer)


def read_status(response, transaction_id: str, attempt_id: str) -> ProviderResult:
	"""Raises _Unreadable unless this is a REQUEST_OK about this attempt, carrying a status we know."""
	answer, data = envelope(response)
	if response.status_code != 200 or answer != REQUEST_OK:
		raise _Unreadable((answer or "")[:40])

	# Only compared when present: the contract comes from a simulated API.
	for name, expected in (("id", transaction_id), ("orderId", attempt_id)):
		if data.get(name) not in (None, expected):
			raise _Unreadable(f"{name} mismatch")

	raw_status = data.get("status")
	if not isinstance(raw_status, str) or raw_status not in STATUSES:
		raise _Unreadable(f"status {str(raw_status)[:40]}")

	return ProviderResult(
		STATUSES[raw_status],
		amount=as_text(data.get("amount")),
		currency=CURRENCY,
		# reconcile writes this over the attempt's id, so it can't be left empty.
		transaction_id=transaction_id,
		provider_status=describe(raw_status, as_text(data.get("statusReason"))),
	)
