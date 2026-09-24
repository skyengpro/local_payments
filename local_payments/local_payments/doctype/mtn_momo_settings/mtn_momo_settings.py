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
