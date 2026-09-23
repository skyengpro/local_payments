# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

TOKEN_LENGTH = 32


class LocalPayment(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from local_payments.local_payments.doctype.local_payment_attempt.local_payment_attempt import (
			LocalPaymentAttempt,
		)

		amount: DF.Currency
		attempts: DF.Table[LocalPaymentAttempt]
		authorization: DF.Literal["", "Pending", "Done", "Failed"]
		authorization_error: DF.SmallText | None
		authorization_tries: DF.Int
		currency: DF.Link
		description: DF.SmallText | None
		paid_amount: DF.Currency
		paid_on: DF.Datetime | None
		payer_email: DF.Data | None
		payer_name: DF.Data | None
		payment_gateway: DF.Link
		provider_transaction_id: DF.Data | None
		redirect_to: DF.SmallText | None
		reference_docname: DF.DynamicLink
		reference_doctype: DF.Link
		request_data: DF.Code | None
		status: DF.Literal["Open", "Paid", "Void"]
		success_redirect: DF.SmallText | None
		title: DF.Data | None
		token: DF.Data | None
	# end: auto-generated types

	def before_insert(self):
		# Always overwritten: the token is the only guest access key, so a caller can neither
		# choose it nor derive it from the (sequential) name.
		self.token = frappe.generate_hash(length=TOKEN_LENGTH)

	def validate(self):
		if flt(self.amount) <= 0:
			frappe.throw(_("Amount must be strictly positive."))
