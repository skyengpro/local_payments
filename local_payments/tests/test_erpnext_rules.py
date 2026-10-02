# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

import unittest
from types import SimpleNamespace

from local_payments import erpnext as ep


def session(status, authorization="", name="LPAY-1"):
	return SimpleNamespace(name=name, status=status, authorization=authorization)


class TestSettlementAction(unittest.TestCase):
	def test_submitted_unpaid_request_on_our_gateway_is_settled(self):
		for status in ("Authorized", "Completed"):
			for pr_status in ("Requested", "Initiated", "Partially Paid"):
				with self.subTest(status=status, pr_status=pr_status):
					self.assertEqual(ep.settlement_action(True, status, 1, pr_status), ep.SETTLE)

	def test_another_gateway_is_left_alone(self):
		# Whatever the request's state, even one we would otherwise refuse.
		for docstatus, pr_status in ((1, "Requested"), (2, "Cancelled"), (0, "Draft")):
			with self.subTest(docstatus=docstatus):
				self.assertEqual(ep.settlement_action(False, "Completed", docstatus, pr_status), ep.SKIP)

	def test_status_other_than_paid_is_ignored(self):
		for status in ("Failed", "Cancelled", "", None):
			with self.subTest(status=status):
				self.assertEqual(ep.settlement_action(True, status, 1, "Requested"), ep.SKIP)

	def test_already_paid_request_is_skipped(self):
		self.assertEqual(ep.settlement_action(True, "Completed", 1, "Paid"), ep.SKIP)

	def test_request_that_is_not_submitted_is_refused(self):
		for docstatus, pr_status in ((2, "Cancelled"), (0, "Draft")):
			with self.subTest(docstatus=docstatus):
				self.assertEqual(ep.settlement_action(True, "Completed", docstatus, pr_status), ep.REFUSE)


class TestUnbookedSessions(unittest.TestCase):
	def test_paid_session_not_yet_booked_is_listed(self):
		for authorization in ("", "Pending", "Failed"):
			with self.subTest(authorization=authorization):
				sessions = [session("Open", name="LPAY-1"), session("Paid", authorization, name="LPAY-2")]
				self.assertEqual(ep.unbooked_sessions(sessions), ["LPAY-2"])

	def test_booked_open_or_void_sessions_are_not_listed(self):
		self.assertEqual(
			ep.unbooked_sessions([session("Paid", "Done"), session("Open"), session("Void")]), []
		)

	def test_no_session_lists_nothing(self):
		self.assertEqual(ep.unbooked_sessions([]), [])
