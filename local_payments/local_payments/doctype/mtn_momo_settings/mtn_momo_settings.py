# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

import re
from urllib.parse import urlsplit

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from local_payments.gateway import LocalPaymentGateway


class MTNMoMoSettings(LocalPaymentGateway, Document):
	"""One MTN MoMo Collection merchant contract.

	gateway_name is set only once: the payment gateway is created under that name and is never renamed.
	"""

	provider_name = "MTN MoMo"

	def validate(self):
		self.validate_api_base_url()
		self.validate_msisdn_format()
		if cint(self.pending_timeout_minutes) <= 0:
			frappe.throw(_("Pending Timeout (Minutes) must be greater than zero."))

	def validate_api_base_url(self):
		# validate() runs before Frappe's mandatory check, which reports a missing value itself.
		if not self.api_base_url:
			return
		self.api_base_url = self.api_base_url.strip()
		# The API user and key go out in a Basic auth header on every token request.
		url = urlsplit(self.api_base_url)
		if url.scheme != "https" or not url.hostname:
			frappe.throw(_("API Base URL must be an https:// address."))

	def validate_msisdn_format(self):
		if self.msisdn_prefix and not re.fullmatch(r"[0-9]+", self.msisdn_prefix):
			frappe.throw(_("MSISDN Prefix must contain digits only, without + or 00."))
		if cint(self.msisdn_national_length) <= 0:
			frappe.throw(_("MSISDN National Length must be greater than zero."))
