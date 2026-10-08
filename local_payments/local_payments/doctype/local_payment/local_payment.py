# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model import display_fieldtypes, table_fields
from frappe.model.document import Document
from frappe.utils import flt

from local_payments import reconcile as rc

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
		authorization: DF.Literal["Pending", "Done", "Failed"]
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
		self.validate_state_unchanged()

	def validate_state_unchanged(self):
		"""Only reconcile.save_state() creates a session or writes its state.

		The state is every field not fixed at creation (set_only_once). Any other save must leave it as it is.
		"""
		if self.flags.local_payments_state:
			return
		if self.is_new():
			frappe.throw(_("Sessions are created by the payment flow only."), exc=frappe.PermissionError)
		before = self.get_doc_before_save()
		for df in self.meta.fields:
			if df.set_only_once or df.get("is_custom_field") or df.fieldtype in display_fieldtypes:
				continue
			if df.fieldtype in table_fields:
				changed = not self.is_child_table_same(df.fieldname)
			else:
				# None, 0 and "" all mean unset: a value not loaded yet reads as None, the database holds 0.
				changed = self.has_value_changed(df.fieldname) and bool(
					before.get(df.fieldname) or self.get(df.fieldname)
				)
			if changed:
				frappe.throw(
					_("{0} is set by the payment flow and cannot be edited.").format(_(df.label)),
					exc=frappe.CannotChangeConstantError,
				)

	def onload(self):
		self.set_onload("can_retry_authorization", rc.can_retry_authorization(self))


# Takes the name: as a document method, Frappe would lock the row and wait before running it.
@frappe.whitelist(methods=["POST"])
def retry_authorization(name: str) -> str:
	"""Run the consumer's callback again for one session and return the new authorization state."""
	frappe.only_for([rc.MANAGER_ROLE, "System Manager"])
	frappe.has_permission("Local Payment", "read", name, throw=True)
	# Refuse at once if an authorization is already running on this row.
	try:
		frappe.db.get_value("Local Payment", name, "name", for_update=True, wait=False)
	except frappe.QueryTimeoutError:
		frappe.throw(_("A payment for this request is being processed. Try again in a moment."))
	rc.authorize(name)
	return frappe.db.get_value("Local Payment", name, "authorization")
