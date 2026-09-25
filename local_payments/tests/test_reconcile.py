# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import frappe
from frappe.database import get_db
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from local_payments import reconcile as rc
from local_payments.lifecycle import ProviderResult

GATEWAY = "Test Local Payment Gateway"
MANAGER = "lp-reconcile-manager@example.com"
EFFECT = "lp-reconcile-effect"
HOOK = "local_payments.tests.test_reconcile.consumer"
PROBE_HOOK = "local_payments.tests.test_reconcile.probe"

# What the consumer callback saw and how it should behave, set per test.
consumer_calls = []
consumer_state = {"fail": False, "commit_first": False}


def consumer(doc, method, *args, **kwargs):
	"""Stands in for a reference document's on_payment_authorized, registered as a doc_events hook."""
	consumer_calls.append((doc.name, args, frappe._dict(frappe.flags.data)))
	# A real effect in the database, so we can tell whether it was kept or rolled back.
	frappe.get_doc({"doctype": "ToDo", "description": EFFECT}).insert(ignore_permissions=True)
	if consumer_state["commit_first"]:
		frappe.db.commit()  # a misbehaving consumer: this drops reconcile's savepoint
	if consumer_state["fail"]:
		frappe.throw("Accounting period is closed")


# [session name, then what a second connection found while the consumer was running].
lock_probe = []


def probe(doc, method, *args, **kwargs):
	"""Consumer that checks, from a second connection, that the session row is locked."""
	other = get_db(
		socket=frappe.conf.db_socket,
		host=frappe.conf.db_host,
		port=frappe.conf.db_port,
		user=frappe.conf.db_user,
		password=frappe.conf.db_password,
		cur_db_name=frappe.conf.db_name,
	)
	other.connect()
	other.sql("set session innodb_lock_wait_timeout = 1")
	try:
		other.sql("select name from `tabLocal Payment` where name = %s for update", lock_probe[0])
		lock_probe.append("not locked")
	except frappe.QueryTimeoutError:
		lock_probe.append("locked")
	finally:
		other.close()


class FakeProvider:
	"""Returns canned results in order and remembers what it was asked."""

	def __init__(self, *results):
		self.results = list(results)
		self.calls = []

	def check_status(self, attempt_id, provider_data):
		self.calls.append(attempt_id)
		return self.results.pop(0)


def succeeded(amount="5000", currency="XAF"):
	return ProviderResult("Succeeded", Decimal(amount), currency, "TX-1")


def make_session(*attempts, status="Open", **extra):
	rows = [
		{
			"attempt_id": frappe.generate_hash(length=36),
			"status": "Pending",
			"expires_on": add_to_date(now_datetime(), hours=1),
			**attempt,
		}
		for attempt in attempts or [{}]
	]
	return frappe.get_doc(
		{
			"doctype": "Local Payment",
			"payment_gateway": GATEWAY,
			"reference_doctype": "User",
			"reference_docname": "Administrator",
			"amount": 5000,
			"currency": "XAF",
			"request_data": frappe.as_json({"order_id": "ORD-1", "title": "Invoice"}),
			"status": status,
			"attempts": rows,
			**extra,
		}
	).insert(ignore_permissions=True)


def reload(session):
	return frappe.get_doc("Local Payment", session.name)


def alerts_for(session):
	return frappe.get_all(
		"Notification Log", {"for_user": MANAGER, "document_name": session.name}, pluck="name"
	)


class TestReconcile(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("Currency", "XAF"):
			frappe.get_doc({"doctype": "Currency", "currency_name": "XAF", "enabled": 1}).insert()
		if not frappe.db.exists("Payment Gateway", GATEWAY):
			frappe.get_doc({"doctype": "Payment Gateway", "gateway": GATEWAY}).insert()
		if not frappe.db.exists("User", MANAGER):
			user = frappe.get_doc(
				{"doctype": "User", "email": MANAGER, "first_name": "LP Reconcile"}
			).insert()
			user.add_roles(rc.MANAGER_ROLE)
		frappe.db.commit()

	def setUp(self):
		consumer_calls.clear()
		consumer_state.update(fail=False, commit_first=False)
		hooks = patch("frappe.get_doc_hooks", return_value={"User": {"on_payment_authorized": [HOOK]}})
		hooks.start()
		self.addCleanup(hooks.stop)
		self.addCleanup(self.clean_up)

	def clean_up(self):
		# reconcile() commits, so the class-level rollback can't undo what these tests created.
		frappe.db.rollback()
		frappe.db.delete("ToDo", {"description": EFFECT})
		frappe.db.delete("Notification Log", {"for_user": MANAGER})
		frappe.db.delete("Local Payment Attempt", {"parenttype": "Local Payment"})
		frappe.db.delete("Local Payment", {"payment_gateway": GATEWAY})
		frappe.db.commit()

	def reconcile(self, session, provider, attempt=0):
		return rc.reconcile(session.attempts[attempt].attempt_id, provider)

	def test_success_records_payment_then_authorizes_the_consumer(self):
		session = make_session()
		provider = FakeProvider(succeeded())
		self.reconcile(session, provider)

		session = reload(session)
		attempt = session.attempts[0]
		self.assertEqual((session.status, session.authorization), ("Paid", "Done"))
		self.assertEqual((session.paid_amount, session.provider_transaction_id), (5000, "TX-1"))
		self.assertEqual(attempt.status, "Succeeded")
		self.assertIsNone(attempt.next_check_on)  # final attempts are not polled again
		self.assertEqual((attempt.confirmed_amount, attempt.confirmed_currency), (5000, "XAF"))

		# The consumer ran once, saw the attempt id as order_id, and its effects were kept.
		((name, args, data),) = consumer_calls
		self.assertEqual((name, args), ("Administrator", ("Completed",)))
		self.assertEqual(data.order_id, attempt.attempt_id)
		self.assertEqual((data.payment_gateway, data.provider_transaction_id), (GATEWAY, "TX-1"))
		self.assertEqual(data.title, "Invoice")
		self.assertTrue(frappe.db.exists("ToDo", {"description": EFFECT}))
		self.assertIsNone(frappe.flags.data)

	def test_provider_status_is_kept_on_the_attempt(self):
		session = make_session()
		self.reconcile(
			session, FakeProvider(ProviderResult("Failed", provider_status="FAILED: NOT_ENOUGH_FUNDS"))
		)

		attempt = reload(session).attempts[0]
		self.assertEqual((attempt.status, attempt.provider_status), ("Failed", "FAILED: NOT_ENOUGH_FUNDS"))

	def test_consumer_runs_under_a_row_lock_on_the_session(self):
		session = make_session()
		lock_probe.clear()
		lock_probe.append(session.name)
		with patch("frappe.get_doc_hooks", return_value={"User": {"on_payment_authorized": [PROBE_HOOK]}}):
			self.reconcile(session, FakeProvider(succeeded()))
		self.assertEqual(lock_probe[1:], ["locked"], reload(session).authorization_error)

	def test_failing_consumer_is_rolled_back_but_payment_stays_recorded(self):
		consumer_state["fail"] = True
		session = make_session()
		self.reconcile(session, FakeProvider(succeeded()))

		session = reload(session)
		self.assertEqual(session.status, "Paid")
		self.assertEqual(session.authorization, "Failed")
		self.assertEqual(session.authorization_tries, 1)
		self.assertIn("Accounting period is closed", session.authorization_error)
		self.assertFalse(frappe.db.exists("ToDo", {"description": EFFECT}))

	def test_failed_authorization_can_be_retried_without_asking_the_provider(self):
		consumer_state["fail"] = True
		session = make_session()
		self.reconcile(session, FakeProvider(succeeded()))

		consumer_state["fail"] = False
		self.assertTrue(rc.authorize(session.name))
		session = reload(session)
		self.assertEqual((session.status, session.authorization), ("Paid", "Done"))
		self.assertEqual(session.authorization_tries, 1)
		self.assertIsNone(session.authorization_error)
		self.assertEqual(len(consumer_calls), 2)

	def test_second_reconcile_on_paid_and_authorized_session_does_nothing(self):
		session = make_session()
		provider = FakeProvider(succeeded(), succeeded())
		self.reconcile(session, provider)
		self.assertIsNone(self.reconcile(session, provider))
		self.assertTrue(rc.authorize(session.name))  # already Done: reports success, runs nothing
		self.assertEqual((len(provider.calls), len(consumer_calls)), (1, 1))

	def test_check_requested_too_soon_is_a_noop(self):
		session = make_session()
		provider = FakeProvider(ProviderResult("Pending"), ProviderResult("Pending"), succeeded())
		self.reconcile(session, provider)
		self.assertIsNone(self.reconcile(session, provider))
		self.assertEqual(len(provider.calls), 1)

		# Once the interval has passed, the attempt is checked again.
		frappe.db.set_value(
			"Local Payment Attempt",
			session.attempts[0].name,
			"last_checked_on",
			now_datetime() - rc.MIN_CHECK_INTERVAL - timedelta(seconds=1),
		)
		frappe.db.commit()
		self.reconcile(session, provider)
		self.assertEqual(len(provider.calls), 2)
		self.assertEqual(reload(session).attempts[0].check_count, 2)

	def test_duplicate_success_alerts_and_never_runs_the_consumer_again(self):
		session = make_session({"status": "Succeeded"}, {}, status="Paid", authorization="Done")
		self.reconcile(session, FakeProvider(succeeded()), attempt=1)

		session = reload(session)
		self.assertTrue(session.attempts[1].duplicate)
		self.assertEqual((session.status, session.authorization), ("Paid", "Done"))
		self.assertEqual(len(alerts_for(session)), 1)
		self.assertFalse(consumer_calls)

	def test_amount_or_currency_mismatch_alerts_and_keeps_session_open(self):
		for result in (succeeded(amount="4000"), succeeded(currency="EUR")):
			with self.subTest(result=result):
				session = make_session()
				self.reconcile(session, FakeProvider(result))

				session = reload(session)
				self.assertTrue(session.attempts[0].amount_mismatch)
				self.assertEqual(session.attempts[0].status, "Succeeded")
				self.assertEqual((session.status, session.authorization), ("Open", ""))
				self.assertEqual(len(alerts_for(session)), 1)
		self.assertFalse(consumer_calls)

	def test_pending_past_local_deadline_becomes_unresolved_and_late_success_is_applied(self):
		session = make_session({"expires_on": add_to_date(now_datetime(), minutes=-1)})
		self.reconcile(session, FakeProvider(ProviderResult("Pending")))
		self.assertEqual(reload(session).attempts[0].status, "Unresolved")

		frappe.db.set_value("Local Payment Attempt", session.attempts[0].name, "last_checked_on", None)
		frappe.db.commit()
		self.reconcile(session, FakeProvider(succeeded()))
		session = reload(session)
		self.assertEqual((session.status, session.authorization), ("Paid", "Done"))

	def test_late_success_on_pending_attempt_past_its_deadline_is_applied(self):
		session = make_session({"expires_on": add_to_date(now_datetime(), minutes=-1)})
		self.reconcile(session, FakeProvider(succeeded()))
		session = reload(session)
		self.assertEqual((session.attempts[0].status, session.status), ("Succeeded", "Paid"))
		self.assertEqual(session.authorization, "Done")

	def test_consumer_that_commits_still_gets_a_failure_recorded(self):
		consumer_state.update(fail=True, commit_first=True)
		session = make_session()
		self.reconcile(session, FakeProvider(succeeded()))

		session = reload(session)
		self.assertEqual((session.status, session.authorization), ("Paid", "Failed"))
		self.assertEqual(session.authorization_tries, 1)
		self.assertIn("Accounting period is closed", session.authorization_error)

	def test_failing_alert_does_not_break_reconciliation(self):
		session = make_session({"status": "Succeeded"}, {}, status="Paid", authorization="Done")
		with patch("local_payments.reconcile.get_users_with_role", side_effect=Exception("mail down")):
			resolution = self.reconcile(session, FakeProvider(succeeded()), attempt=1)
		self.assertTrue(resolution.duplicate)
		self.assertTrue(reload(session).attempts[1].duplicate)

	def test_unknown_attempt_is_ignored_without_calling_the_provider(self):
		provider = FakeProvider()
		self.assertIsNone(rc.reconcile("does-not-exist", provider))
		self.assertFalse(provider.calls)

	def test_success_settled_by_a_concurrent_check_is_not_applied_twice(self):
		session = make_session()

		class SlowProvider(FakeProvider):
			def check_status(self, attempt_id, provider_data):
				# While we wait for the provider, another trigger settles the same attempt.
				frappe.db.set_value("Local Payment Attempt", session.attempts[0].name, "status", "Failed")
				frappe.db.commit()
				return super().check_status(attempt_id, provider_data)

		self.assertIsNone(self.reconcile(session, SlowProvider(succeeded())))
		self.assertEqual(reload(session).status, "Open")


class TestSchedulingPolicy(IntegrationTestCase):
	"""reconcile() owns the dates the scheduler reads back (ARCHITECTURE, "Scheduled tasks")."""

	def test_failed_authorization_backoff_doubles_then_is_capped(self):
		hours = [rc.authorization_retry_delay(rc.AUTH_FAILED, n) / timedelta(hours=1) for n in range(10)]
		self.assertEqual(hours, [1, 1, 2, 4, 8, 16, 24, 24, 24, 24])

	def test_pending_authorization_only_gets_a_short_grace(self):
		self.assertEqual(rc.authorization_retry_delay(rc.AUTH_PENDING, 0), rc.PENDING_AUTHORIZATION_GRACE)

	def test_unresolved_polling_slows_down_with_age(self):
		ladder = [rc.unresolved_poll_interval(timedelta(hours=age)) for age in (0, 5, 6, 23, 24, 71)]
		self.assertEqual([i / timedelta(hours=1) for i in ladder], [1, 1, 4, 4, 12, 12])

	def test_every_retry_fits_well_before_the_daily_alert(self):
		tries = range(1, rc.MAX_AUTHORIZATION_TRIES)
		total = sum((rc.authorization_retry_delay(rc.AUTH_FAILED, n) for n in tries), timedelta())
		self.assertLess(total, timedelta(days=1))

	def test_an_authorization_that_used_up_its_tries_gets_no_retry_date(self):
		session = frappe._dict(authorization=rc.AUTH_FAILED, authorization_tries=rc.MAX_AUTHORIZATION_TRIES)
		self.assertIsNone(rc._next_authorization_retry(now_datetime(), session))

	def test_a_settled_attempt_gets_no_next_check(self):
		for status in ("Succeeded", "Failed", "Expired", "Error"):
			with self.subTest(status=status):
				row = frappe._dict(status=status, expires_on=None)
				self.assertIsNone(rc._next_attempt_check(now_datetime(), row))

	def test_an_unresolved_attempt_is_rescheduled_on_the_decreasing_ladder(self):
		now = now_datetime()
		row = frappe._dict(status="Unresolved", expires_on=add_to_date(now, hours=-30))
		self.assertEqual(rc._next_attempt_check(now, row), now + timedelta(hours=12))
