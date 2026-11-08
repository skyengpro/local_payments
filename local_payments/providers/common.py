# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

"""What every provider client shares. No frappe import."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

# Length of the provider_status column on the attempt.
PROVIDER_STATUS_MAX_LENGTH = 140
# Enough for a provider's error code and message. A proxy's HTML error page is cut.
RESPONSE_LOG_MAX_LENGTH = 2000
# Connect, read, in seconds. Just over a multiple of 3 s, TCP's retransmission window, as requests advises.
DEFAULT_TIMEOUT = (6.05, 15)


class InvalidRequest(ValueError):
	"""Refused locally, before any call to the provider."""


@dataclass(frozen=True)
class Exchange:
	"""One API call as sent and as answered, for the caller's network log.

	Whatever carries credentials is never in it.
	"""

	method: str
	url: str
	request_headers: dict
	request_body: dict | None
	status_code: int | None = None
	response_body: str | None = None
	# The exception's name when no response came back, e.g. "ReadTimeout".
	error: str | None = None


def whole_amount(amount) -> int:
	"""A whole, positive amount. Never rounded."""
	try:
		value = Decimal(str(amount))
	except InvalidOperation:
		raise InvalidRequest("The amount is not a number.")
	if not value.is_finite() or value <= 0 or value != value.to_integral_value():
		raise InvalidRequest("The amount must be whole and positive.")
	return int(value)


def json_body(response) -> dict:
	"""The answer's JSON object, or an empty one if it isn't one."""
	try:
		body = response.json()
	except ValueError:
		return {}
	return body if isinstance(body, dict) else {}


def describe(status: str, reason: str | None) -> str:
	return (f"{status}: {reason}" if reason else status)[:PROVIDER_STATUS_MAX_LENGTH]


def as_text(value) -> str | None:
	# Providers' examples send some string fields as numbers.
	return None if value is None or value == "" else str(value)
