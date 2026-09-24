# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

"""Scheduled triggers (ARCHITECTURE, "Scheduled tasks").

Thin wrappers around `reconcile`: each job selects what is due, then hands it over one item at a time.
Nothing here decides an outcome (D3) or commits a payment (D4).

Due dates are not recomputed here. `reconcile()` writes `next_check_on` and `authorization_next_retry_on`
every time it records an outcome, so each job is one comparison on one indexed column. Selecting and
filtering must stay the same thing: a job that fetched a batch and then dropped the items that are not
due yet would let items that are never due crowd out the ones that are.

Every job is bounded by `BATCH_SIZE` and `TIME_BUDGET`, so a slow provider cannot push a run past the
queue timeout. What is left over is picked up by the next run. One failing item never stops the others.
"""

from collections.abc import Callable, Iterable
from datetime import timedelta

import frappe
from frappe.utils import now_datetime

from local_payments import lifecycle as lc
from local_payments import reconcile as rc

BATCH_SIZE = 25
# Cron and hourly jobs run on the default queue, which kills a job after 300 s
# (frappe/core/doctype/scheduled_job_type/scheduled_job_type.py, get_queue_name).
# One provider call can still start just under the wire, so leave it room to finish.
TIME_BUDGET = timedelta(seconds=240)

ATTEMPT = "Local Payment Attempt"
SESSION = "Local Payment"


def check_due_attempts() -> None:
	"""Every 2 minutes: ask the provider about Initiated and Pending attempts that are due."""
	attempts = frappe.get_all(
		ATTEMPT,
		filters={
			"parenttype": SESSION,
			"status": ["in", [lc.INITIATED, lc.PENDING]],
			# Frappe compares a date filter through ifnull(), so an attempt that was never scheduled reads
			# as due. That is the safe direction here: a new attempt is picked up rather than forgotten.
			"next_check_on": ["<=", now_datetime()],
		},
		# Longest overdue first, so a busy run cannot keep postponing the same attempts.
		order_by="next_check_on asc",
		limit=BATCH_SIZE,
		pluck="attempt_id",
	)
	_each(attempts, rc.reconcile)


def check_unresolved_attempts() -> None:
	"""Hourly: re-check Unresolved attempts, less and less often, until 72 hours after their deadline."""
	now = now_datetime()
	attempts = frappe.get_all(
		ATTEMPT,
		filters={
			"parenttype": SESSION,
			"status": lc.UNRESOLVED,
			"next_check_on": ["<=", now],
			"expires_on": [">", now - rc.UNRESOLVED_ALERT_AFTER],
		},
		order_by="next_check_on asc",
		limit=BATCH_SIZE,
		pluck="attempt_id",
	)
	_each(attempts, rc.reconcile)


def retry_authorizations() -> None:
	"""Hourly: run `on_payment_authorized` again for Paid sessions whose authorization is not Done.

	Sessions that reached the maximum number of tries have no retry date left, so they drop out of this
	query on their own: the daily alert and the manual "Retry authorization" action take over.
	"""
	sessions = frappe.get_all(
		SESSION,
		filters=[
			["status", "=", lc.PAID],
			["authorization", "in", [rc.AUTH_PENDING, rc.AUTH_FAILED]],
			# No retry date means the authorization has used up its tries. Frappe compares a date filter
			# through ifnull(), which would otherwise make those rows look due forever.
			["authorization_next_retry_on", "is", "set"],
			["authorization_next_retry_on", "<=", now_datetime()],
		],
		order_by="authorization_next_retry_on asc",
		limit=BATCH_SIZE,
		pluck="name",
	)
	_each(sessions, rc.authorize)


def send_daily_alerts() -> None:
	"""Daily: tell the managers about what the scheduler gave up on.

	Each alert is sent once, recorded by a flag on the row itself so a repeat run selects nothing.
	"""
	now = now_datetime()
	_each(
		frappe.get_all(
			ATTEMPT,
			filters={
				"parenttype": SESSION,
				"status": lc.UNRESOLVED,
				"alerted": 0,
				"expires_on": ["<=", now - rc.UNRESOLVED_ALERT_AFTER],
			},
			fields=["name", "parent", "attempt_id"],
			order_by="expires_on asc",
			limit=BATCH_SIZE,
		),
		_alert_unresolved,
		key=lambda row: row.attempt_id,
	)
	_each(
		frappe.get_all(
			SESSION,
			filters={
				"status": lc.PAID,
				"authorization": rc.AUTH_FAILED,
				"authorization_alerted": 0,
				"authorization_tries": [">=", rc.MAX_AUTHORIZATION_TRIES],
			},
			fields=["name", "authorization_tries"],
			order_by="modified asc",
			limit=BATCH_SIZE,
		),
		_alert_authorization,
		key=lambda row: row.name,
	)


def _alert_unresolved(row) -> None:
	# Another attempt may have paid the session since: nothing left to chase, but do not ask again either.
	paid = frappe.db.get_value(SESSION, row.parent, "status") != lc.OPEN
	frappe.db.set_value(ATTEMPT, row.name, "alerted", 1, update_modified=False)
	if not paid:
		rc.alert_managers(row.parent, row.attempt_id, "unresolved_timeout")


def _alert_authorization(row) -> None:
	frappe.db.set_value(SESSION, row.name, "authorization_alerted", 1, update_modified=False)
	rc.alert_managers(row.name, row.authorization_tries, "authorization_exhausted")


def _each(items: Iterable, action: Callable, key: Callable = str) -> None:
	"""Run `action` on each item. An exception is logged and the next item still runs."""
	started = now_datetime()
	for item in items:
		if now_datetime() - started > TIME_BUDGET:
			break
		try:
			action(item)
		except Exception:
			# Drop whatever the failed item left half-done, then log so the entry survives.
			frappe.db.rollback()
			frappe.log_error(title=f"Local Payment scheduler failed on {key(item)}")
