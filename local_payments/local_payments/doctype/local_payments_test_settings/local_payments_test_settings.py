# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from local_payments.gateway import LocalPaymentGateway


class LocalPaymentsTestSettings(LocalPaymentGateway, Document):
	"""Stand-in Settings doctype so gateway.py can be tested without a real provider."""

	provider_name = "Local Payments Test"
