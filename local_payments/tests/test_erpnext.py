# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

from unittest import SkipTest
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from local_payments import erpnext as ep
from local_payments import reconcile as rc

GATEWAY_NAME = "lp-erpnext"
GATEWAY = f"MTN MoMo-{GATEWAY_NAME}"
COMPANY = "_Test Company"
BANK = "_Test Bank - _TC"


class TestAsAdministrator(IntegrationTestCase):
	def test_puts_the_caller_back_after_the_block(self):
		frappe.set_user("Guest")
		frappe.local.form_dict = frappe._dict(token="t")
		session = frappe.local.session
		try:
			with ep.as_administrator():
				self.assertEqual(frappe.session.user, "Administrator")
			self.assertIs(frappe.local.session, session)
			self.assertEqual((frappe.session.user, frappe.session.sid), ("Guest", "Guest"))
			self.assertEqual(frappe.local.form_dict, {"token": "t"})
		finally:
			frappe.set_user("Administrator")

	def test_puts_the_caller_back_when_the_block_raises(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.ValidationError), ep.as_administrator():
				frappe.throw("refused")
			self.assertEqual(frappe.session.user, "Guest")
		finally:
			frappe.set_user("Administrator")


class TestSettlePaymentRequest(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if "erpnext" not in frappe.get_installed_apps():
			raise SkipTest("ERPNext is not installed")
		# Importing ERPNext's test helpers creates its test company, customers and items.
		import erpnext.selling.doctype.sales_order.test_sales_order

		from local_payments.local_payments.doctype.mtn_momo_settings.test_mtn_momo_settings import (
			make_settings,
		)

		if not frappe.db.exists("Currency", "XAF"):
			frappe.get_doc({"doctype": "Currency", "currency_name": "XAF", "enabled": 1}).insert()
		if not frappe.db.exists("MTN MoMo Settings", GATEWAY_NAME):
			# Disabled, so ERPNext doesn't create a gateway account for the default company on save.
			make_settings(gateway_name=GATEWAY_NAME, enabled=0)
		account = {"payment_gateway": GATEWAY, "currency": "INR", "company": COMPANY}
		cls.gateway_account = frappe.db.get_value("Payment Gateway Account", account) or (
			frappe.get_doc({"doctype": "Payment Gateway Account", "payment_account": BANK, **account})
			.insert(ignore_permissions=True)
			.name
		)
		frappe.db.set_value(
			"Company",
			COMPANY,
			{
				"exchange_gain_loss_account": "_Test Exchange Gain/Loss - _TC",
				"write_off_account": "_Test Write Off - _TC",
				"cost_center": "_Test Cost Center - _TC",
			},
		)
		frappe.db.commit()

	def setUp(self):
		self.addCleanup(self.clean_up)

	def clean_up(self):
		# authorize() commits, so the class rollback can't undo the session.
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.db.delete("Local Payment Attempt", {"parenttype": "Local Payment"})
		frappe.db.delete("Local Payment", {"payment_gateway": GATEWAY})
		frappe.db.commit()

	def paid_session(self, status="Paid", authorization="Pending"):
		from erpnext.accounts.doctype.payment_request.payment_request import make_payment_request
		from erpnext.selling.doctype.sales_order.test_sales_order import make_sales_order

		with (
			patch(
				"erpnext.accounts.doctype.payment_request.payment_request.PaymentRequest.get_payment_url",
				return_value="https://example.com/pay",
			),
			patch("erpnext.accounts.doctype.payment_request.payment_request.PaymentRequest.send_email"),
		):
			order = make_sales_order(currency="INR")
			request = make_payment_request(
				dt="Sales Order",
				dn=order.name,
				mute_email=1,
				payment_gateway_account=self.gateway_account,
				submit_doc=1,
				return_doc=1,
			)
		session = frappe.get_doc(
			{
				"doctype": "Local Payment",
				"payment_gateway": GATEWAY,
				"reference_doctype": "Payment Request",
				"reference_docname": request.name,
				"amount": request.grand_total,
				"currency": request.currency,
				"status": status,
				"authorization": authorization,
				"attempts": [
					{"attempt_id": frappe.generate_hash(length=36), "status": "Succeeded"},
				]
				if status == "Paid"
				else [],
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		return session, request

	def assert_settled(self, session, request):
		session = frappe.get_doc("Local Payment", session.name)
		self.assertEqual(session.authorization, "Done", session.authorization_error)
		self.assertEqual(frappe.db.get_value("Payment Request", request.name, "status"), "Paid")
		entries = frappe.get_all("Payment Entry", {"reference_no": request.name, "docstatus": 1}, ["paid_to"])
		self.assertEqual([e.paid_to for e in entries], [BANK])

	def test_settles_as_administrator(self):
		session, request = self.paid_session()
		rc.authorize(session.name)
		self.assert_settled(session, request)

	def test_settles_as_guest(self):
		session, request = self.paid_session()
		frappe.set_user("Guest")
		rc.authorize(session.name)
		self.assertEqual(frappe.session.user, "Guest")
		frappe.set_user("Administrator")
		self.assert_settled(session, request)

	def test_cancelling_the_request_voids_its_open_sessions(self):
		session, request = self.paid_session(status="Open", authorization="")
		request.reload().cancel()
		self.assertEqual(frappe.db.get_value("Local Payment", session.name, "status"), "Void")

	def test_cancel_is_refused_while_a_payment_is_not_booked(self):
		session, request = self.paid_session(authorization="Failed")
		with self.assertRaisesRegex(frappe.ValidationError, session.name):
			request.reload().cancel()
		# What the request handler does with the error: the whole cancel is undone.
		frappe.db.rollback()
		self.assertEqual(frappe.db.get_value("Payment Request", request.name, "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Local Payment", session.name, "status"), "Paid")
