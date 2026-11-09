# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

import json
from pathlib import Path
from unittest.mock import patch

import frappe
import requests
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime, set_request
from frappe.website.serve import get_response_content
from payments.utils import get_payment_gateway_controller

import local_payments
from local_payments import api
from local_payments import lifecycle as lc
from local_payments import reconcile as rc
from local_payments.local_payments.doctype.mtn_momo_settings.test_mtn_momo_settings import (
	http_response,
	site_host,
)
from local_payments.local_payments.doctype.nokash_settings.nokash_settings import CALLBACK_PATH
from local_payments.providers.nokash import MTN_MOMO, ORANGE_MONEY, NokashError
from local_payments.tests.test_checkout import PAGE
from local_payments.tests.test_checkout import make_session as make_open_session
from local_payments.tests.test_reconcile import (
	EFFECT,
	HOOK,
	consumer_calls,
	consumer_state,
	make_session,
	reload,
)

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


def recorded_for(name, attempt_id, **data):
	"""A recorded status answer about this attempt, as Nokash would give it."""
	body = RECORDED[name]["body"]
	return http_response(
		RECORDED[name]["status"], {**body, "data": {**body["data"], "orderId": attempt_id, **data}}
	)


def payment_args(**overrides):
	return {
		"amount": 1000,
		"currency": "XAF",
		"reference_doctype": "User",
		"reference_docname": "Administrator",
		**overrides,
	}


def let_the_check_interval_pass(attempt_id):
	frappe.db.set_value(
		"Local Payment Attempt",
		{"attempt_id": attempt_id},
		"last_checked_on",
		add_to_date(now_datetime(), minutes=-1),
		update_modified=False,
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- the next check reads it in its own transaction


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
		frappe.db.delete(
			"Notification Log", {"document_type": "Local Payment", "document_name": ["in", sessions]}
		)
		frappe.db.delete("ToDo", {"description": EFFECT})
		frappe.db.delete("Local Payment Attempt", {"parent": ["in", sessions]})
		frappe.db.delete("Local Payment", {"payment_gateway": GATEWAY})
		frappe.db.delete("Error Log", {"method": "Nokash payment request refused"})
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- reconcile() committed what this removes
		frappe.set_user("Administrator")
		if hasattr(frappe.local, "request"):
			delattr(frappe.local, "request")
		frappe.local.form_dict = frappe._dict()

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
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- reconcile() runs its own transactions

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
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- reconcile() rolls back on a failed check

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

	def consumer_hook(self):
		consumer_calls.clear()
		consumer_state.update(fail=False, error=frappe.ValidationError, commit_first=False)
		hooks = patch("frappe.get_doc_hooks", return_value={"User": {"on_payment_authorized": [HOOK]}})
		hooks.start()
		self.addCleanup(hooks.stop)

	def open_session(self):
		return make_open_session(payment_gateway=GATEWAY, amount=1000)

	def render(self, token):
		frappe.set_user("Guest")
		frappe.local.form_dict = frappe._dict(token=token)
		set_request(method="GET", path=PAGE, query_string=f"token={token}")
		content = get_response_content(PAGE)
		frappe.set_user("Administrator")
		return content

	def test_saving_a_configuration_makes_its_gateway_selectable_by_any_consumer(self):
		gateway = frappe.get_doc("Payment Gateway", GATEWAY)
		self.assertEqual((gateway.gateway_settings, gateway.gateway_controller), (SETTINGS, "nokash-test"))
		self.assertEqual(get_payment_gateway_controller(GATEWAY).name, self.settings.name)

		with patch("local_payments.gateway.call_hook_method") as announced:
			make_settings(gateway_name="nokash-announced")
			make_settings(gateway_name="nokash-quiet", enabled=0)
		announced.assert_called_once_with("payment_gateway_enabled", gateway="Nokash-nokash-announced")

	def test_several_configurations_coexist(self):
		production = make_settings(gateway_name="nokash-production", environment="Production")
		self.assertEqual((self.settings.environment, production.environment), ("Sandbox", "Production"))
		for settings, gateway in ((self.settings, GATEWAY), (production, "Nokash-nokash-production")):
			with self.subTest(gateway=gateway):
				self.assertEqual(get_payment_gateway_controller(gateway).name, settings.name)
		frappe.clear_messages()

	def test_a_disabled_configuration_refuses_new_payment_requests(self):
		self.settings.db_set("enabled", 0)
		with self.assertRaises(frappe.ValidationError):
			self.settings.get_payment_url(**payment_args())
		self.assertFalse(frappe.db.exists("Local Payment", {"payment_gateway": GATEWAY}))

	def test_the_keys_are_encrypted(self):
		meta = frappe.get_meta(SETTINGS)
		auth = frappe.qb.DocType("__Auth")
		stored = dict(
			frappe.qb.from_(auth)
			.select(auth.fieldname, auth.password)
			.where((auth.doctype == SETTINGS) & (auth.name == self.settings.name))
			.run()
		)
		for field, secret in (("i_space_key", I_SPACE_KEY), ("app_space_key", APP_SPACE_KEY)):
			with self.subTest(field):
				self.assertEqual(meta.get_field(field).fieldtype, "Password")
				self.assertNotEqual(frappe.db.get_value(SETTINGS, self.settings.name, field), secret)
				self.assertNotEqual(stored[field], secret)
				self.assertEqual(self.settings.get_password(field), secret)

	def test_only_system_manager_can_open_the_configuration(self):
		self.assertEqual({perm.role for perm in frappe.get_meta(SETTINGS).permissions}, {"System Manager"})
		self.assertFalse(frappe.has_permission(SETTINGS, "read", user="Guest"))

	def test_a_refused_payment_request_creates_nothing_and_calls_nobody(self):
		self.settings.minimum_amount = 500
		cases = {
			"below the configured minimum": {"amount": 499},
			"a fraction of XAF": {"amount": 1000.5},
			"another currency": {"currency": "EUR"},
		}
		with patch(HTTP) as http:
			for case, overrides in cases.items():
				with self.subTest(case), self.assertRaises(frappe.ValidationError):
					self.settings.get_payment_url(**payment_args(**overrides))
		http.return_value.request.assert_not_called()
		self.assertFalse(frappe.db.exists("Local Payment", {"payment_gateway": GATEWAY}))

	def test_the_minimum_itself_is_accepted_without_calling_nokash(self):
		self.settings.minimum_amount = 500
		with patch(HTTP) as http:
			url = self.settings.get_payment_url(**payment_args(amount=500))
		self.assertIn("/local_payment_checkout?token=", url)
		http.return_value.request.assert_not_called()

	def test_erpnext_finds_the_minimum_check(self):
		self.settings.validate_minimum_transaction_amount("XAF", 200)
		with self.assertRaises(frappe.ValidationError):
			self.settings.validate_minimum_transaction_amount("XAF", 199)

	def test_the_minimum_defaults_when_empty_and_cannot_be_negative(self):
		self.assertEqual(self.settings.minimum_amount, 200)
		self.settings.minimum_amount = 0
		self.settings.save()
		self.assertEqual(self.settings.minimum_amount, 200)
		self.settings.minimum_amount = -1
		with self.assertRaises(frappe.ValidationError):
			self.settings.save()

	def test_the_page_offers_the_operators_enabled_in_the_configuration(self):
		both = self.render(self.open_session().token)
		self.assertIn(f'data-operator="{MTN_MOMO}"', both)
		self.assertIn(f'data-operator="{ORANGE_MONEY}"', both)

		self.settings.db_set("accept_mtn_momo", 0)
		alone = self.render(self.open_session().token)
		self.assertNotIn('name="operator"', alone)
		self.assertIn('id="lp-start-attempt"', alone)

	def test_a_bad_number_or_operator_is_refused_before_anything_is_recorded_or_sent(self):
		session = self.open_session()
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- start_attempt() runs its own transactions
		refused = {
			"too short": ("60000000", MTN_MOMO),
			"too long": ("6000000011", MTN_MOMO),
			"letters": ("6000000ab", MTN_MOMO),
			"other country": ("+33600000001", MTN_MOMO),
			"unknown operator": ("600000001", "AIRTEL"),
			"no operator": ("600000001", None),
		}
		with patch(HTTP) as http:
			for case, (number, operator) in refused.items():
				with self.subTest(case), self.assertRaises(frappe.ValidationError):
					api.start_attempt(session.token, number, operator)
		http.return_value.request.assert_not_called()
		self.assertEqual(reload(session).attempts, [])

	def test_only_a_payin_refused_by_nokash_alerts_the_managers(self):
		def start(answer):
			session = self.open_session()
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- start_attempt() runs its own transactions
			with patch(HTTP) as http:
				if isinstance(answer, Exception):
					http.return_value.request.side_effect = answer
				else:
					http.return_value.request.return_value = answer
				state = api.start_attempt(session.token, "600000001", MTN_MOMO)
			alerts = frappe.get_all(
				"Notification Log",
				{"document_type": "Local Payment", "document_name": session.name},
				pluck="subject",
			)
			return state["attempt_status"], alerts

		status, alerts = start(recorded("payin_below_minimum"))
		self.assertEqual(status, lc.ERROR)
		self.assertEqual(len(alerts), len(rc.alert_recipients()))
		self.assertIn("refused", alerts[0])

		status, alerts = start(requests.ReadTimeout())
		self.assertEqual((status, alerts), (lc.INITIATED, []))

	def test_the_minimum_is_checked_last_so_the_refusal_gives_the_real_reason(self):
		with self.assertRaisesRegex(frappe.ValidationError, "only accepts"):
			self.settings.get_payment_url(**payment_args(amount=5, currency="EUR"))
		self.settings.db_set("enabled", 0)
		with self.assertRaisesRegex(frappe.ValidationError, "is disabled"):
			self.settings.get_payment_url(**payment_args(amount=5))

	def test_a_payment_with_each_operator_ends_paid_and_notifies_once_by_polling_alone(self):
		self.consumer_hook()
		accepted_id = RECORDED["payin_accepted"]["body"]["data"]["id"]
		for operator in (MTN_MOMO, ORANGE_MONEY):
			with self.subTest(operator):
				consumer_calls.clear()
				session = self.open_session()
				frappe.db.commit()  # nosemgrep: frappe-manual-commit -- start_attempt() runs its own transactions

				with patch(HTTP) as http:
					request = http.return_value.request
					request.return_value = recorded("payin_accepted")
					state = api.start_attempt(session.token, "600000001", operator)
					self.assertEqual(state["attempt_status"], lc.INITIATED)
					sent = request.call_args.kwargs["json"]
					self.assertEqual((sent["payment_method"], sent["amount"]), (operator, 1000))
					self.assertNotIn("callback_url", sent)

					attempt_id = reload(session).attempts[0].attempt_id
					request.return_value = recorded_for("status_pending", attempt_id, id=accepted_id)
					self.assertEqual(api.get_status(session.token)["attempt_status"], lc.PENDING)

					let_the_check_interval_pass(attempt_id)
					request.return_value = recorded_for("status_success", attempt_id, id=accepted_id)
					state = api.get_status(session.token)
					self.assertEqual((state["status"], state["attempt_status"]), ("Paid", lc.SUCCEEDED))

					calls = request.call_count
					let_the_check_interval_pass(attempt_id)
					api.get_status(session.token)
					self.assertEqual(request.call_count, calls)

				session = reload(session)
				self.assertEqual(
					(session.authorization, session.provider_transaction_id), ("Done", accepted_id)
				)
				self.assertEqual(len(consumer_calls), 1)

	def test_a_replayed_success_notifies_the_consumer_once(self):
		self.consumer_hook()
		session = self.session(
			{"status": "Pending", "attempt_id": ATTEMPT_ID, "provider_transaction_id": TRANSACTION_ID}
		)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- reconcile() runs its own transactions

		with patch(HTTP) as http:
			http.return_value.request.return_value = recorded_for(
				"status_success", ATTEMPT_ID, id=TRANSACTION_ID
			)
			rc.reconcile(ATTEMPT_ID)
			let_the_check_interval_pass(ATTEMPT_ID)
			self.assertIsNone(rc.reconcile(ATTEMPT_ID))

		self.assertEqual(reload(session).status, "Paid")
		self.assertEqual(len(consumer_calls), 1)

	def test_a_different_amount_is_flagged_and_the_session_stays_open(self):
		self.consumer_hook()
		session = self.session(
			{"status": "Pending", "attempt_id": ATTEMPT_ID, "provider_transaction_id": TRANSACTION_ID}
		)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- reconcile() runs its own transactions

		with patch(HTTP) as http:
			http.return_value.request.return_value = recorded_for(
				"status_success", ATTEMPT_ID, id=TRANSACTION_ID, amount=900
			)
			rc.reconcile(ATTEMPT_ID)

		session = reload(session)
		self.assertEqual((session.status, session.attempts[0].amount_mismatch), ("Open", 1))
		self.assertEqual(consumer_calls, [])

	def test_still_pending_after_the_delay_becomes_unresolved_and_a_late_success_counts(self):
		self.consumer_hook()
		session = self.session(
			{
				"status": "Pending",
				"attempt_id": ATTEMPT_ID,
				"provider_transaction_id": TRANSACTION_ID,
				"expires_on": add_to_date(now_datetime(), minutes=-1),
			}
		)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- reconcile() runs its own transactions

		with patch(HTTP) as http:
			http.return_value.request.return_value = recorded_for(
				"status_pending", ATTEMPT_ID, id=TRANSACTION_ID
			)
			rc.reconcile(ATTEMPT_ID)
			row = reload(session).attempts[0]
			self.assertEqual(row.status, lc.UNRESOLVED)
			self.assertTrue(row.next_check_on)

			let_the_check_interval_pass(ATTEMPT_ID)
			http.return_value.request.return_value = recorded_for(
				"status_success", ATTEMPT_ID, id=TRANSACTION_ID
			)
			rc.reconcile(ATTEMPT_ID)

		self.assertEqual(reload(session).status, "Paid")
		self.assertEqual(len(consumer_calls), 1)

	def test_the_notification_address_comes_from_host_name_only_and_carries_the_attempt_id(self):
		# The payer's request can name any host.
		set_request(method="POST", base_url="http://attacker.example")
		cases = {
			"host_name set": (
				"https://pay.example.com",
				1,
				f"https://pay.example.com{CALLBACK_PATH}?attempt={ATTEMPT_ID}",
			),
			"no host_name": (None, 1, None),
			"notification off": ("https://pay.example.com", 0, None),
		}
		for case, (host_name, send_callback, expected) in cases.items():
			with self.subTest(case), site_host(host_name):
				self.settings.db_set("send_callback", send_callback)
				_started, request = self.initiate(recorded("payin_accepted"))
				self.assertEqual(request.call_args.kwargs["json"].get("callback_url"), expected)

	def test_an_enabled_production_gateway_needs_a_host_nokash_can_call(self):
		production = {"environment": "Production", "send_callback": 1}
		cases = {
			"no host_name": (None, production, False),
			"http host_name": ("http://pay.example.com", production, False),
			"https host_name": ("https://pay.example.com", production, True),
			"callback off": (None, {**production, "send_callback": 0}, True),
			"sandbox": (None, {**production, "environment": "Sandbox"}, True),
		}
		for case, (host_name, values, saved) in cases.items():
			with self.subTest(case), site_host(host_name):
				self.settings.update(values)
				if saved:
					self.settings.save()
				else:
					self.assertRaises(frappe.ValidationError, self.settings.save)
			self.settings.reload()
		frappe.clear_messages()
