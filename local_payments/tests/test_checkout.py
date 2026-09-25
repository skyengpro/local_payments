# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, fmt_money, now_datetime, set_request
from frappe.website.serve import get_response, get_response_content

from local_payments import api
from local_payments import reconcile as rc
from local_payments.lifecycle import ProviderResult

SETTINGS = "Local Payments Test Settings"
GATEWAY_NAME = "lp-checkout"
GATEWAY = "Local Payments Test-lp-checkout"
PROVIDER = "Local Payments Test"
PAGE = "local_payment_checkout"
# The form element itself: the page script names the same id.
FORM = 'id="lp-start-attempt"'

UNKNOWN_TOKEN = "f" * 32
MALFORMED_TOKENS = ("", "not-a-token", "F" * 32, "a" * 31, "a" * 33, "' or 1=1 --")


class FakeProvider:
	"""Returns canned results in order and remembers what it was asked."""

	def __init__(self, *results):
		self.results = list(results)
		self.calls = []

	def check_status(self, attempt_id, provider_data):
		self.calls.append(attempt_id)
		return self.results.pop(0)


def make_session(*attempts, status="Open", **extra):
	rows = [
		{
			"attempt_id": frappe.generate_hash(length=36),
			"status": "Pending",
			"expires_on": add_to_date(now_datetime(), hours=1),
			**attempt,
		}
		for attempt in attempts
	]
	return frappe.get_doc(
		{
			"doctype": "Local Payment",
			"payment_gateway": GATEWAY,
			"reference_doctype": "User",
			"reference_docname": "Administrator",
			"amount": 5000,
			"currency": "XAF",
			"title": "Invoice 42",
			"status": status,
			"attempts": rows,
			**extra,
		}
	).insert(ignore_permissions=True)


class CheckoutTestCase(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("Currency", "XAF"):
			frappe.get_doc({"doctype": "Currency", "currency_name": "XAF", "enabled": 1}).insert()
		if not frappe.db.exists(SETTINGS, GATEWAY_NAME):
			frappe.get_doc(
				{
					"doctype": SETTINGS,
					"gateway_name": GATEWAY_NAME,
					"currency": "XAF",
					"enabled": 1,
				}
			).insert()
		frappe.db.commit()

	def setUp(self):
		self.addCleanup(self.clean_up)

	def clean_up(self):
		# get_status commits through reconcile(), so the class rollback can't undo these sessions.
		frappe.db.rollback()
		frappe.db.delete("Local Payment Attempt", {"parenttype": "Local Payment"})
		frappe.db.delete("Local Payment", {"payment_gateway": GATEWAY})
		frappe.db.commit()
		frappe.set_user("Administrator")
		if hasattr(frappe.local, "request"):
			delattr(frappe.local, "request")
		frappe.local.form_dict = frappe._dict()


class TestCheckoutPage(CheckoutTestCase):
	def render(self, token):
		frappe.set_user("Guest")
		frappe.local.form_dict = frappe._dict(token=token)
		set_request(method="GET", path=PAGE, query_string=f"token={token}")
		return get_response_content(PAGE)

	def response(self, token):
		frappe.set_user("Guest")
		frappe.local.form_dict = frappe._dict(token=token)
		set_request(method="GET", path=PAGE, query_string=f"token={token}")
		return get_response(PAGE)

	def test_open_session_shows_what_to_pay_and_the_payment_form(self):
		session = make_session()
		content = self.render(session.token)

		self.assertIn("Invoice 42", content)
		# XAF has no minor unit, so the page shows no decimals.
		self.assertIn(fmt_money(5000, 0, currency="XAF"), content)
		self.assertIn("XAF", content)
		self.assertIn(PROVIDER, content)
		self.assertIn(FORM, content)

	def test_page_never_shows_the_session_name(self):
		session = make_session()
		self.assertNotIn(session.name, self.render(session.token))

	def test_paid_session_shows_its_outcome_instead_of_the_form(self):
		session = make_session({"status": "Succeeded"}, status="Paid", redirect_to="/orders/42")
		content = self.render(session.token)

		self.assertIn("Payment received", content)
		self.assertIn("/orders/42", content)
		self.assertNotIn(FORM, content)

	def test_void_session_shows_its_outcome_instead_of_the_form(self):
		session = make_session(status="Void")
		content = self.render(session.token)

		self.assertIn("Cancelled", content)
		self.assertNotIn(FORM, content)

	def test_unknown_or_malformed_token_gets_a_not_found_page(self):
		for token in (UNKNOWN_TOKEN, *MALFORMED_TOKENS):
			with self.subTest(token=token):
				response = self.response(token)
				self.assertEqual(response.status_code, 404)
				self.assertNotIn("Traceback", frappe.safe_decode(response.get_data()))

	def test_opening_the_page_again_changes_nothing(self):
		session = make_session()
		self.render(session.token)
		self.render(session.token)

		session.reload()
		self.assertEqual(session.status, "Open")
		self.assertFalse(session.attempts)


class TestGetStatus(CheckoutTestCase):
	def get_status(self, session):
		return api.get_status(session.token)

	def test_unknown_or_malformed_token_is_refused_without_asking_the_provider(self):
		provider = FakeProvider()
		with patch.object(rc, "_provider_for", return_value=provider):
			for token in (UNKNOWN_TOKEN, *MALFORMED_TOKENS):
				with self.subTest(token=token), self.assertRaises(frappe.DoesNotExistError):
					api.get_status(token)
		self.assertFalse(provider.calls)

	def test_session_without_an_attempt_reports_open_and_asks_nothing(self):
		provider = FakeProvider()
		with patch.object(rc, "_provider_for", return_value=provider):
			state = self.get_status(make_session())

		self.assertEqual(state, {"status": "Open", "attempt_status": None, "redirect_url": None})
		self.assertFalse(provider.calls)

	def test_running_attempt_is_checked_once_per_minimum_interval(self):
		session = make_session({})
		provider = FakeProvider(ProviderResult("Pending"), ProviderResult("Pending"))
		with patch.object(rc, "_provider_for", return_value=provider):
			self.assertEqual(self.get_status(session)["attempt_status"], "Pending")
			# A second tab polling right away must not produce a second call.
			self.get_status(session)
			self.assertEqual(len(provider.calls), 1)

			frappe.db.set_value(
				"Local Payment Attempt",
				session.attempts[0].name,
				"last_checked_on",
				now_datetime() - rc.MIN_CHECK_INTERVAL - timedelta(seconds=1),
			)
			frappe.db.commit()
			self.get_status(session)

		self.assertEqual(len(provider.calls), 2)

	def test_settled_attempt_is_not_checked_again(self):
		session = make_session({"status": "Failed"})
		provider = FakeProvider()
		with patch.object(rc, "_provider_for", return_value=provider):
			state = self.get_status(session)

		self.assertEqual(state["attempt_status"], "Failed")
		self.assertFalse(provider.calls)

	def test_provider_failure_leaves_the_payer_with_the_last_known_state(self):
		session = make_session({})
		queue = "insert_queue_for_Error Log"
		before = frappe.cache.llen(queue)

		with patch.object(rc, "_provider_for", side_effect=Exception("gateway timeout")):
			state = self.get_status(session)

		self.assertEqual((state["status"], state["attempt_status"]), ("Open", "Pending"))
		# Logged out of band, because Frappe rolls a GET request back on its way out.
		self.assertEqual(frappe.cache.llen(queue), before + 1)
		frappe.cache.lpop(queue)

	def test_an_exit_url_that_is_not_a_web_address_is_dropped(self):
		session = make_session({"status": "Succeeded"}, status="Paid", redirect_to="javascript:alert(1)")
		self.assertIn("/payment-success?", self.get_status(session)["redirect_url"])

	def test_success_reports_paid_and_where_to_send_the_payer(self):
		session = make_session({})
		provider = FakeProvider(ProviderResult("Succeeded", Decimal("5000"), "XAF", "TX-1"))
		with patch.object(rc, "_provider_for", return_value=provider):
			state = self.get_status(session)

		self.assertEqual((state["status"], state["attempt_status"]), ("Paid", "Succeeded"))
		self.assertIn("/payment-success?doctype=User&docname=Administrator", state["redirect_url"])

	def test_exit_url_prefers_what_the_consumer_asked_for(self):
		cases = (
			({"success_redirect": "/thanks", "redirect_to": "/orders/42"}, "/thanks"),
			({"redirect_to": "/orders/42"}, "/orders/42"),
		)
		for fields, expected in cases:
			with self.subTest(**fields):
				session = make_session({"status": "Succeeded"}, status="Paid", **fields)
				self.assertEqual(self.get_status(session)["redirect_url"], expected)
