# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

SETTINGS = "Local Payments Test Settings"
GATEWAY = "Local Payments Test-lp-test"

REQUEST = {
	"amount": 5000,
	"currency": "XAF",
	"reference_doctype": "User",
	"reference_docname": "Administrator",
	"title": "Invoice 1",
	"order_id": "ORD-1",
}


def make_settings(**overrides):
	values = {"doctype": SETTINGS, "gateway_name": "lp-test", "currency": "XAF", "enabled": 1, **overrides}
	if frappe.db.exists(SETTINGS, values["gateway_name"]):
		frappe.delete_doc(SETTINGS, values["gateway_name"], force=True)
	return frappe.get_doc(values).insert()


def sessions():
	return frappe.get_all("Local Payment", filters={"payment_gateway": GATEWAY}, pluck="name")


class TestLocalPaymentGateway(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		for code in ("XAF", "USD"):
			if not frappe.db.exists("Currency", code):
				frappe.get_doc({"doctype": "Currency", "currency_name": code, "enabled": 1}).insert()

	def setUp(self):
		# Tests share one transaction per class, so start each one without leftover sessions.
		for name in sessions():
			frappe.delete_doc("Local Payment", name, force=True)
		self.settings = make_settings()

	def test_save_creates_gateway_linked_to_its_settings_and_announces_it(self):
		frappe.delete_doc("Payment Gateway", GATEWAY, force=True)
		with patch("local_payments.gateway.call_hook_method") as hook:
			self.settings.save()
		gateway = frappe.get_doc("Payment Gateway", GATEWAY)
		self.assertEqual((gateway.gateway_settings, gateway.gateway_controller), (SETTINGS, "lp-test"))
		hook.assert_called_once_with("payment_gateway_enabled", gateway=GATEWAY)

	def test_resaving_does_not_duplicate_the_gateway(self):
		self.settings.save()
		self.assertEqual(frappe.db.count("Payment Gateway", {"name": GATEWAY}), 1)

	def test_disabled_settings_do_not_announce_the_gateway(self):
		self.settings.enabled = 0
		with patch("local_payments.gateway.call_hook_method") as hook:
			self.settings.save()
		hook.assert_not_called()

	def test_currency_must_match_settings(self):
		self.settings.validate_transaction_currency("XAF")
		with self.assertRaises(frappe.ValidationError):
			self.settings.validate_transaction_currency("USD")

	def test_get_payment_url_creates_open_session(self):
		url = self.settings.get_payment_url(**REQUEST)
		(name,) = sessions()
		session = frappe.get_doc("Local Payment", name)
		self.assertTrue(url.endswith(f"/local_payment_checkout?token={session.token}"))
		self.assertEqual((session.status, session.amount, session.currency), ("Open", 5000, "XAF"))
		self.assertEqual(frappe.parse_json(session.request_data).order_id, "ORD-1")
		self.assertFalse(session.attempts)

	def test_open_session_is_reused_only_for_the_same_request(self):
		url = self.settings.get_payment_url(**REQUEST)
		self.assertEqual(self.settings.get_payment_url(**REQUEST), url)
		self.assertEqual(len(sessions()), 1)

		self.settings.get_payment_url(**{**REQUEST, "amount": 6000})
		self.settings.get_payment_url(**{**REQUEST, "reference_docname": "Guest"})
		self.assertEqual(len(sessions()), 3)

	def test_paid_or_void_session_is_not_reused(self):
		url = self.settings.get_payment_url(**REQUEST)
		frappe.db.set_value("Local Payment", sessions()[0], "status", "Void")
		self.assertNotEqual(self.settings.get_payment_url(**REQUEST), url)

	def test_disabled_gateway_refuses_to_create_a_session(self):
		self.settings.enabled = 0
		self.settings.save()
		with self.assertRaises(frappe.ValidationError):
			self.settings.get_payment_url(**REQUEST)
		self.assertFalse(sessions())

	def test_fractional_amount_rejected_for_currency_without_minor_unit(self):
		with self.assertRaises(frappe.ValidationError):
			self.settings.get_payment_url(**{**REQUEST, "amount": 5000.5})
		self.assertFalse(sessions())
		self.settings.get_payment_url(**{**REQUEST, "amount": 5000.0})

	def test_fractional_amount_accepted_for_currency_with_minor_unit(self):
		settings = make_settings(currency="USD")
		settings.get_payment_url(**{**REQUEST, "currency": "USD", "amount": 10.5})
		self.assertEqual(frappe.db.get_value("Local Payment", sessions()[0], "amount"), 10.5)

	def test_non_finite_amount_is_a_validation_error(self):
		for amount in ("nan", "inf", 0, -5):
			with self.subTest(amount=amount), self.assertRaises(frappe.ValidationError):
				self.settings.get_payment_url(**{**REQUEST, "amount": amount})
		self.assertFalse(sessions())
