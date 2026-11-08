# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

import json
from pathlib import Path
from unittest.mock import patch

import frappe
import requests
from frappe.tests import IntegrationTestCase

import local_payments
from local_payments import lifecycle as lc
from local_payments import reconcile as rc
from local_payments.local_payments.doctype.mtn_momo_settings.test_mtn_momo_settings import http_response
from local_payments.providers.nokash import MTN_MOMO, ORANGE_MONEY, NokashError
from local_payments.tests.test_reconcile import make_session, reload

SETTINGS = "Nokash Settings"
GATEWAY = "Nokash-nokash-test"
HTTP = "local_payments.providers.nokash.requests.Session"
ATTEMPT_ID = "3f5c1c6e-8f4b-4a57-9d9a-3b1f5f0f2a11"
MSISDN = "237000000001"
NATIONAL = "000000001"
TRANSACTION_ID = "nk_tx_xm5ojhd01lkv06c0bjvu"
I_SPACE_KEY = "fake-i-key"
APP_SPACE_KEY = "fake-app-key"

RECORDED = json.loads(
	(Path(local_payments.__file__).parent / "tests" / "fixtures" / "nokash.json").read_text()
)


def recorded(name):
	return http_response(RECORDED[name]["status"], RECORDED[name]["body"])


def make_settings(**overrides):
	values = {
		"doctype": SETTINGS,
		"gateway_name": "nokash-test",
		"enabled": 1,
		"i_space_key": I_SPACE_KEY,
		"app_space_key": APP_SPACE_KEY,
		**overrides,
	}
	if frappe.db.exists(SETTINGS, values["gateway_name"]):
		frappe.delete_doc(SETTINGS, values["gateway_name"], force=True)
	return frappe.get_doc(values).insert()


class TestNokashSettings(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("Currency", "XAF"):
			frappe.get_doc({"doctype": "Currency", "currency_name": "XAF", "enabled": 1}).insert()

	def setUp(self):
		if frappe.db.exists("Payment Gateway", GATEWAY):
			frappe.delete_doc("Payment Gateway", GATEWAY, force=True)
		self.settings = make_settings()
		self.addCleanup(self.clean_up)

	def clean_up(self):
		# reconcile() commits, so the class-level rollback can't undo what these tests created.
		frappe.db.rollback()
		sessions = frappe.get_all("Local Payment", {"payment_gateway": GATEWAY}, pluck="name")
		frappe.db.delete("Version", {"ref_doctype": "Local Payment", "docname": ["in", sessions]})
		frappe.db.delete("Integration Request", {"reference_docname": ["in", sessions]})
		frappe.db.delete("Local Payment Attempt", {"parent": ["in", sessions]})
		frappe.db.delete("Local Payment", {"payment_gateway": GATEWAY})
		frappe.db.delete("Error Log", {"method": "Nokash payment request refused"})
		frappe.db.commit()

	def session(self, *attempts, **extra):
		return make_session(*attempts, payment_gateway=GATEWAY, amount=1000, **extra)

	def initiate(self, answer, session=None, operator=MTN_MOMO):
		session = session or self.session()
		with patch(HTTP) as http:
			if isinstance(answer, Exception):
				http.return_value.request.side_effect = answer
			else:
				http.return_value.request.return_value = answer
			started = self.settings.initiate(ATTEMPT_ID, session, MSISDN, {"operator": operator})
		return started, http.return_value.request

	def test_the_gateway_only_takes_xaf_and_cameroon_numbers_without_any_field(self):
		self.assertEqual(self.settings.payer_msisdn("600000001"), "237600000001")
		self.settings.validate_transaction_currency("XAF")
		with self.assertRaises(frappe.ValidationError):
			self.settings.validate_transaction_currency("EUR")

	def test_a_gateway_needs_an_operator(self):
		with self.assertRaises(frappe.ValidationError):
			make_settings(gateway_name="nokash-refused", accept_mtn_momo=0, accept_orange_money=0)

	def test_the_payer_chooses_only_between_the_operators_offered(self):
		both = {"accept_mtn_momo": 1, "accept_orange_money": 1}
		orange_only = {"accept_mtn_momo": 0, "accept_orange_money": 1}
		cases = {
			"both, one chosen": (both, MTN_MOMO, MTN_MOMO),
			"both, none chosen": (both, None, None),
			"both, unknown": (both, "AIRTEL", None),
			"one, nothing to choose": (orange_only, None, ORANGE_MONEY),
			"one, sent empty by the page": (orange_only, "", ORANGE_MONEY),
			"one, the other chosen": (orange_only, MTN_MOMO, None),
		}
		for case, (offered, operator, kept) in cases.items():
			with self.subTest(case):
				self.settings.update(offered)
				if kept:
					self.assertEqual(self.settings.attempt_data(operator), {"operator": kept})
				else:
					with self.assertRaises(frappe.ValidationError):
						self.settings.attempt_data(operator)

		self.settings.update(both)
		self.assertEqual([op["value"] for op in self.settings.payer_operators()], [MTN_MOMO, ORANGE_MONEY])
		self.settings.update(orange_only)
		self.assertEqual(self.settings.payer_operators(), [])

	def test_initiation_outcomes(self):
		accepted_id = RECORDED["payin_accepted"]["body"]["data"]["id"]
		cases = {
			"accepted": (recorded("payin_accepted"), lc.INITIATED, accepted_id, "Completed"),
			"refused": (recorded("payin_below_minimum"), lc.ERROR, None, "Failed"),
			"server error": (recorded("server_error"), lc.INITIATED, None, "Failed"),
			"timeout": (requests.ReadTimeout(), lc.INITIATED, None, "Failed"),
		}
		for case, (answer, status, transaction_id, logged) in cases.items():
			with self.subTest(case):
				started, _request = self.initiate(answer)
				self.assertEqual((started.status, started.transaction_id), (status, transaction_id))
				self.assertEqual(
					frappe.db.get_value("Integration Request", started.integration_request, "status"), logged
				)

	def test_the_network_log_holds_no_secret_and_the_refusal_no_number(self):
		started, request = self.initiate(recorded("payin_below_minimum"))
		signature = request.call_args.kwargs["headers"]["hmac-signature"]
		self.assertEqual(request.call_args.kwargs["json"]["i_space_key"], I_SPACE_KEY)

		log = frappe.get_doc("Integration Request", started.integration_request).as_dict()
		logged = frappe.as_json(log)
		for secret in (I_SPACE_KEY, APP_SPACE_KEY, signature):
			self.assertNotIn(secret, logged)

		refusal = frappe.get_last_doc("Error Log", filters={"method": "Nokash payment request refused"}).error
		self.assertIn("REQUEST_BAD_INFOS", refusal)
		self.assertNotIn(NATIONAL, refusal)

	def test_a_failed_network_log_does_not_lose_the_transaction_id(self):
		with (
			patch("frappe.model.document.Document.insert", side_effect=RuntimeError("log down")),
			patch("local_payments.gateway.log_error") as logged,
		):
			started, _request = self.initiate(
				recorded("payin_accepted"), frappe._dict(name="LPAY-1", amount=1000)
			)

		self.assertEqual(
			(started.status, started.integration_request, started.transaction_id),
			(lc.INITIATED, None, RECORDED["payin_accepted"]["body"]["data"]["id"]),
		)
		logged.assert_called_once()

	def test_a_request_refused_locally_never_leaves(self):
		session = frappe._dict(name=self.session().name, amount=1000.5)
		started, request = self.initiate(recorded("payin_accepted"), session)
		self.assertEqual((started.status, started.integration_request), (lc.ERROR, None))
		request.assert_not_called()

	def test_a_success_goes_through_reconcile_and_marks_the_session_paid(self):
		session = self.session(
			{"status": "Pending", "attempt_id": ATTEMPT_ID, "provider_transaction_id": TRANSACTION_ID}
		)
		attempt_id = session.attempts[0].attempt_id
		frappe.db.commit()

		with patch(HTTP) as http:
			http.return_value.request.return_value = recorded("status_success")
			rc.reconcile(attempt_id)

		self.assertEqual(
			http.return_value.request.call_args.kwargs["params"], {"transaction_id": TRANSACTION_ID}
		)
		session = reload(session)
		row = session.attempts[0]
		self.assertEqual((session.status, session.provider_transaction_id), ("Paid", TRANSACTION_ID))
		self.assertEqual((row.status, row.provider_transaction_id), ("Succeeded", TRANSACTION_ID))
		self.assertFalse(row.amount_mismatch)

	def test_a_failed_check_reports_the_error_and_leaves_the_status(self):
		session = self.session({"status": "Pending", "provider_transaction_id": TRANSACTION_ID})
		frappe.db.commit()

		with patch(HTTP) as http:
			http.return_value.request.return_value = recorded("status_unknown_transaction")
			with self.assertRaises(NokashError):
				rc.reconcile(session.attempts[0].attempt_id)

		self.assertEqual(reload(session).attempts[0].status, "Pending")

	def test_an_attempt_with_no_id_asks_nothing_and_says_so(self):
		session = self.session({"status": "Initiated"})
		with patch(HTTP) as http:
			result = self.settings.check_status(session.attempts[0].attempt_id, {})

		http.return_value.request.assert_not_called()
		self.assertEqual((result.status, result.no_reference), (lc.PENDING, True))
