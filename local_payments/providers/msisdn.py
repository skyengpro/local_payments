# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

"""Payer phone numbers, shared by every provider. No frappe import."""

import re

# Cameroon: the start of the national number says which network it belongs to.
CAMEROON_PREFIX = "237"
MTN = "MTN"
ORANGE = "ORANGE"
NETWORK_PREFIXES = {
	MTN: ("67", "650", "651", "652", "653", "654", "681"),
	ORANGE: ("69", "655", "656", "657", "658", "659"),
}

# What a payer may type between the digits of their number.
SEPARATORS = re.compile(r"[\s.()-]")


def normalize_msisdn(number, prefix: str, national_length: int) -> str | None:
	"""Country code then national number, digits only, or None if `number` can't be read as one.

	Accepts separators, a leading + or 00, and the national number alone.
	"""
	msisdn = SEPARATORS.sub("", number if isinstance(number, str) else "")
	if msisdn.startswith("+"):
		msisdn = msisdn[1:]
	elif msisdn.startswith("00"):
		msisdn = msisdn[2:]
	if len(msisdn) == national_length:
		msisdn = prefix + msisdn
	if not re.fullmatch(rf"{re.escape(prefix)}[0-9]{{{national_length}}}", msisdn):
		return None
	return msisdn


def is_on_network(msisdn: str, country_prefix: str, network: str) -> bool:
	"""True when this Cameroon number (country code first) starts like one of `network`'s numbers.

	A number that starts like no network's, Camtel's for instance, is on none of them.
	"""
	national = msisdn[len(country_prefix) :] if msisdn.startswith(country_prefix) else msisdn
	return national.startswith(NETWORK_PREFIXES[network])
