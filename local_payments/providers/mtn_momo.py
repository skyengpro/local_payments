# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

"""MTN MoMo Collection client: access token, RequestToPay and status query.

No frappe import. The caller passes the contract's configuration and a token store, and turns an
`Initiation` into an attempt status itself.
"""

import base64
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Protocol
from urllib.parse import quote

import requests

from local_payments import lifecycle as lc
from local_payments.lifecycle import ProviderResult
from local_payments.providers.msisdn import normalize_msisdn

# The sandbox only accepts EUR, whatever the contract's currency.
SANDBOX_CURRENCY = "EUR"
# Seconds shaved off expires_in, so a cached token is never used right at its expiry.
TOKEN_EXPIRY_MARGIN = 60
TEXT_MAX_LENGTH = 160
# Length of the provider_status column on the attempt.
PROVIDER_STATUS_MAX_LENGTH = 140
# Enough for MTN's error code and message. A proxy's HTML error page is cut.
RESPONSE_LOG_MAX_LENGTH = 2000
# Connect, read, in seconds. Just over a multiple of 3 s, TCP's retransmission window, as requests advises.
DEFAULT_TIMEOUT = (6.05, 15)

STATUSES = {"PENDING": lc.PENDING, "SUCCESSFUL": lc.SUCCEEDED, "FAILED": lc.FAILED}

# MTN does not publish which characters it refuses in payerMessage and payeeNote, so only these pass.
REFUSED_TEXT = re.compile(r"[^A-Za-z0-9 .,\-_:/]")


class InvalidRequest(ValueError):
	"""Refused locally, before any call to MTN."""


class ErrorKind(StrEnum):
	UNAUTHORIZED = "unauthorized"
	REJECTED = "rejected"
	UNAVAILABLE = "unavailable"
	UNEXPECTED = "unexpected"


class MtnMomoError(Exception):
	"""A status query that got no usable answer: the attempt's state is still unknown."""

	def __init__(self, kind: ErrorKind, http_status: int | None = None, code: str | None = None):
		self.kind = kind
		self.http_status = http_status
		self.code = code
		detail = ", ".join(str(part) for part in (http_status, code) if part)
		super().__init__(f"MTN MoMo status query {kind}" + (f" ({detail})" if detail else ""))


class InitiationOutcome(StrEnum):
	ACCEPTED = "accepted"  # 202: MTN queued the request
	DUPLICATE_REFERENCE = "duplicate_reference"  # 409: this attempt_id already exists at MTN
	REJECTED = "rejected"  # 4xx: MTN refused it, nothing was created
	UNAUTHORIZED = "unauthorized"  # still 401 after a fresh token
	UNKNOWN = "unknown"  # timeout, 5xx or anything else: the request may exist
	NOT_SENT = "not_sent"  # no token could be obtained, so RequestToPay never left


@dataclass(frozen=True)
class Initiation:
	outcome: InitiationOutcome
	http_status: int | None = None
	code: str | None = None


@dataclass(frozen=True)
class Exchange:
	"""One API call as sent and as answered, for the caller's network log.

	The headers that carry credentials (Authorization, Ocp-Apim-Subscription-Key) are never in it.
	"""

	method: str
	url: str
	request_headers: dict
	request_body: dict | None
	status_code: int | None = None
	response_body: str | None = None
	# The exception's name when no response came back, e.g. "ReadTimeout".
	error: str | None = None


@dataclass(frozen=True)
class MtnMomoConfig:
	api_base_url: str
	target_environment: str
	subscription_key: str = field(repr=False)
	api_user: str
	api_key: str = field(repr=False)
	currency: str
	msisdn_prefix: str
	msisdn_national_length: int
	sandbox: bool = False


class TokenStore(Protocol):
	def get(self, key: str) -> str | None: ...

	def set(self, key: str, token: str, ttl_seconds: int) -> None: ...

	def delete(self, key: str) -> None: ...


class _Unauthorized(Exception):
	pass


class _TokenUnavailable(Exception):
	pass


class _Unreadable(Exception):
	pass


class MtnMomoClient:
	def __init__(
		self,
		config: MtnMomoConfig,
		token_store: TokenStore,
		cache_key: str,
		http: requests.Session | None = None,
		timeout=DEFAULT_TIMEOUT,
		on_exchange: Callable[[Exchange], None] | None = None,
	):
		self.config = config
		self.token_store = token_store
		self.cache_key = cache_key
		self.http = http or requests.Session()
		self.timeout = timeout
		# Told about every API call except the token request, whose answer is a credential.
		self.on_exchange = on_exchange

	def request_to_pay(
		self,
		attempt_id: str,
		amount,
		msisdn: str,
		external_id: str,
		payer_message: str = "",
		payee_note: str = "",
		callback_url: str | None = None,
	) -> Initiation:
		"""Ask MTN to push a payment request to the payer's phone.

		`attempt_id` goes out as X-Reference-Id, so a retry with the same id can never create a second
		request. Raises InvalidRequest, before any call, for a bad number or amount.
		"""
		body = {
			"amount": format_amount(amount),
			"currency": SANDBOX_CURRENCY if self.config.sandbox else self.config.currency,
			"externalId": external_id,
			"payer": {"partyIdType": "MSISDN", "partyId": self.validate_msisdn(msisdn)},
			"payerMessage": clean_text(payer_message),
			"payeeNote": clean_text(payee_note),
		}
		headers = {"X-Reference-Id": attempt_id}
		if callback_url:
			headers["X-Callback-Url"] = callback_url

		try:
			response = self._call("POST", "/collection/v1_0/requesttopay", headers, body)
		except _TokenUnavailable:
			return Initiation(InitiationOutcome.NOT_SENT)
		except _Unauthorized:
			return Initiation(InitiationOutcome.UNAUTHORIZED, 401)
		except requests.RequestException:
			return Initiation(InitiationOutcome.UNKNOWN)

		status, code = response.status_code, error_code(response)
		if status == 202:
			return Initiation(InitiationOutcome.ACCEPTED, status)
		if status == 409:
			return Initiation(InitiationOutcome.DUPLICATE_REFERENCE, status, code)
		if 400 <= status < 500:
			return Initiation(InitiationOutcome.REJECTED, status, code)
		return Initiation(InitiationOutcome.UNKNOWN, status, code)

	def check_status(self, attempt_id: str) -> ProviderResult:
		"""Ask MTN where the request stands. Raises MtnMomoError when MTN gives no usable answer."""
		try:
			response = self._call("GET", f"/collection/v1_0/requesttopay/{quote(attempt_id, safe='')}", {})
		except _Unauthorized:
			raise MtnMomoError(ErrorKind.UNAUTHORIZED, 401)
		except (_TokenUnavailable, requests.RequestException):
			raise MtnMomoError(ErrorKind.UNAVAILABLE)

		status = response.status_code
		if status == 200:
			try:
				return self._status_result(response)
			except _Unreadable as unreadable:
				# Raised here, not in _status_result, so the logged traceback leaves out the payer's data.
				raise MtnMomoError(ErrorKind.UNEXPECTED, status, str(unreadable))
		if status == 404:
			# MTN never created the request.
			return ProviderResult(lc.FAILED, provider_status=describe("404", error_code(response)))
		if status == 400:
			raise MtnMomoError(ErrorKind.REJECTED, status, error_code(response))
		if status >= 500:
			raise MtnMomoError(ErrorKind.UNAVAILABLE, status, error_code(response))
		raise MtnMomoError(ErrorKind.UNEXPECTED, status)

	def validate_msisdn(self, msisdn: str) -> str:
		"""Country code then national number, ASCII digits only."""
		prefix, length = self.config.msisdn_prefix, self.config.msisdn_national_length
		# Already in shape: normalizing gives the same number back.
		if not msisdn or normalize_msisdn(msisdn, prefix, length) != msisdn:
			# The number itself stays out of the message: it is personal data.
			raise InvalidRequest(f"The phone number must be {prefix} followed by {length} digits.")
		return msisdn

	def _status_result(self, response) -> ProviderResult:
		try:
			payload = response.json()
		except ValueError:
			raise _Unreadable("invalid JSON")
		raw_status = payload.get("status") if isinstance(payload, dict) else None
		if not isinstance(raw_status, str) or raw_status not in STATUSES:
			raise _Unreadable(f"status {str(raw_status)[:40]}")

		return ProviderResult(
			STATUSES[raw_status],
			amount=as_text(payload.get("amount")),
			currency=self._incoming_currency(as_text(payload.get("currency"))),
			transaction_id=as_text(payload.get("financialTransactionId")),
			provider_status=describe(raw_status, reason_code(payload.get("reason"))),
		)

	def _incoming_currency(self, currency: str | None) -> str | None:
		# The sandbox answers in EUR for a request the session holds in the contract's currency.
		if self.config.sandbox and currency and currency.upper() == SANDBOX_CURRENCY:
			return self.config.currency
		return currency

	def _call(self, method: str, path: str, headers: dict, body: dict | None = None):
		"""Send one API call, renewing the token and retrying once on a 401."""
		url = self._url(path)
		# Everything sent except the credentials, which is what may be logged.
		loggable = {"X-Target-Environment": self.config.target_environment, **headers}
		for renew in (False, True):
			token = self._token(renew)
			try:
				response = self.http.request(
					method,
					url,
					headers={
						"Authorization": f"Bearer {token}",
						"Ocp-Apim-Subscription-Key": self.config.subscription_key,
						**loggable,
					},
					json=body,
					timeout=self.timeout,
				)
			except requests.RequestException as exc:
				self._report(Exchange(method, url, loggable, body, error=type(exc).__name__))
				raise
			self._report(
				Exchange(
					method,
					url,
					loggable,
					body,
					response.status_code,
					response.text[:RESPONSE_LOG_MAX_LENGTH] or None,
				)
			)
			if response.status_code != 401:
				return response
		raise _Unauthorized

	def _report(self, exchange: Exchange) -> None:
		if self.on_exchange:
			self.on_exchange(exchange)

	def _token(self, renew: bool) -> str:
		if renew:
			self.token_store.delete(self.cache_key)
		elif cached := self.token_store.get(self.cache_key):
			return cached

		# Frappe masks traceback variables whose name contains "secret", so this one never reaches a log.
		basic_auth_secret = base64.b64encode(
			f"{self.config.api_user}:{self.config.api_key}".encode()
		).decode()
		try:
			response = self.http.request(
				"POST",
				self._url("/collection/token/"),
				headers={
					"Authorization": f"Basic {basic_auth_secret}",
					"Ocp-Apim-Subscription-Key": self.config.subscription_key,
				},
				timeout=self.timeout,
			)
		except requests.RequestException:
			raise _TokenUnavailable
		if response.status_code == 401:
			raise _Unauthorized
		if response.status_code != 200:
			raise _TokenUnavailable

		try:
			payload = response.json()
			token, expires_in = payload["access_token"], int(payload.get("expires_in") or 0)
		except (ValueError, TypeError, KeyError):
			raise _TokenUnavailable
		if not token or not isinstance(token, str):
			raise _TokenUnavailable

		if expires_in - TOKEN_EXPIRY_MARGIN > 0:
			self.token_store.set(self.cache_key, token, expires_in - TOKEN_EXPIRY_MARGIN)
		return token

	def _url(self, path: str) -> str:
		return self.config.api_base_url.rstrip("/") + path


def format_amount(amount) -> str:
	"""A whole, positive amount as MTN expects it: a string of digits. Never rounded."""
	try:
		value = Decimal(str(amount))
	except InvalidOperation:
		raise InvalidRequest("The amount is not a number.")
	if not value.is_finite() or value <= 0 or value != value.to_integral_value():
		raise InvalidRequest("MTN MoMo amounts must be whole and positive.")
	return str(int(value))


def clean_text(text: str | None) -> str:
	"""Accents dropped, other refused characters replaced by spaces, at most 160 characters."""
	decomposed = unicodedata.normalize("NFKD", text or "")
	unaccented = "".join(char for char in decomposed if not unicodedata.combining(char))
	return " ".join(REFUSED_TEXT.sub(" ", unaccented).split())[:TEXT_MAX_LENGTH]


def error_code(response) -> str | None:
	"""MTN's error code from an error body ({"code": ...}, or {"error": ...} for the token)."""
	try:
		payload = response.json()
	except ValueError:
		return None
	if not isinstance(payload, dict):
		return None
	return as_text(payload.get("code") or payload.get("error"))


def reason_code(reason) -> str | None:
	"""The reason of a FAILED status. MTN documents it both as a plain code and as {code, message}."""
	if isinstance(reason, dict):
		return as_text(reason.get("code"))
	return as_text(reason)


def describe(status: str, reason: str | None) -> str:
	return (f"{status}: {reason}" if reason else status)[:PROVIDER_STATUS_MAX_LENGTH]


def as_text(value) -> str | None:
	# MTN's examples send some string fields as numbers.
	return None if value is None or value == "" else str(value)
