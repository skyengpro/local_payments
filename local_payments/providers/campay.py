# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

"""CamPay client: collect, transaction status and webhook signature.

No frappe import. The caller passes the application's token, and turns an `Initiation` into an attempt
status itself.
"""

import base64
import binascii
import hashlib
import hmac
import json
import re
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

BASE_URLS = {"Sandbox": "https://demo.campay.net/api", "Production": "https://www.campay.net/api"}
COLLECT_PATH = "/collect/"
STATUS_PATH = "/transaction/{reference}/"

# The offer covers Cameroon only.
CURRENCY = "XAF"
MSISDN_PREFIX = "237"
MSISDN_NATIONAL_LENGTH = 9

STATUSES = {
	"PENDING": lc.PENDING,
	"SUCCESSFUL": lc.SUCCEEDED,
	"FAILED": lc.FAILED,
}

# Refusals of the number or the amount: nothing was created at CamPay.
REJECTED_CODES = frozenset({"ER101", "ER102", "ER201"})
# A transaction with our external_reference may already exist, so this is no refusal.
DUPLICATE_ATTEMPT = "DUPLICATE_ATTEMPT"

# CamPay leaves "None", as a string, in `reason` until a transaction fails.
NO_REASON = "None"

# The reference goes into the status URL's path, so a slash or a dot in it must never get there.
REFERENCE_PATTERN = re.compile(r"\A[0-9A-Za-z-]{1,64}\Z")

# Clock drift allowed on the webhook token's nbf and exp, in seconds.
JWT_LEEWAY = 60


class CamPayError(Exception):
	"""A status query that got no usable answer: the attempt's state is still unknown."""

	def __init__(self, http_status: int | None = None, code: str | None = None):
		self.http_status = http_status
		self.code = code
		detail = ", ".join(str(part) for part in (http_status, code) if part)
		super().__init__("CamPay status query failed" + (f" ({detail})" if detail else ""))

	@property
	def refused(self) -> bool:
		"""True when CamPay turned our token down. Not an outage, so no retry will fix it."""
		return self.http_status in (401, 403)


class InitiationOutcome(StrEnum):
	ACCEPTED = "accepted"  # 200 with a reference
	REJECTED = "rejected"  # the number, the amount or the token was refused: nothing was created
	UNKNOWN = "unknown"  # timeout, 5xx, 429 or anything else: the transaction may exist


@dataclass(frozen=True)
class Initiation:
	outcome: InitiationOutcome
	reference: str | None = None
	http_status: int | None = None
	code: str | None = None

	@property
	def credentials_refused(self) -> bool:
		"""A refusal no payer can cause: the token or the application needs fixing."""
		return self.outcome == InitiationOutcome.REJECTED and self.http_status in (401, 403)


@dataclass(frozen=True)
class CamPayConfig:
	api_base_url: str
	access_token: str = field(repr=False)


class _Unreadable(Exception):
	pass


class CamPayClient:
	def __init__(
		self,
		config: CamPayConfig,
		http: requests.Session | None = None,
		on_exchange: Callable[[Exchange], None] | None = None,
	):
		self.config = config
		self.http = http or requests.Session()
		# Told about the collect, never about a status query.
		self.on_exchange = on_exchange

	def collect(self, attempt_id: str, amount, msisdn: str, description: str) -> Initiation:
		"""Ask CamPay to push a payment request to the payer's phone, once.

		The answer only gives CamPay's reference: a status check decides the outcome. Raises InvalidRequest,
		before any call, for a bad number or amount.
		"""
		body = {
			"amount": str(whole_amount(amount)),
			"currency": CURRENCY,
			"from": payer_number(msisdn),
			"description": description,
			"external_reference": attempt_id,
		}
		url = self._url(COLLECT_PATH)
		try:
			response = self.http.request(
				"POST",
				url,
				headers=self._headers(),
				json=body,
				timeout=DEFAULT_TIMEOUT,
				# A redirect would send the token to whatever address it names.
				allow_redirects=False,
			)
		except requests.RequestException as exc:
			self._report(Exchange("POST", url, {}, body, error=type(exc).__name__))
			return Initiation(InitiationOutcome.UNKNOWN)

		text = (
			response.text.replace(self.config.access_token, "***")
			if self.config.access_token
			else response.text
		)
		self._report(
			Exchange("POST", url, {}, body, response.status_code, text[:RESPONSE_LOG_MAX_LENGTH] or None)
		)
		return read_collect(response)

	def check_status(self, reference: str, attempt_id: str) -> ProviderResult:
		"""Ask CamPay where a transaction stands. Raises CamPayError when it gives no usable answer."""
		if not isinstance(reference, str) or not REFERENCE_PATTERN.match(reference):
			raise CamPayError(code="reference not usable")
		try:
			response = self.http.request(
				"GET",
				self._url(STATUS_PATH.format(reference=reference)),
				headers=self._headers(),
				timeout=DEFAULT_TIMEOUT,
				allow_redirects=False,
			)
		except requests.RequestException:
			# `from None` keeps the transport error, and the request it holds, out of the logged traceback.
			raise CamPayError() from None

		try:
			return read_status(response, reference, attempt_id)
		except _Unreadable as unreadable:
			# Raised here, not in read_status, so the logged traceback leaves out the payer's data.
			raise CamPayError(response.status_code, str(unreadable) or None) from None

	def _headers(self) -> dict:
		# Built inline at each call, so no frame of collect or check_status holds the token in a local a traceback could show.
		return {"Authorization": f"Token {self.config.access_token}"}

	def _report(self, exchange: Exchange) -> None:
		if self.on_exchange:
			self.on_exchange(exchange)

	def _url(self, path: str) -> str:
		return self.config.api_base_url.rstrip("/") + path


def payer_number(msisdn: str) -> str:
	"""The number as CamPay expects it: 237 then the 9-digit national number."""
	normalized = normalize_msisdn(msisdn, MSISDN_PREFIX, MSISDN_NATIONAL_LENGTH)
	if not normalized:
		# The number itself stays out of the message: it is personal data.
		raise InvalidRequest(f"The phone number must have {MSISDN_NATIONAL_LENGTH} digits.")
	return normalized


def read_collect(response) -> Initiation:
	body = json_body(response)
	status = response.status_code
	code = as_text(body.get("error_code"))

	if status in (401, 403):
		return Initiation(InitiationOutcome.REJECTED, http_status=status, code=code)
	if status == 400 and code in REJECTED_CODES:
		return Initiation(InitiationOutcome.REJECTED, http_status=status, code=code)

	reference = body.get("reference")
	if status == 200 and isinstance(reference, str) and REFERENCE_PATTERN.match(reference):
		return Initiation(InitiationOutcome.ACCEPTED, reference, status)
	return Initiation(InitiationOutcome.UNKNOWN, http_status=status, code=code)


def read_status(response, reference: str, attempt_id: str) -> ProviderResult:
	"""Raises _Unreadable unless this is a 200 about this very attempt, carrying a status we know."""
	if response.status_code != 200:
		raise _Unreadable()
	body = json_body(response)

	# An answer about another transaction proves nothing about ours.
	for name, expected in (("reference", reference), ("external_reference", attempt_id)):
		if body.get(name) != expected:
			raise _Unreadable(f"{name} mismatch")

	raw_status = body.get("status")
	if not isinstance(raw_status, str) or raw_status not in STATUSES:
		raise _Unreadable(f"status {str(raw_status)[:40]}")

	reason = as_text(body.get("reason"))
	return ProviderResult(
		STATUSES[raw_status],
		amount=as_text(body.get("amount")),
		currency=as_text(body.get("currency")),
		transaction_id=reference,
		provider_status=describe(raw_status, None if reason == NO_REASON else reason),
	)


def webhook_signature_valid(token, webhook_key: str, now: float) -> bool:
	"""True for an HS256 JWT signed with `webhook_key` and valid at `now`, in Unix seconds.

	It only proves the call comes from CamPay: the token names no transaction. Never raises.
	"""
	if not (isinstance(token, str) and isinstance(webhook_key, str) and webhook_key):
		return False
	parts = token.split(".")
	if len(parts) != 3:
		return False
	header_part, payload_part, signature_part = parts

	header, payload, signature = _b64_json(header_part), _b64_json(payload_part), _b64(signature_part)
	# The algorithm is fixed here: a token naming another one, "none" above all, is never accepted.
	if not isinstance(header, dict) or header.get("alg") != "HS256" or signature is None:
		return False

	signed = f"{header_part}.{payload_part}".encode("ascii")
	expected = hmac.new(webhook_key.encode(), signed, hashlib.sha256).digest()
	if not hmac.compare_digest(expected, signature) or not isinstance(payload, dict):
		return False

	not_before, expires = payload.get("nbf"), payload.get("exp")
	if not all(isinstance(t, int | float) and not isinstance(t, bool) for t in (not_before, expires)):
		return False
	return not_before - JWT_LEEWAY <= now <= expires + JWT_LEEWAY


def _b64(part: str) -> bytes | None:
	"""Decodes one base64url part of a token, padding or not. None for anything that isn't one."""
	if not part or not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", part):
		return None
	try:
		return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
	except (binascii.Error, ValueError):
		return None


def _b64_json(part: str):
	raw = _b64(part)
	if raw is None:
		return None
	try:
		return json.loads(raw)
	except (UnicodeDecodeError, ValueError):
		return None
