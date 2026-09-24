# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

import base64
from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.tests import IntegrationTestCase
from payments.utils import get_payment_gateway_controller

from local_payments.local_payments.doctype.mtn_momo_settings.mtn_momo_settings import CacheTokenStore
from local_payments.providers.mtn_momo import MtnMomoError

SETTINGS = "MTN MoMo Settings"
GATEWAY = "MTN MoMo-momo-test"


def http_response(status, body):
	response = MagicMock(status_code=status)
	response.json.return_value = body
	return response


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

	def test_check_status_asks_mtn_with_the_decrypted_credentials(self):
		self.addCleanup(CacheTokenStore().delete, self.settings.token_cache_key)
		replies = [
			http_response(200, {"access_token": "fake-access-token", "expires_in": 3600}),
			http_response(200, {"amount": "5000", "currency": "EUR", "status": "SUCCESSFUL"}),
		]
		with patch("local_payments.providers.mtn_momo.requests.Session") as session:
			session.return_value.request.side_effect = replies
			result = self.settings.check_status("3f5c1c6e-8f4b-4a57-9d9a-3b1f5f0f2a11", {})

		token_headers = session.return_value.request.call_args_list[0].kwargs["headers"]
		basic = base64.b64encode(b"00000000-0000-4000-8000-000000000000:fake-api-key").decode()
		self.assertEqual(token_headers["Authorization"], f"Basic {basic}")
		self.assertEqual(token_headers["Ocp-Apim-Subscription-Key"], "fake-subscription-key")
		# The settings default to Sandbox, whose EUR answer is read back in the contract's currency.
		self.assertEqual((result.status, result.amount, result.currency), ("Succeeded", "5000", "XAF"))

	def test_a_failed_status_query_logs_no_secret_or_payer_number(self):
		self.addCleanup(CacheTokenStore().delete, self.settings.token_cache_key)
		token = http_response(200, {"access_token": "fake-access-token", "expires_in": 3600})
		unknown_status = http_response(200, {"status": "ONGOING", "payer": {"partyId": "237000000001"}})
		# Named so Frappe masks it: this frame is part of the traceback under test.
		basic_auth_secret = base64.b64encode(b"00000000-0000-4000-8000-000000000000:fake-api-key").decode()
		cases = {
			"token timeout": [requests.ConnectTimeout()],
			"status timeout": [token, requests.ReadTimeout()],
			"unknown status": [token, unknown_status],
			"non-text status": [
				token,
				http_response(200, {"status": [], "payer": {"partyId": "237000000001"}}),
			],
		}
		for case, replies in cases.items():
			CacheTokenStore().delete(self.settings.token_cache_key)
			with self.subTest(case), patch("local_payments.providers.mtn_momo.requests.Session") as session:
				session.return_value.request.side_effect = replies
				try:
					self.settings.check_status("3f5c1c6e-8f4b-4a57-9d9a-3b1f5f0f2a11", {})
				except MtnMomoError:
					# What frappe.log_error would store in the Error Log.
					logged = frappe.get_traceback(with_context=True)
				self.assertIn("providers/mtn_momo.py", logged)
				for secret in (
					"fake-api-key",
					"fake-subscription-key",
					"fake-access-token",
					basic_auth_secret,
					"237000000001",
				):
					self.assertNotIn(secret, logged)

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
			"sandbox on a production host": {"api_base_url": "https://api.mtn.example"},
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
