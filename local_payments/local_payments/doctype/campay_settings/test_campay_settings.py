# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

import json
import time
from pathlib import Path
from unittest.mock import patch

import frappe
import requests
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime
from payments.utils import get_payment_gateway_controller

import local_payments
from local_payments import api
from local_payments import lifecycle as lc
from local_payments import reconcile as rc
from local_payments.gateway import MTN_MOMO, ORANGE_MONEY
from local_payments.local_payments.doctype.campay_settings.campay_settings import CALLBACK_PATH
from local_payments.local_payments.doctype.mtn_momo_settings.test_mtn_momo_settings import (
	http_response,
	site_host,
)
from local_payments.local_payments.doctype.nokash_settings.test_nokash_settings import (
	let_the_check_interval_pass,
	payment_args,
)
from local_payments.providers.campay import BASE_URLS, CamPayError
from local_payments.tests.test_campay import jwt
from local_payments.tests.test_checkout import make_session as make_open_session
from local_payments.tests.test_reconcile import (
	EFFECT,
	HOOK,
	consumer_calls,
	consumer_state,
	make_session,
	reload,
)

SETTINGS = "CamPay Settings"
GATEWAY = "CamPay-_test-campay"
HTTP = "local_payments.providers.campay.requests.Session"
ATTEMPT_ID = "3f5c1c6e-8f4b-4a57-9d9a-3b1f5f0f2a11"
MSISDN = "237670000001"
NATIONAL = "670000001"
# Numbers on each network, as Cameroon assigns them: 67 is MTN, 69 is Orange.
NUMBER_OF = {MTN_MOMO: "670000001", ORANGE_MONEY: "690000001"}
ACCESS_TOKEN = "fake-access-token"
WEBHOOK_KEY = "fake-webhook-key"

RECORDED = json.loads(
	(Path(local_payments.__file__).parent / "tests" / "fixtures" / "campay.json").read_text()
)
REFERENCE = RECORDED["collect_accepted"]["body"]["reference"]


def recorded(name, **fields):
	"""A recorded answer, with some of its fields replaced."""
	return http_response(RECORDED[name]["status"], {**RECORDED[name]["body"], **fields})


def make_settings(**overrides):
	values = {
		"doctype": SETTINGS,
		"gateway_name": "_test-campay",
		"enabled": 1,
		"access_token": ACCESS_TOKEN,
		"webhook_key": WEBHOOK_KEY,
		**overrides,
	}
	if frappe.db.exists(SETTINGS, values["gateway_name"]):
		frappe.delete_doc(SETTINGS, values["gateway_name"], force=True)
	return frappe.get_doc(values).insert()


def token(key=WEBHOOK_KEY, now=None):
	now = int(now or time.time())
	return jwt(claims={"iat": now, "nbf": now, "exp": now + 3600, "source": "CamPay"}, key=key)


class TestCamPaySettings(IntegrationTestCase):
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
		frappe.db.delete(
			"Notification Log", {"document_type": "Local Payment", "document_name": ["in", sessions]}
		)
		frappe.db.delete("ToDo", {"description": EFFECT})
		frappe.db.delete("Local Payment Attempt", {"parent": ["in", sessions]})
		frappe.db.delete("Local Payment", {"payment_gateway": GATEWAY})
		frappe.db.delete("Error Log", {"method": "CamPay payment request refused"})
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- reconcile() committed what this removes
		frappe.set_user("Administrator")
		if hasattr(frappe.local, "request"):
			delattr(frappe.local, "request")
		frappe.local.form_dict = frappe._dict()

	def session(self, *attempts, **extra):
		return make_session(*attempts, payment_gateway=GATEWAY, amount=100, **extra)

	def pending(self, **extra):
		return self.session(
			{"status": "Pending", "attempt_id": ATTEMPT_ID, "provider_transaction_id": REFERENCE, **extra}
		)

	def initiate(self, answer, session=None):
		session = session or self.session(title="Order 42")
		with patch(HTTP) as http:
			if isinstance(answer, Exception):
				http.return_value.request.side_effect = answer
			else:
				http.return_value.request.return_value = answer
			started = self.settings.initiate(ATTEMPT_ID, session, MSISDN, {"operator": MTN_MOMO})
		return started, http.return_value.request

	def reconcile(self, answer):
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- reconcile() runs its own transactions
		with patch(HTTP) as http:
			http.return_value.request.return_value = answer
			rc.reconcile(ATTEMPT_ID)
		return http.return_value.request

	def alerts(self, session):
		return frappe.db.count(
			"Notification Log", {"document_type": "Local Payment", "document_name": session.name}
		)

	def consumer_hook(self):
		consumer_calls.clear()
		consumer_state.update(fail=False, error=frappe.ValidationError, commit_first=False)
		hooks = patch("frappe.get_doc_hooks", return_value={"User": {"on_payment_authorized": [HOOK]}})
		hooks.start()
		self.addCleanup(hooks.stop)

	# Configuration

	def test_defaults_and_fixed_values(self):
		self.assertEqual(
			(self.settings.environment, self.settings.api_base_url), ("Sandbox", BASE_URLS["Sandbox"])
		)
		self.assertEqual((self.settings.minimum_amount, self.settings.maximum_amount), (1, 0))
		self.assertEqual(self.settings.payment_description, "{title}")
		self.assertEqual(self.settings.payer_msisdn("670000001"), MSISDN)
		self.settings.validate_transaction_currency("XAF")
		with self.assertRaises(frappe.ValidationError):
			self.settings.validate_transaction_currency("EUR")

	@site_host("https://pay.example.com")
	def test_the_api_address_follows_the_environment_only_while_it_is_a_default(self):
		self.settings.environment = "Production"
		self.settings.save()
		self.assertEqual(self.settings.api_base_url, BASE_URLS["Production"])
		self.settings.api_base_url = "https://proxy.example.com/campay"
		self.settings.environment = "Sandbox"
		self.settings.save()
		self.assertEqual(self.settings.api_base_url, "https://proxy.example.com/campay")
		self.settings.api_base_url = "http://demo.campay.net/api"
		with self.assertRaises(frappe.ValidationError):
			self.settings.save()
		frappe.clear_messages()

	def test_saving_a_configuration_makes_its_gateway_selectable(self):
		gateway = frappe.get_doc("Payment Gateway", GATEWAY)
		self.assertEqual((gateway.gateway_settings, gateway.gateway_controller), (SETTINGS, "_test-campay"))
		self.assertEqual(get_payment_gateway_controller(GATEWAY).name, self.settings.name)

	def test_the_secrets_are_encrypted_and_only_system_manager_opens_the_configuration(self):
		meta = frappe.get_meta(SETTINGS)
		for field, secret in (("access_token", ACCESS_TOKEN), ("webhook_key", WEBHOOK_KEY)):
			with self.subTest(field):
				self.assertEqual(meta.get_field(field).fieldtype, "Password")
				self.assertNotEqual(frappe.db.get_value(SETTINGS, self.settings.name, field), secret)
				self.assertEqual(self.settings.get_password(field), secret)
		self.assertEqual({perm.role for perm in meta.permissions}, {"System Manager"})
		self.assertFalse(frappe.has_permission(SETTINGS, "read", user="Guest"))

	def test_a_gateway_needs_an_operator(self):
		self.settings.accept_mtn_momo = self.settings.accept_orange_money = 0
		with self.assertRaises(frappe.ValidationError):
			self.settings.save()

	def test_amount_limits(self):
		self.settings.minimum_amount = 0
		self.settings.save()
		self.assertEqual(self.settings.minimum_amount, 1)
		for minimum, maximum in ((-1, 0), (1, -1), (50, 49)):
			with self.subTest(minimum=minimum, maximum=maximum), self.assertRaises(frappe.ValidationError):
				self.settings.update({"minimum_amount": minimum, "maximum_amount": maximum}).save()
			self.settings.reload()
		self.settings.update({"minimum_amount": 50, "maximum_amount": 50}).save()

	def test_a_payment_request_outside_the_limits_creates_nothing_and_calls_nobody(self):
		self.settings.update({"minimum_amount": 10, "maximum_amount": 100})
		cases = {
			"below the minimum": {"amount": 9},
			"above the maximum": {"amount": 101},
			"a fraction of XAF": {"amount": 50.5},
			"another currency": {"currency": "EUR"},
		}
		with patch(HTTP) as http:
			for case, overrides in cases.items():
				with self.subTest(case), self.assertRaises(frappe.ValidationError):
					self.settings.get_payment_url(**payment_args(**overrides))
			for amount in (10, 100):
				self.assertIn(
					"/local_payment_checkout?token=",
					self.settings.get_payment_url(**payment_args(amount=amount)),
				)
		http.return_value.request.assert_not_called()

	def test_the_webhook_address_comes_from_host_name(self):
		cases = {
			"https host_name": ("https://pay.example.com", f"https://pay.example.com{CALLBACK_PATH}"),
			"no host_name": (None, None),
		}
		for case, (host_name, expected) in cases.items():
			with self.subTest(case), site_host(host_name):
				self.settings.save()
				self.assertEqual(self.settings.webhook_url, expected)

	def test_an_enabled_production_gateway_with_a_webhook_key_needs_a_host_campay_can_call(self):
		production = {"environment": "Production"}
		cases = {
			"no host_name": (None, production, False),
			"http host_name": ("http://pay.example.com", production, False),
			"https host_name": ("https://pay.example.com", production, True),
			"no webhook key": (None, {**production, "webhook_key": ""}, True),
			"disabled": (None, {**production, "enabled": 0}, True),
			"sandbox": (None, {"environment": "Sandbox"}, True),
		}
		for case, (host_name, values, saved) in cases.items():
			with self.subTest(case), site_host(host_name):
				fresh = make_settings(gateway_name="_test-campay-host", enabled=0)
				fresh.update({"enabled": 1, **values})
				if saved:
					fresh.save()
				else:
					self.assertRaises(frappe.ValidationError, fresh.save)
		frappe.delete_doc(SETTINGS, "_test-campay-host", force=True)
		frappe.clear_messages()

	# Collect

	def test_the_collect_sends_the_title_as_description_and_the_attempt_id_as_external_reference(self):
		_started, request = self.initiate(recorded("collect_accepted"))
		self.assertEqual(
			request.call_args.kwargs["json"],
			{
				"amount": "100",
				"currency": "XAF",
				"from": MSISDN,
				"description": "Order 42",
				"external_reference": ATTEMPT_ID,
			},
		)

	def test_the_description_never_breaks_and_is_never_empty(self):
		cases = {
			"template": ("Paid to {title} {stray", "Order 42", "Paid to Order 42 {stray"),
			"no title": ("{title}", None, "Payment"),
			"empty template": ("", "Order 42", "Order 42"),
		}
		for case, (template, title, expected) in cases.items():
			with self.subTest(case):
				self.settings.payment_description = template
				self.assertEqual(self.settings.description_for(frappe._dict(title=title)), expected)

	def test_initiation_outcomes(self):
		cases = {
			"accepted": (recorded("collect_accepted"), lc.INITIATED, REFERENCE, None, "Completed"),
			"invalid number": (recorded("collect_invalid_number"), lc.ERROR, None, None, "Failed"),
			"invalid amount": (recorded("collect_invalid_amount"), lc.ERROR, None, None, "Failed"),
			"refused token": (
				http_response(401, {"detail": "x"}),
				lc.ERROR,
				None,
				"provider_refused",
				"Failed",
			),
			"duplicate": (recorded("collect_duplicate"), lc.INITIATED, None, None, "Failed"),
			"server error": (http_response(500), lc.INITIATED, None, None, "Failed"),
			"timeout": (requests.ReadTimeout(), lc.INITIATED, None, None, "Failed"),
		}
		for case, (answer, status, reference, alert, logged) in cases.items():
			with self.subTest(case):
				started, request = self.initiate(answer)
				self.assertEqual(request.call_count, 1)
				self.assertEqual(
					(started.status, started.transaction_id, started.alert), (status, reference, alert)
				)
				self.assertEqual(
					frappe.db.get_value("Integration Request", started.integration_request, "status"), logged
				)

	def test_the_network_log_holds_no_token_and_the_refusal_no_number(self):
		started, request = self.initiate(recorded("collect_invalid_number"))
		self.assertEqual(request.call_args.kwargs["headers"], {"Authorization": f"Token {ACCESS_TOKEN}"})

		logged = frappe.as_json(frappe.get_doc("Integration Request", started.integration_request).as_dict())
		self.assertNotIn(ACCESS_TOKEN, logged)
		self.assertNotIn(WEBHOOK_KEY, logged)

		refusal = frappe.get_last_doc("Error Log", filters={"method": "CamPay payment request refused"}).error
		self.assertIn("ER101", refusal)
		self.assertNotIn(NATIONAL, refusal)

	def test_a_request_refused_locally_never_leaves(self):
		session = frappe._dict(name=self.session().name, amount=100.5, title="x")
		started, request = self.initiate(recorded("collect_accepted"), session)
		self.assertEqual((started.status, started.integration_request), (lc.ERROR, None))
		request.assert_not_called()

	def test_a_number_of_the_other_network_is_refused_before_anything_is_recorded_or_sent(self):
		session = make_open_session(payment_gateway=GATEWAY, amount=100)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- start_attempt() runs its own transactions
		with patch(HTTP) as http:
			with self.assertRaisesRegex(frappe.ValidationError, "not an MTN number"):
				api.start_attempt(session.token, NUMBER_OF[ORANGE_MONEY], MTN_MOMO)
			with self.assertRaises(frappe.ValidationError):
				api.start_attempt(session.token, NUMBER_OF[MTN_MOMO], "AIRTEL")
		http.return_value.request.assert_not_called()
		self.assertEqual(reload(session).attempts, [])

	# Status

	def test_a_payment_with_each_operator_ends_paid_and_notifies_once_by_polling_alone(self):
		self.consumer_hook()
		for operator in (MTN_MOMO, ORANGE_MONEY):
			with self.subTest(operator):
				consumer_calls.clear()
				session = make_open_session(payment_gateway=GATEWAY, amount=100)
				frappe.db.commit()  # nosemgrep: frappe-manual-commit -- start_attempt() runs its own transactions

				with patch(HTTP) as http:
					request = http.return_value.request
					request.return_value = recorded("collect_accepted")
					state = api.start_attempt(session.token, NUMBER_OF[operator], operator)
					self.assertEqual(state["attempt_status"], lc.INITIATED)

					attempt_id = reload(session).attempts[0].attempt_id
					request.return_value = recorded("status_pending", external_reference=attempt_id)
					self.assertEqual(api.get_status(session.token)["attempt_status"], lc.PENDING)
					self.assertTrue(request.call_args.args[1].endswith(f"/transaction/{REFERENCE}/"))

					let_the_check_interval_pass(attempt_id)
					request.return_value = recorded("status_successful", external_reference=attempt_id)
					state = api.get_status(session.token)
					self.assertEqual((state["status"], state["attempt_status"]), ("Paid", lc.SUCCEEDED))

				session = reload(session)
				self.assertEqual(
					(session.authorization, session.provider_transaction_id), ("Done", REFERENCE)
				)
				self.assertEqual(len(consumer_calls), 1)

	def test_a_sandbox_success_reports_zero_and_never_pays_the_session(self):
		self.consumer_hook()
		session = self.pending()
		self.reconcile(recorded("status_successful_sandbox"))

		session = reload(session)
		row = session.attempts[0]
		self.assertEqual((session.status, row.status, row.amount_mismatch), ("Open", lc.SUCCEEDED, 1))
		self.assertEqual(consumer_calls, [])

	def test_a_failure_ends_the_attempt_with_its_reason(self):
		session = self.pending()
		self.reconcile(recorded("status_failed"))
		row = reload(session).attempts[0]
		self.assertEqual((row.status, row.provider_status), (lc.FAILED, "FAILED: TESTING"))
		self.assertEqual(reload(session).status, "Open")

	def test_an_answer_about_another_attempt_changes_nothing(self):
		session = self.pending()
		other = "11111111-1111-4111-8111-111111111111"
		with self.assertRaises(CamPayError):
			self.reconcile(recorded("status_successful", external_reference=other))
		self.assertEqual(reload(session).attempts[0].status, lc.PENDING)
		self.assertEqual(reload(session).status, "Open")

	def test_still_pending_after_the_delay_becomes_unresolved_and_a_late_success_counts(self):
		self.consumer_hook()
		session = self.pending(expires_on=add_to_date(now_datetime(), minutes=-1))
		self.reconcile(recorded("status_pending"))
		self.assertEqual(reload(session).attempts[0].status, lc.UNRESOLVED)

		let_the_check_interval_pass(ATTEMPT_ID)
		self.reconcile(recorded("status_successful"))
		self.assertEqual(reload(session).status, "Paid")
		self.assertEqual(len(consumer_calls), 1)

	def test_a_refused_token_alerts_the_managers_once_and_leaves_the_attempt_to_its_deadline(self):
		session = self.pending()
		for check in (1, 2):
			with self.subTest(check=check), self.assertRaises(CamPayError) as refused:
				self.reconcile(http_response(401, {"detail": "Invalid token."}))
			self.assertTrue(refused.exception.refused)
			let_the_check_interval_pass(ATTEMPT_ID)
			self.assertEqual(reload(session).attempts[0].status, lc.PENDING)
			self.assertEqual(self.alerts(session), 1)

	def test_an_unknown_reference_or_a_server_error_alerts_nobody(self):
		session = self.pending()
		for answer in (recorded("status_not_found"), http_response(500)):
			with self.assertRaises(CamPayError):
				self.reconcile(answer)
			let_the_check_interval_pass(ATTEMPT_ID)
		self.assertEqual(reload(session).attempts[0].status, lc.PENDING)
		self.assertEqual(self.alerts(session), 0)

	def test_an_attempt_with_no_reference_asks_nothing_and_says_so(self):
		session = self.session({"status": "Initiated"})
		with patch(HTTP) as http:
			result = self.settings.check_status(session.attempts[0].attempt_id, {})
		http.return_value.request.assert_not_called()
		self.assertEqual((result.status, result.no_reference), (lc.PENDING, True))

	# Webhook

	def test_the_webhook_is_verified_with_the_gateways_own_key_and_must_be_about_the_attempt(self):
		body = {"external_reference": ATTEMPT_ID, "reference": REFERENCE, "signature": token()}
		self.assertTrue(self.settings.verify_callback(body, None, ATTEMPT_ID, REFERENCE))
		# Before the collect answered, the token and the attempt id are all there is to check.
		self.assertTrue(self.settings.verify_callback(body, None, ATTEMPT_ID, None))

		cases = {
			"other key": {**body, "signature": token(key="other-key")},
			"expired token": {**body, "signature": token(now=time.time() - 7200)},
			"no token": {k: v for k, v in body.items() if k != "signature"},
			"other attempt": {**body, "external_reference": "11111111-1111-4111-8111-111111111111"},
			"other reference": {**body, "reference": "9d9a9d9a-0000-4000-8000-000000000000"},
		}
		for case, changed in cases.items():
			with self.subTest(case):
				self.assertFalse(self.settings.verify_callback(changed, None, ATTEMPT_ID, REFERENCE))

		self.settings.webhook_key = ""
		self.settings.save()
		self.assertFalse(self.settings.verify_callback(body, None, ATTEMPT_ID, REFERENCE))
