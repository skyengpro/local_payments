# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

import unittest
from decimal import Decimal

from local_payments import lifecycle as lc
from local_payments.lifecycle import ProviderResult

ATTEMPT_STATES = (lc.INITIATED, lc.PENDING, lc.SUCCEEDED, lc.FAILED, lc.EXPIRED, lc.UNRESOLVED, lc.ERROR)
SESSION_STATES = (lc.OPEN, lc.PAID, lc.VOID)

# The edges of ARCHITECTURE.md "Lifecycle", written out separately from the tables under test.
ATTEMPT_EDGES = {
	(lc.INITIATED, lc.PENDING),
	(lc.INITIATED, lc.SUCCEEDED),
	(lc.INITIATED, lc.FAILED),
	(lc.INITIATED, lc.EXPIRED),
	(lc.INITIATED, lc.UNRESOLVED),
	(lc.PENDING, lc.SUCCEEDED),
	(lc.PENDING, lc.FAILED),
	(lc.PENDING, lc.EXPIRED),
	(lc.PENDING, lc.UNRESOLVED),
	(lc.UNRESOLVED, lc.SUCCEEDED),
	(lc.UNRESOLVED, lc.FAILED),
	(lc.UNRESOLVED, lc.EXPIRED),
}
SESSION_EDGES = {(lc.OPEN, lc.PAID), (lc.OPEN, lc.VOID)}


def succeeded(amount="5000", currency="XAF"):
	return ProviderResult(lc.SUCCEEDED, Decimal(amount), currency, "TX-1")


def resolve(result, *, session=lc.OPEN, attempt=lc.PENDING, past_deadline=False):
	return lc.resolve(
		session_status=session,
		session_amount=Decimal("5000"),
		session_currency="XAF",
		attempt_status=attempt,
		result=result,
		past_deadline=past_deadline,
	)


class TestTransitions(unittest.TestCase):
	def assert_matches(self, check, states, edges):
		for current in states:
			for new in states:
				with self.subTest(current=current, new=new):
					if (current, new) in edges:
						check(current, new)
					else:
						with self.assertRaises(lc.IllegalTransition):
							check(current, new)

	def test_attempt_transitions_match_diagram(self):
		self.assert_matches(lc.check_attempt_transition, ATTEMPT_STATES, ATTEMPT_EDGES)

	def test_session_transitions_match_diagram(self):
		self.assert_matches(lc.check_session_transition, SESSION_STATES, SESSION_EDGES)

	def test_illegal_transition_names_both_states(self):
		with self.assertRaisesRegex(lc.IllegalTransition, "Succeeded -> Pending"):
			lc.check_attempt_transition(lc.SUCCEEDED, lc.PENDING)


class TestNewAttemptGuard(unittest.TestCase):
	def test_only_initiated_and_pending_block(self):
		for status in (lc.INITIATED, lc.PENDING):
			with self.subTest(status=status), self.assertRaises(lc.AttemptInProgress):
				lc.ensure_can_start_attempt([lc.FAILED, status])
		lc.ensure_can_start_attempt([lc.SUCCEEDED, lc.FAILED, lc.EXPIRED, lc.UNRESOLVED, lc.ERROR])


class TestResolve(unittest.TestCase):
	def test_success_pays_open_session_even_after_unresolved(self):
		for attempt in (lc.PENDING, lc.UNRESOLVED):
			with self.subTest(attempt=attempt):
				r = resolve(succeeded(), attempt=attempt)
				self.assertEqual((r.attempt_status, r.session_status), (lc.SUCCEEDED, lc.PAID))
				self.assertTrue(r.session_paid)

	def test_success_on_paid_session_is_duplicate(self):
		# Even with a wrong amount: duplicate is checked first and the session never changes.
		for result in (succeeded(), succeeded(amount="1")):
			r = resolve(result, session=lc.PAID)
			self.assertEqual((r.attempt_status, r.session_status), (lc.SUCCEEDED, lc.PAID))
			self.assertTrue(r.duplicate)
			self.assertFalse(r.amount_mismatch or r.session_paid)

	def test_amount_or_currency_mismatch_leaves_session_open(self):
		for result in (
			succeeded(amount="4999"),
			succeeded(amount="5000.5"),  # XAF has no sub-unit, so never rounded to 5000
			succeeded(currency="EUR"),
			ProviderResult(lc.SUCCEEDED, None, "XAF", "TX-1"),
		):
			with self.subTest(result=result):
				r = resolve(result)
				self.assertEqual((r.attempt_status, r.session_status), (lc.SUCCEEDED, lc.OPEN))
				self.assertTrue(r.amount_mismatch)
				self.assertFalse(r.duplicate or r.session_paid)

	def test_float_amount_from_frappe_matches_exactly(self):
		self.assertTrue(lc.amounts_match(5000.0, Decimal("5000")))
		self.assertFalse(lc.amounts_match(5000, 5000.01))

	def test_failure_or_expiry_changes_only_the_attempt(self):
		for status in (lc.FAILED, lc.EXPIRED):
			r = resolve(ProviderResult(status))
			self.assertEqual((r.attempt_status, r.session_status), (status, lc.OPEN))

	def test_still_pending_changes_nothing(self):
		for attempt in (lc.PENDING, lc.UNRESOLVED):
			r = resolve(ProviderResult(lc.PENDING), attempt=attempt, past_deadline=False)
			self.assertEqual((r.attempt_status, r.session_status), (attempt, lc.OPEN))

	def test_pending_past_local_deadline_becomes_unresolved(self):
		for attempt in (lc.INITIATED, lc.PENDING):
			r = resolve(ProviderResult(lc.PENDING), attempt=attempt, past_deadline=True)
			self.assertEqual(r.attempt_status, lc.UNRESOLVED)

	def test_unresolved_attempt_polled_again_stays_unresolved(self):
		r = resolve(ProviderResult(lc.PENDING), attempt=lc.UNRESOLVED, past_deadline=True)
		self.assertEqual(r.attempt_status, lc.UNRESOLVED)

	def test_late_success_is_applied_whatever_the_deadline(self):
		for attempt in (lc.INITIATED, lc.PENDING, lc.UNRESOLVED):
			with self.subTest(attempt=attempt):
				r = resolve(succeeded(), attempt=attempt, past_deadline=True)
				self.assertEqual((r.attempt_status, r.session_status), (lc.SUCCEEDED, lc.PAID))

	def test_any_result_on_a_final_attempt_is_illegal(self):
		for attempt in (lc.SUCCEEDED, lc.FAILED, lc.EXPIRED, lc.ERROR):
			with self.subTest(attempt=attempt), self.assertRaises(lc.IllegalTransition):
				resolve(succeeded(), attempt=attempt)

	def test_success_on_void_session_does_not_reopen_it(self):
		r = resolve(succeeded(), session=lc.VOID)
		self.assertEqual((r.attempt_status, r.session_status), (lc.SUCCEEDED, lc.VOID))
		self.assertFalse(r.session_paid)
