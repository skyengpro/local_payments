# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase
from payments.utils import get_payment_gateway_controller

SETTINGS = "MTN MoMo Settings"
GATEWAY = "MTN MoMo-momo-test"


def make_settings(**overrides):
	values = {
		"doctype": SETTINGS,
		"gateway_name": "momo-test",
		"enabled": 1,
		"currency": "XAF",
		"api_user": "00000000-0000-4000-8000-000000000000",
		"api_key": "fake-api-key",
		"subscription_key": "fake-subscription-key",
		"msisdn_prefix": "237",
		"msisdn_national_length": 9,
		**overrides,
	}
	if frappe.db.exists(SETTINGS, values["gateway_name"]):
		frappe.delete_doc(SETTINGS, values["gateway_name"], force=True)
	return frappe.get_doc(values).insert()


class TestMTNMoMoSettings(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("Currency", "XAF"):
			frappe.get_doc({"doctype": "Currency", "currency_name": "XAF", "enabled": 1}).insert()

	def setUp(self):
		if frappe.db.exists("Payment Gateway", GATEWAY):
			frappe.delete_doc("Payment Gateway", GATEWAY, force=True)
		self.settings = make_settings()

	def test_saving_registers_a_gateway_that_resolves_back_to_these_settings(self):
		gateway = frappe.get_doc("Payment Gateway", GATEWAY)
		self.assertEqual((gateway.gateway_settings, gateway.gateway_controller), (SETTINGS, "momo-test"))

		controller = get_payment_gateway_controller(GATEWAY)
		self.assertEqual((controller.doctype, controller.name), (SETTINGS, "momo-test"))

	def test_gateway_name_cannot_change_after_insert(self):
		self.settings.gateway_name = "momo-renamed"
		with self.assertRaises(frappe.CannotChangeConstantError):
			self.settings.save()
		self.assertFalse(frappe.db.exists("Payment Gateway", "MTN MoMo-momo-renamed"))

	def test_credentials_are_passwords_readable_by_system_manager_only(self):
		meta = frappe.get_meta(SETTINGS)
		for fieldname in ("subscription_key", "api_key"):
			self.assertEqual(meta.get_field(fieldname).fieldtype, "Password", fieldname)
		self.assertEqual({perm.role for perm in meta.permissions}, {"System Manager"})

	def test_empty_pending_timeout_falls_back_to_the_default(self):
		self.settings.pending_timeout_minutes = None
		self.settings.save()
		self.assertEqual(self.settings.reload().pending_timeout_minutes, 15)

	def test_invalid_configuration_is_rejected_on_save(self):
		cases = {
			"plain http": {"api_base_url": "http://sandbox.momodeveloper.mtn.com"},
			"no host": {"api_base_url": "https://"},
			"prefix with plus": {"msisdn_prefix": "+237"},
			"prefix with letters": {"msisdn_prefix": "23a"},
			"zero national length": {"msisdn_national_length": 0},
			"negative timeout": {"pending_timeout_minutes": -1},
		}
		for case, values in cases.items():
			with self.subTest(case), self.assertRaises(frappe.ValidationError):
				self.settings.update(values)
				self.settings.save()
			self.settings.reload()
