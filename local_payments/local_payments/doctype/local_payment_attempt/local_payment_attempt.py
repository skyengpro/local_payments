# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class LocalPaymentAttempt(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		amount_mismatch: DF.Check
		attempt_id: DF.Data | None
		check_count: DF.Int
		confirmed_amount: DF.Currency
		confirmed_currency: DF.Data | None
		duplicate: DF.Check
		expires_on: DF.Datetime | None
		integration_request: DF.Data | None
		last_checked_on: DF.Datetime | None
		next_check_on: DF.Datetime | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		payer_msisdn: DF.Data | None
		provider_data: DF.Code | None
		provider_status: DF.Data | None
		provider_transaction_id: DF.Data | None
		started_on: DF.Datetime | None
		status: DF.Literal["Initiated", "Pending", "Succeeded", "Failed", "Expired", "Unresolved", "Error"]
	# end: auto-generated types

	pass
