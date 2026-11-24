# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

import hashlib
import hmac
import json
import uuid
from datetime import timedelta
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
# Numbers on each network, as Cameroon assigns them: 67 is MTN, 69 is Orange.
MTN_NUMBER = "670000001"
ORANGE_NUMBER = "690000001"
NUMBER_OF = {MTN_MOMO: MTN_NUMBER, ORANGE_MONEY: ORANGE_NUMBER}
TRANSACTION_ID = "nk_tx_xm5ojhd01lkv06c0bjvu"
I_SPACE_KEY = "fake-i-key"
APP_SPACE_KEY = "fake-app-key"
INTEGRATION_KEY = "fake-integration-key"

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
		"integration_key": INTEGRATION_KEY,
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
		headers = request.call_args.kwargs["headers"]
		signature = headers["hmac-signature"]
		self.assertEqual(request.call_args.kwargs["json"]["i_space_key"], I_SPACE_KEY)
		# Without it Nokash refuses the call outright, so the configured key has to reach the request.
		self.assertEqual(headers["X-NK-Integration"], INTEGRATION_KEY)

		log = frappe.get_doc("Integration Request", started.integration_request).as_dict()
		logged = frappe.as_json(log)
		for secret in (I_SPACE_KEY, APP_SPACE_KEY, INTEGRATION_KEY, signature):
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

		call = http.return_value.request.call_args
		self.assertTrue(call.args[1].endswith("/lapas-on-trans/trans/611/status-request"))
		# The key travels in the form body, never in the URL.
		self.assertEqual(call.kwargs["data"], {"i_space_key": I_SPACE_KEY, "transaction_id": TRANSACTION_ID})
		self.assertNotIn("params", call.kwargs)
		session = reload(session)
		row = session.attempts[0]
		self.assertEqual((session.status, session.provider_transaction_id), ("Paid", TRANSACTION_ID))
		self.assertEqual((row.status, row.provider_transaction_id), ("Succeeded", TRANSACTION_ID))
		self.assertFalse(row.amount_mismatch)

	def test_nokash_gets_at_most_one_status_call_every_30_seconds_per_attempt(self):
		self.assertEqual(rc.min_check_interval(GATEWAY), timedelta(seconds=30))
		self.assertEqual(rc.min_check_interval(None), rc.MIN_CHECK_INTERVAL)

		session = self.session(
			{"status": "Pending", "attempt_id": ATTEMPT_ID, "provider_transaction_id": TRANSACTION_ID}
		)
		for seconds_ago, asked in ((20, False), (31, True)):
			with self.subTest(seconds_ago=seconds_ago):
				frappe.db.set_value(
					"Local Payment Attempt",
					{"attempt_id": ATTEMPT_ID},
					"last_checked_on",
					add_to_date(now_datetime(), seconds=-seconds_ago),
					update_modified=False,
				)
				frappe.db.commit()  # nosemgrep: frappe-manual-commit -- reconcile() runs its own transactions
				with patch(HTTP) as http:
					http.return_value.request.return_value = recorded_for("status_pending", ATTEMPT_ID)
					rc.reconcile(ATTEMPT_ID)
				self.assertEqual(http.return_value.request.called, asked)

		# The next check is not due before the interval is over, so the scheduler leaves it alone until then.
		row = reload(session).attempts[0]
		self.assertGreaterEqual(row.next_check_on - row.last_checked_on, timedelta(seconds=30))

	def test_a_refused_key_alerts_the_managers_once_and_leaves_the_attempt_to_its_deadline(self):
		session = self.session(
			{"status": "Pending", "attempt_id": ATTEMPT_ID, "provider_transaction_id": TRANSACTION_ID}
		)
		alerts = {"document_type": "Local Payment", "document_name": session.name}
		for check in (1, 2):
			with self.subTest(check=check):
				frappe.db.commit()  # nosemgrep: frappe-manual-commit -- reconcile() runs its own transactions
				with patch(HTTP) as http, self.assertRaises(NokashError) as refused:
					http.return_value.request.return_value = recorded("status_611_refused")
					rc.reconcile(ATTEMPT_ID)
				self.assertTrue(refused.exception.refused)
				let_the_check_interval_pass(ATTEMPT_ID)

				self.assertEqual(reload(session).attempts[0].status, "Pending")
				self.assertEqual(frappe.db.count("Notification Log", alerts), 1)

	def test_a_server_error_is_not_a_refused_key_and_alerts_nobody(self):
		session = self.session(
			{"status": "Pending", "attempt_id": ATTEMPT_ID, "provider_transaction_id": TRANSACTION_ID}
		)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- reconcile() runs its own transactions
		with patch(HTTP) as http, self.assertRaises(NokashError):
			http.return_value.request.return_value = recorded("server_error")
			rc.reconcile(ATTEMPT_ID)
		self.assertEqual(
			frappe.db.count(
				"Notification Log", {"document_type": "Local Payment", "document_name": session.name}
			),
			0,
		)

	def signed(self, body, key=I_SPACE_KEY):
		signed = ":".join(
			"null" if body.get(name) is None else str(body[name])
			for name in ("id", "amount", "orderId", "status", "phone")
		)
		return f"t=1762956000,s={hmac.new(key.encode(), signed.encode(), hashlib.sha256).hexdigest()}"

	def notification(self, **changes):
		return {
			"id": TRANSACTION_ID,
			"orderId": ATTEMPT_ID,
			"amount": "1000",
			"status": "SUCCESS",
			"statusReason": None,
			"phone": MSISDN,
			**changes,
		}

	def test_a_notification_is_verified_with_the_gateways_own_key_and_must_be_about_the_attempt(self):
		body = self.notification()
		verified = self.settings.verify_callback

		self.assertTrue(verified(body, self.signed(body), ATTEMPT_ID, TRANSACTION_ID))
		# An attempt that never got its id can only be compared by order id.
		self.assertTrue(verified(body, self.signed(body), ATTEMPT_ID, None))

		forged = {**body, "status": "FAILED"}
		cases = {
			"signed with another key": (
				body,
				self.signed(body, key="not-our-key"),
				ATTEMPT_ID,
				TRANSACTION_ID,
			),
			"body changed after signing": (forged, self.signed(body), ATTEMPT_ID, TRANSACTION_ID),
			"genuine, about another attempt": (body, self.signed(body), str(uuid.uuid4()), TRANSACTION_ID),
			"genuine, about another transaction": (body, self.signed(body), ATTEMPT_ID, "nk_tx_other"),
			"no signature": (body, None, ATTEMPT_ID, TRANSACTION_ID),
		}
		for case, args in cases.items():
			with self.subTest(case):
				self.assertFalse(verified(*args))

	def test_a_signed_notification_queues_a_check_and_a_forged_one_is_refused(self):
		self.session(
			{"status": "Pending", "attempt_id": ATTEMPT_ID, "provider_transaction_id": TRANSACTION_ID}
		)
		body = self.notification()
		for case, header, queued in (
			("signed", self.signed(body), True),
			("signed with another key", self.signed(body, key="not-our-key"), False),
			("not signed", None, False),
		):
			with self.subTest(case):
				set_request(
					method="POST",
					path="/api/method/local_payments.api.nokash_callback",
					query_string=f"attempt={ATTEMPT_ID}",
					data=json.dumps(body),
					content_type="application/json",
					headers={"hmac-signature": header} if header else {},
				)
				frappe.local.request_ip = "127.0.0.1"
				frappe.local.response = frappe._dict()
				with patch.object(frappe, "enqueue") as enqueue:
					api.nokash_callback()
				self.assertEqual(enqueue.called, queued)
				self.assertEqual(frappe.local.response.get("http_status_code"), None if queued else 401)

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
		for field, secret in (
			("i_space_key", I_SPACE_KEY),
			("app_space_key", APP_SPACE_KEY),
			("integration_key", INTEGRATION_KEY),
		):
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
			"unknown operator": (MTN_NUMBER, "AIRTEL"),
			"no operator": (MTN_NUMBER, None),
		}
		with patch(HTTP) as http:
			for case, (number, operator) in refused.items():
				with self.subTest(case), self.assertRaises(frappe.ValidationError):
					api.start_attempt(session.token, number, operator)
		http.return_value.request.assert_not_called()
		self.assertEqual(reload(session).attempts, [])

	def test_a_number_of_the_other_network_is_refused_by_name_before_anything_is_recorded_or_sent(self):
		session = self.open_session()
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- start_attempt() runs its own transactions
		cases = {
			"Orange number on MTN": (ORANGE_NUMBER, MTN_MOMO, "This is not an MTN number"),
			"Orange 655 on MTN": ("+237 655 000 001", MTN_MOMO, "This is not an MTN number"),
			"MTN number on Orange": (MTN_NUMBER, ORANGE_MONEY, "This is not an Orange number"),
			"MTN 650 on Orange": ("650000001", ORANGE_MONEY, "This is not an Orange number"),
			"MTN 681 on Orange": ("681000001", ORANGE_MONEY, "This is not an Orange number"),
			"no network's number on MTN": ("620000001", MTN_MOMO, "This is not an MTN number"),
		}
		with patch(HTTP) as http:
			for case, (number, operator, message) in cases.items():
				with self.subTest(case), self.assertRaisesRegex(frappe.ValidationError, message):
					api.start_attempt(session.token, number, operator)
		http.return_value.request.assert_not_called()
		self.assertEqual(reload(session).attempts, [])

	def test_every_number_on_the_chosen_network_goes_through(self):
		for operator, numbers in {
			MTN_MOMO: ("670000001", "650000001", "654000001", "681000001"),
			ORANGE_MONEY: ("690000001", "655000001", "659000001"),
		}.items():
			for number in numbers:
				with self.subTest(operator=operator, number=number):
					msisdn = self.settings.payer_msisdn(number)
					self.settings.validate_payer_network(msisdn, {"operator": operator})

	def test_only_a_payin_refused_by_nokash_alerts_the_managers(self):
		def start(answer):
			session = self.open_session()
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- start_attempt() runs its own transactions
			with patch(HTTP) as http:
				if isinstance(answer, Exception):
					http.return_value.request.side_effect = answer
				else:
					http.return_value.request.return_value = answer
				try:
					api.start_attempt(session.token, MTN_NUMBER, MTN_MOMO)
				except frappe.ValidationError as refused:
					# The payer reads that the method is unavailable: the keys or the settings need fixing.
					self.assertIn("not available at the moment", str(refused))
			alerts = frappe.get_all(
				"Notification Log",
				{"document_type": "Local Payment", "document_name": session.name},
				pluck="subject",
			)
			status = frappe.db.get_value("Local Payment Attempt", {"parent": session.name}, "status")
			return status, alerts

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
					state = api.start_attempt(session.token, NUMBER_OF[operator], operator)
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
