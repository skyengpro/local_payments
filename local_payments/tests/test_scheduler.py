# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from local_payments import lifecycle as lc
from local_payments import reconcile as rc
from local_payments import scheduler as sch
from local_payments.tests.test_reconcile import GATEWAY, MANAGER, make_session


def ago(**delta):
	return add_to_date(now_datetime(), **{key: -value for key, value in delta.items()})


def attempt_ids(session):
	return {row.attempt_id for row in session.attempts}


def paid_session(authorization, tries=0, retry_on=None, **extra):
	"""A Paid session waiting on its consumer callback. `retry_on` is the date the scheduler reads."""
	return make_session(
		{"status": "Succeeded"},
		status="Paid",
		authorization=authorization,
		authorization_tries=tries,
		authorization_next_retry_on=retry_on,
		**extra,
	)


class TestScheduler(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("Currency", "XAF"):
			frappe.get_doc({"doctype": "Currency", "currency_name": "XAF", "enabled": 1}).insert()
		if not frappe.db.exists("Payment Gateway", GATEWAY):
			frappe.get_doc({"doctype": "Payment Gateway", "gateway": GATEWAY}).insert()
		if not frappe.db.exists("User", MANAGER):
			user = frappe.get_doc({"doctype": "User", "email": MANAGER, "first_name": "LP Sched"}).insert()
			user.add_roles(rc.MANAGER_ROLE)
		frappe.db.commit()

	def setUp(self):
		self.addCleanup(self.clean_up)

	def clean_up(self):
		# alert_managers() commits, so the class-level rollback can't undo what these tests created.
		frappe.db.rollback()
		frappe.db.delete("Notification Log", {"for_user": MANAGER})
		frappe.db.delete("Error Log", {"method": ["like", "Local Payment scheduler failed%"]})
		frappe.db.delete("Local Payment Attempt", {"parenttype": "Local Payment"})
		frappe.db.delete("Local Payment", {"payment_gateway": GATEWAY})
		frappe.db.commit()

	def picked(self, job, target):
		"""Run a job with `target` (reconcile or authorize) replaced, and return what it was called with."""
		with patch(f"local_payments.reconcile.{target}") as mock:
			job()
		return [call.args[0] for call in mock.call_args_list]

	def alerts(self, session):
		return frappe.get_all(
			"Notification Log", {"for_user": MANAGER, "document_name": session.name}, pluck="subject"
		)

	# every 2 minutes

	def test_due_initiated_and_pending_attempts_are_checked(self):
		session = make_session(
			{"status": "Initiated", "next_check_on": ago(minutes=1)},
			{"status": "Pending", "next_check_on": ago(seconds=5)},
			{"status": "Pending", "next_check_on": add_to_date(now_datetime(), minutes=5)},
			{"status": "Succeeded", "next_check_on": ago(minutes=1)},
			{"status": "Unresolved", "next_check_on": ago(minutes=1)},
			{"status": "Failed", "next_check_on": ago(minutes=1)},
		)
		ids = [row.attempt_id for row in session.attempts]

		picked = self.picked(sch.check_due_attempts, "reconcile")

		self.assertCountEqual(picked, [ids[0], ids[1]])

	def test_one_failing_attempt_does_not_stop_the_others(self):
		session = make_session(
			{"next_check_on": ago(minutes=3)},
			{"next_check_on": ago(minutes=2)},
			{"next_check_on": ago(minutes=1)},
		)
		bad = session.attempts[1].attempt_id

		seen = []

		def flaky(attempt_id):
			seen.append(attempt_id)
			if attempt_id == bad:
				raise Exception("provider down")

		with patch("local_payments.reconcile.reconcile", side_effect=flaky):
			sch.check_due_attempts()

		self.assertCountEqual(seen, attempt_ids(session))
		self.assertTrue(frappe.db.exists("Error Log", {"method": ["like", f"%{bad}%"]}))

	def test_a_run_is_bounded_by_the_batch_size(self):
		make_session(*[{"next_check_on": ago(minutes=1)}] * 4)
		with patch.object(sch, "BATCH_SIZE", 3):
			self.assertEqual(len(self.picked(sch.check_due_attempts, "reconcile")), 3)

	def test_a_run_stops_when_its_time_budget_is_spent(self):
		make_session({"next_check_on": ago(minutes=1)})
		with patch.object(sch, "TIME_BUDGET", timedelta(seconds=-1)):
			self.assertEqual(self.picked(sch.check_due_attempts, "reconcile"), [])

	def test_the_longest_overdue_attempt_is_checked_first(self):
		recent = make_session({"next_check_on": ago(minutes=1)})
		overdue = make_session({"next_check_on": ago(hours=1)})
		with patch.object(sch, "BATCH_SIZE", 1):
			picked = self.picked(sch.check_due_attempts, "reconcile")
		self.assertEqual(picked, [overdue.attempts[0].attempt_id])
		self.assertNotIn(recent.attempts[0].attempt_id, picked)

	def test_an_attempt_that_was_never_scheduled_is_still_picked_up(self):
		session = make_session({"status": "Pending", "next_check_on": None})
		self.assertEqual(self.picked(sch.check_due_attempts, "reconcile"), [session.attempts[0].attempt_id])

	def test_an_authorization_that_gave_up_is_never_retried(self):
		"""No retry date must mean "never", not "due since the beginning of time"."""
		paid_session(rc.AUTH_FAILED, tries=rc.MAX_AUTHORIZATION_TRIES, retry_on=None)
		self.assertEqual(self.picked(sch.retry_authorizations, "authorize"), [])

	# hourly: Unresolved attempts

	def test_unresolved_attempts_are_rechecked_when_their_next_check_is_due(self):
		session = make_session(
			{"status": "Unresolved", "expires_on": ago(hours=2), "next_check_on": ago(minutes=1)},
			{"status": "Unresolved", "expires_on": ago(hours=2), "next_check_on": ago(hours=9)},
			# Not due yet.
			{
				"status": "Unresolved",
				"expires_on": ago(hours=2),
				"next_check_on": add_to_date(now_datetime(), hours=1),
			},
			# Past 72 h: polling stops, the daily alert takes over.
			{"status": "Unresolved", "expires_on": ago(hours=73), "next_check_on": ago(hours=1)},
			# Not Unresolved: the 2-minute job owns these.
			{"status": "Pending", "expires_on": ago(hours=2), "next_check_on": ago(hours=1)},
		)
		ids = [row.attempt_id for row in session.attempts]

		picked = self.picked(sch.check_unresolved_attempts, "reconcile")

		self.assertCountEqual(picked, [ids[0], ids[1]])

	def test_a_due_unresolved_attempt_is_never_crowded_out_by_ones_that_are_not_due(self):
		"""Selecting and filtering are one query: a batch of not-due rows must not hide a due one."""
		later = add_to_date(now_datetime(), hours=5)
		not_due = {"status": "Unresolved", "expires_on": ago(hours=30), "next_check_on": later}
		make_session(*[not_due] * 3)
		due = make_session(
			{"status": "Unresolved", "expires_on": ago(hours=2), "next_check_on": ago(minutes=1)}
		)

		with patch.object(sch, "BATCH_SIZE", 3):
			picked = self.picked(sch.check_unresolved_attempts, "reconcile")

		self.assertEqual(picked, [due.attempts[0].attempt_id])

	# hourly: authorizations

	def test_pending_and_failed_authorizations_are_retried_once_their_delay_has_passed(self):
		due = [
			paid_session(rc.AUTH_PENDING, retry_on=ago(minutes=1)),
			paid_session(rc.AUTH_FAILED, tries=1, retry_on=ago(minutes=1)),
			paid_session(rc.AUTH_FAILED, tries=3, retry_on=ago(hours=5)),
		]
		not_due = [
			paid_session(rc.AUTH_PENDING, retry_on=add_to_date(now_datetime(), minutes=4)),
			paid_session(rc.AUTH_FAILED, tries=1, retry_on=add_to_date(now_datetime(), minutes=30)),
			# Given up: reconcile() leaves no retry date, so the daily alert takes over.
			paid_session(rc.AUTH_FAILED, tries=rc.MAX_AUTHORIZATION_TRIES, retry_on=None),
			paid_session(rc.AUTH_DONE, retry_on=None),
			paid_session("", retry_on=None),
		]
		make_session({"status": "Pending"})  # an Open session has nothing to authorize

		picked = self.picked(sch.retry_authorizations, "authorize")

		self.assertCountEqual(picked, [session.name for session in due])
		for session in not_due:
			self.assertNotIn(session.name, picked)

	def test_a_due_authorization_is_never_crowded_out_by_ones_that_are_not_due(self):
		soon = add_to_date(now_datetime(), hours=5)
		for _ in range(3):
			paid_session(rc.AUTH_FAILED, tries=4, retry_on=soon)
		due = paid_session(rc.AUTH_FAILED, tries=1, retry_on=ago(minutes=1))

		with patch.object(sch, "BATCH_SIZE", 3):
			picked = self.picked(sch.retry_authorizations, "authorize")

		self.assertEqual(picked, [due.name])

	def test_one_failing_authorization_does_not_stop_the_others(self):
		sessions = [paid_session(rc.AUTH_FAILED, tries=1, retry_on=ago(minutes=1)) for _ in range(3)]
		seen = []

		def flaky(name):
			seen.append(name)
			if name == sessions[0].name:
				raise Exception("database hiccup")

		with patch("local_payments.reconcile.authorize", side_effect=flaky):
			sch.retry_authorizations()

		self.assertCountEqual(seen, [session.name for session in sessions])

	# daily alerts

	def test_unresolved_over_72_hours_alerts_managers_once(self):
		stale = make_session({"status": "Unresolved", "expires_on": ago(hours=80)})
		recent = make_session({"status": "Unresolved", "expires_on": ago(hours=70)})
		paid_meanwhile = make_session(
			{"status": "Unresolved", "expires_on": ago(hours=80)}, {"status": "Succeeded"}, status="Paid"
		)

		sch.send_daily_alerts()
		sch.send_daily_alerts()

		self.assertEqual(len(self.alerts(stale)), 1)
		self.assertIn(stale.attempts[0].attempt_id, self.alerts(stale)[0])
		self.assertEqual(self.alerts(recent), [])
		self.assertEqual(self.alerts(paid_meanwhile), [])

	def test_authorization_still_failing_after_the_last_try_alerts_managers_once(self):
		exhausted = paid_session(rc.AUTH_FAILED, tries=rc.MAX_AUTHORIZATION_TRIES)
		retrying = paid_session(rc.AUTH_FAILED, tries=rc.MAX_AUTHORIZATION_TRIES - 1)

		sch.send_daily_alerts()
		sch.send_daily_alerts()

		self.assertEqual(len(self.alerts(exhausted)), 1)
		self.assertEqual(self.alerts(retrying), [])

	def test_a_failing_alert_does_not_stop_the_other_alerts(self):
		stale = make_session({"status": "Unresolved", "expires_on": ago(hours=80)})
		exhausted = paid_session(rc.AUTH_FAILED, tries=rc.MAX_AUTHORIZATION_TRIES)
		real = rc.alert_managers
		# A failing alert rolls back, which would take the seeded rows with it. clean_up() removes them.
		frappe.db.commit()

		def flaky(session_name, detail, reason):
			if reason == "unresolved_timeout":
				raise Exception("mail down")
			real(session_name, detail, reason)

		with patch("local_payments.reconcile.alert_managers", side_effect=flaky):
			sch.send_daily_alerts()

		self.assertEqual(self.alerts(stale), [])
		self.assertEqual(len(self.alerts(exhausted)), 1)

	# wiring

	def test_scheduler_events_point_at_the_jobs(self):
		events = frappe.get_hooks("scheduler_events", app_name="local_payments")
		wired = {
			*events["cron"]["*/2 * * * *"],
			*events["hourly"],
			*events["daily"],
		}
		self.assertEqual(
			wired,
			{
				"local_payments.scheduler.check_due_attempts",
				"local_payments.scheduler.check_unresolved_attempts",
				"local_payments.scheduler.retry_authorizations",
				"local_payments.scheduler.send_daily_alerts",
			},
		)
		for path in wired:
			self.assertTrue(callable(frappe.get_attr(path)))
