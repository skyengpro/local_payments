# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

from contextlib import contextmanager
from decimal import Decimal
from unittest import SkipTest
from unittest.mock import patch

import frappe
from frappe.database import get_db
from frappe.tests import IntegrationTestCase
from frappe.utils import nowdate

from local_payments import api
from local_payments import erpnext as ep
from local_payments import reconcile as rc
from local_payments.lifecycle import ProviderResult
from local_payments.tests.test_reconcile import FakeProvider, make_session

GATEWAY_NAME = "lp-erpnext"
GATEWAY = f"MTN MoMo-{GATEWAY_NAME}"
# ERPNext's own test gateway: a gateway that is not ours.
OTHER_GATEWAY = "_Test Gateway"
COMPANY = "_Test Company"
BANK = "_Test Bank - _TC"
CLOSED_PERIOD = "_Test LP Closed Period"
# ERPNext names the period after it and the company's abbreviation.
CLOSED_PERIOD_NAME = f"{CLOSED_PERIOD} - _TC"
PAYMENT_REQUEST = "erpnext.accounts.doctype.payment_request.payment_request"


@contextmanager
def locked_elsewhere(session_name):
	"""Hold a lock on a session row from a second connection, as a running authorization would."""
	other = get_db(
		socket=frappe.conf.db_socket,
		host=frappe.conf.db_host,
		port=frappe.conf.db_port,
		user=frappe.conf.db_user,
		password=frappe.conf.db_password,
		cur_db_name=frappe.conf.db_name,
	)
	other.connect()
	try:
		other.begin()
		other.sql("select name from `tabLocal Payment` where name = %s for update", session_name)
		yield
	finally:
		other.rollback()
		other.close()


class TestHookPaths(IntegrationTestCase):
	def test_payment_request_handlers_load_on_any_site(self):
		# erpnext.py imports nothing from ERPNext, so this holds with or without it.
		handlers = frappe.get_doc_hooks()["Payment Request"]
		self.assertEqual(set(handlers), {"on_payment_authorized", "on_cancel"})
		for paths in handlers.values():
			for path in paths:
				self.assertTrue(callable(frappe.get_attr(path)))


class TestAsAdministrator(IntegrationTestCase):
	def test_puts_the_caller_back_after_the_block(self):
		with self.set_user("Guest"):
			frappe.local.form_dict = frappe._dict(token="t")
			session = frappe.local.session
			with ep.as_administrator():
				self.assertEqual(frappe.session.user, "Administrator")
			self.assertIs(frappe.local.session, session)
			self.assertEqual((frappe.session.user, frappe.session.sid), ("Guest", "Guest"))
			self.assertEqual(frappe.local.form_dict, {"token": "t"})

	def test_restores_everything_set_user_replaces(self):
		before = dict(iter(frappe.local))
		frappe.set_user("Guest")
		replaced = {name for name, value in iter(frappe.local) if before.get(name) is not value}
		frappe.set_user("Administrator")
		self.assertLessEqual(replaced, {*ep.USER_STATE, "cache"})

	def test_leaves_a_fresh_cache(self):
		frappe.local.cache["lp-probe"] = "caller"
		with ep.as_administrator():
			frappe.local.cache["lp-probe"] = "administrator"
		self.assertNotIn("lp-probe", frappe.local.cache)

	def test_puts_the_caller_back_when_the_block_raises(self):
		with self.set_user("Guest"):
			with self.assertRaises(frappe.ValidationError), ep.as_administrator():
				frappe.throw("refused")
			self.assertEqual(frappe.session.user, "Guest")


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
		if not frappe.db.exists("Payment Gateway", OTHER_GATEWAY):
			frappe.get_doc({"doctype": "Payment Gateway", "gateway": OTHER_GATEWAY}).insert()
		cls.gateway_account = cls.make_gateway_account(GATEWAY)
		cls.other_gateway_account = cls.make_gateway_account(OTHER_GATEWAY)
		frappe.db.commit()

	@staticmethod
	def make_gateway_account(gateway):
		account = {"payment_gateway": gateway, "currency": "INR", "company": COMPANY}
		return frappe.db.get_value("Payment Gateway Account", account) or (
			frappe.get_doc({"doctype": "Payment Gateway Account", "payment_account": BANK, **account})
			.insert(ignore_permissions=True)
			.name
		)

	def setUp(self):
		self.created = []
		self.addCleanup(self.clean_up)

	def clean_up(self):
		# authorize() commits, so the class rollback can't undo what these tests created.
		frappe.set_user("Administrator")
		frappe.db.rollback()
		if sessions := frappe.get_all("Local Payment", {"payment_gateway": GATEWAY}, pluck="name"):
			frappe.db.delete(
				"Local Payment Attempt", {"parenttype": "Local Payment", "parent": ["in", sessions]}
			)
			frappe.db.delete("Local Payment", {"name": ["in", sessions]})
		requests = [name for doctype, name in self.created if doctype == "Payment Request"]
		entries = frappe.get_all("Payment Entry", {"reference_no": ["in", requests]}, pluck="name")
		for doctype, name in [("Payment Entry", e) for e in entries] + self.created[::-1]:
			doc = frappe.get_doc(doctype, name)
			if doc.docstatus == 1:
				doc.cancel()
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
		# Cancelling keeps the entries' ledger rows, marked cancelled.
		for ledger in ("GL Entry", "Payment Ledger Entry"):
			frappe.db.delete(ledger, {"voucher_type": "Payment Entry", "voucher_no": ["in", entries]})
		if frappe.db.exists("Accounting Period", CLOSED_PERIOD_NAME):
			frappe.delete_doc("Accounting Period", CLOSED_PERIOD_NAME, force=True)
		frappe.db.commit()

	def payment_request(self, gateway_account=None):
		from erpnext.accounts.doctype.payment_request.payment_request import make_payment_request
		from erpnext.selling.doctype.sales_order.test_sales_order import make_sales_order

		with (
			patch(
				f"{PAYMENT_REQUEST}.PaymentRequest.get_payment_url", return_value="https://example.com/pay"
			),
			patch(f"{PAYMENT_REQUEST}.PaymentRequest.send_email"),
		):
			order = make_sales_order(currency="INR")
			self.created.append(("Sales Order", order.name))
			request = make_payment_request(
				dt="Sales Order",
				dn=order.name,
				mute_email=1,
				payment_gateway_account=gateway_account or self.gateway_account,
				submit_doc=1,
				return_doc=1,
			)
			self.created.append(("Payment Request", request.name))
		frappe.db.commit()
		return request

	def session(self, request, status="Paid", authorization=None):
		"""A session on the request: Paid with its Succeeded attempt, or Open with one still Pending."""
		paid = status == "Paid"
		attempt = "Succeeded" if paid else "Pending"
		if authorization is None:
			authorization = "Pending" if paid else ""
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
				"attempts": [{"attempt_id": frappe.generate_hash(length=36), "status": attempt}],
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		return session

	def paid_session(self, **kwargs):
		request = self.payment_request()
		return self.session(request, **kwargs), request

	def succeeded(self, request):
		return ProviderResult("Succeeded", Decimal(str(request.grand_total)), request.currency, "TX-1")

	def close_period_for_payment_entries(self):
		period = frappe.new_doc("Accounting Period")
		period.update(
			{"period_name": CLOSED_PERIOD, "company": COMPANY, "start_date": nowdate(), "end_date": nowdate()}
		)
		period.append("closed_documents", {"document_type": "Payment Entry", "closed": 1})
		period.insert()
		frappe.db.commit()
		return period

	def entries(self, request):
		return frappe.get_all("Payment Entry", {"reference_no": request.name, "docstatus": 1}, ["paid_to"])

	def assert_settled(self, session, request):
		session = frappe.get_doc("Local Payment", session.name)
		self.assertEqual(session.authorization, "Done", session.authorization_error)
		self.assertEqual(frappe.db.get_value("Payment Request", request.name, "status"), "Paid")
		self.assertEqual([e.paid_to for e in self.entries(request)], [BANK])

	def test_request_paid_from_the_payment_page_is_settled(self):
		request = self.payment_request()
		session = self.session(request, status="Open")
		with self.set_user("Guest"):
			rc.reconcile(session.attempts[0].attempt_id, FakeProvider(self.succeeded(request)))
			self.assertEqual(frappe.session.user, "Guest")
		self.assert_settled(session, request)

	def test_settles_as_administrator(self):
		session, request = self.paid_session()
		rc.authorize(session.name)
		self.assert_settled(session, request)

	def test_request_on_another_gateway_is_left_as_it_is(self):
		request = self.payment_request(self.other_gateway_account)
		request.run_method("on_payment_authorized", "Completed")
		self.assertEqual(frappe.db.get_value("Payment Request", request.name, "status"), "Requested")
		self.assertEqual(self.entries(request), [])

	def test_request_without_a_paid_session_is_not_settled(self):
		request = self.payment_request()
		self.session(request, status="Open")
		request.run_method("on_payment_authorized", "Completed")
		self.assertEqual(frappe.db.get_value("Payment Request", request.name, "status"), "Requested")
		self.assertEqual(self.entries(request), [])

	def test_settling_again_never_books_a_second_entry(self):
		session, request = self.paid_session()
		self.assertTrue(rc.authorize(session.name))
		self.assertTrue(rc.authorize(session.name))
		# The request in memory still says Requested: only the locked read can tell it is paid.
		request.run_method("on_payment_authorized", "Completed")
		self.assert_settled(session, request)

	def test_request_settled_by_erpnext_itself_is_not_settled_again(self):
		from erpnext.accounts.doctype.payment_request.payment_request import PaymentRequest

		# What a later ERPNext could do: settle in its own method, which runs before our hook.
		def upstream(self, status):
			self.set_as_paid()

		session, request = self.paid_session()
		with patch.object(PaymentRequest, "on_payment_authorized", upstream, create=True):
			rc.authorize(session.name)
		self.assert_settled(session, request)

	def test_refused_entry_keeps_the_payment_and_settles_once_fixed(self):
		session, request = self.paid_session()
		period = self.close_period_for_payment_entries()

		self.assertFalse(rc.authorize(session.name))
		failed = frappe.get_doc("Local Payment", session.name)
		self.assertEqual((failed.status, failed.authorization), ("Paid", "Failed"))
		self.assertIn(period.name, failed.authorization_error)
		self.assertTrue(frappe.db.exists("Error Log", {"reference_name": session.name}))
		self.assertEqual(frappe.db.get_value("Payment Request", request.name, "status"), "Requested")
		self.assertEqual(self.entries(request), [])

		frappe.delete_doc("Accounting Period", period.name)
		frappe.db.commit()
		self.assertTrue(rc.authorize(session.name))
		self.assert_settled(session, request)

	def test_refused_entry_shows_nothing_to_the_payer(self):
		request = self.payment_request()
		session = self.session(request, status="Open")
		self.close_period_for_payment_entries()
		frappe.clear_messages()

		provider = FakeProvider(self.succeeded(request))
		with self.set_user("Guest"), patch.object(rc, "_provider_for", return_value=provider):
			state = api.get_status(session.token)

		self.assertEqual(state["status"], "Paid")
		# Frappe turns message_log into the response's _server_messages, which the page would show.
		self.assertEqual(frappe.local.message_log, [])
		self.assertEqual(frappe.db.get_value("Local Payment", session.name, "authorization"), "Failed")

	def test_cancelling_the_request_closes_its_payment_page(self):
		request = self.payment_request()
		session = self.session(request, status="Open")
		request.reload().cancel()
		frappe.db.commit()
		self.assertEqual(frappe.db.get_value("Local Payment", session.name, "status"), "Void")

		with self.set_user("Guest"), self.assertRaises(frappe.ValidationError):
			api.start_attempt(session.token, "677000000")

	def test_cancel_is_refused_while_a_payment_is_not_booked(self):
		session, request = self.paid_session(authorization="Failed")
		with self.assertRaisesRegex(frappe.ValidationError, session.name):
			request.reload().cancel()
		# What the request handler does with the error: the whole cancel is undone.
		frappe.db.rollback()
		self.assertEqual(frappe.db.get_value("Payment Request", request.name, "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Local Payment", session.name, "status"), "Paid")

	def test_cancel_during_an_authorization_is_refused_at_once(self):
		request = self.payment_request()
		session = self.session(request, status="Open")
		with (
			locked_elsewhere(session.name),
			self.assertRaisesRegex(frappe.ValidationError, "being processed"),
		):
			request.reload().cancel()
		frappe.db.rollback()
		self.assertEqual(frappe.db.get_value("Payment Request", request.name, "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Local Payment", session.name, "status"), "Open")

	def test_cancel_is_not_held_up_by_other_requests_sessions(self):
		request = self.payment_request()
		session = self.session(request, status="Open")
		unrelated = make_session(payment_gateway=GATEWAY)
		frappe.db.commit()
		with locked_elsewhere(unrelated.name):
			request.reload().cancel()
		self.assertEqual(frappe.db.get_value("Local Payment", session.name, "status"), "Void")
