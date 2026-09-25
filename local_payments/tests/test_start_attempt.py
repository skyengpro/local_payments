# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

import json
import uuid
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime, set_request

from local_payments import api
from local_payments import lifecycle as lc
from local_payments import reconcile as rc
from local_payments.gateway import Initiated
from local_payments.lifecycle import ProviderResult
from local_payments.local_payments.doctype.local_payments_test_settings.local_payments_test_settings import (
	LocalPaymentsTestSettings,
)
from local_payments.tests.test_checkout import (
	GATEWAY_NAME,
	MALFORMED_TOKENS,
	SETTINGS,
	UNKNOWN_TOKEN,
	CheckoutTestCase,
	FakeProvider,
	make_session,
)

NATIONAL = "600000001"
MSISDN = "237600000001"
ACCEPTED = Initiated(lc.INITIATED)


class FakeInitiate:
	"""Stands in for the provider call. Records the attempt as it was on record when the call went out."""

	def __init__(self, result=ACCEPTED, during_call=None):
		self.result = result
		self.during_call = during_call
		self.calls = []

	def __call__(self, attempt_id, session, msisdn):
		on_record = frappe.db.get_value("Local Payment Attempt", {"attempt_id": attempt_id}, "status")
		self.calls.append((attempt_id, session.name, msisdn, on_record))
		if self.during_call:
			self.during_call(attempt_id)
		return self.result


def attempts(session):
	return frappe.get_all(
		"Local Payment Attempt",
		filters={"parent": session.name},
		fields=["*"],
		order_by="idx asc",
	)


class TestStartAttempt(CheckoutTestCase):
	def start(self, session, msisdn=NATIONAL, fake=None):
		fake = fake or FakeInitiate()
		with patch.object(LocalPaymentsTestSettings, "initiate", fake):
			return api.start_attempt(session.token, msisdn), fake

	def test_unknown_or_malformed_token_is_refused_without_writing(self):
		fake = FakeInitiate()
		with patch.object(LocalPaymentsTestSettings, "initiate", fake):
			for token in (UNKNOWN_TOKEN, *MALFORMED_TOKENS):
				with self.subTest(token=token), self.assertRaises(frappe.DoesNotExistError):
					api.start_attempt(token, NATIONAL)
		self.assertEqual(fake.calls, [])

	def test_closed_session_is_refused(self):
		for status in (lc.PAID, lc.VOID):
			with self.subTest(status=status):
				session = make_session(status=status)
				with self.assertRaises(frappe.ValidationError):
					self.start(session)
				self.assertEqual(attempts(session), [])

	def test_a_running_attempt_blocks_a_new_one(self):
		for status in (lc.INITIATED, lc.PENDING):
			with self.subTest(status=status):
				session = make_session({"status": status})
				fake = FakeInitiate()
				with self.assertRaises(frappe.ValidationError):
					self.start(session, fake=fake)
				self.assertEqual(len(attempts(session)), 1)
				self.assertEqual(fake.calls, [])

	def test_a_settled_or_unresolved_attempt_does_not_block(self):
		session = make_session({"status": lc.FAILED}, {"status": lc.UNRESOLVED})
		self.start(session)
		self.assertEqual(len(attempts(session)), 3)

	def test_disabled_gateway_is_refused(self):
		frappe.db.set_value(SETTINGS, GATEWAY_NAME, "enabled", 0)
		frappe.db.commit()
		self.addCleanup(frappe.db.commit)
		self.addCleanup(frappe.db.set_value, SETTINGS, GATEWAY_NAME, "enabled", 1)

		session = make_session()
		with self.assertRaises(frappe.ValidationError):
			self.start(session)
		self.assertEqual(attempts(session), [])

	def test_invalid_number_is_refused_before_anything_is_written_or_sent(self):
		session = make_session()
		for number in ("", None, "60000000", "6000000011", "abc600000001", "+33600000001", "0237600000001"):
			fake = FakeInitiate()
			with self.subTest(number=number), self.assertRaises(frappe.ValidationError):
				self.start(session, msisdn=number, fake=fake)
			self.assertEqual(fake.calls, [])
		self.assertEqual(attempts(session), [])

	def test_the_start_budget_is_per_token_and_counts_mistyped_numbers(self):
		session = make_session()
		set_request(method="POST", path="/api/method/local_payments.api.start_attempt")
		# An address of its own, so the per-address budget of other runs does not interfere.
		frappe.local.request_ip = f"test-{frappe.generate_hash(length=8)}"
		frappe.local.form_dict = frappe._dict(cmd="local_payments.api.start_attempt", token=session.token)

		for _typo in range(api.STARTS_PER_TOKEN):
			with self.assertRaises(frappe.ValidationError):
				self.start(session, msisdn="1")
		with self.assertRaises(frappe.RateLimitExceededError):
			self.start(session)
		self.assertEqual(attempts(session), [])

	def test_a_valid_start_records_the_attempt_before_the_provider_call(self):
		session = make_session()
		state, fake = self.start(session, msisdn="+237 600 000 001")

		(row,) = attempts(session)
		self.assertEqual(uuid.UUID(row.attempt_id).version, 4)
		self.assertEqual((row.status, row.payer_msisdn), (lc.INITIATED, MSISDN))
		self.assertEqual(get_datetime(row.expires_on) - get_datetime(row.started_on), timedelta(minutes=15))
		# Due now that the provider has answered.
		self.assertLessEqual(get_datetime(row.next_check_on), now_datetime())
		self.assertEqual(fake.calls, [(row.attempt_id, session.name, MSISDN, lc.INITIATED)])
		self.assertEqual(state, {"status": lc.OPEN, "attempt_status": lc.INITIATED, "redirect_url": None})

	def test_a_refused_initiation_ends_in_error_and_the_payer_can_try_again(self):
		session = make_session()
		self.start(session, fake=FakeInitiate(Initiated(lc.ERROR, "fake-integration-request")))

		(row,) = attempts(session)
		self.assertEqual(row.status, lc.ERROR)
		self.assertIsNone(row.next_check_on)
		self.assertEqual(row.integration_request, "fake-integration-request")

		state, _fake = self.start(session)
		self.assertEqual(state["attempt_status"], lc.INITIATED)
		self.assertEqual(len(attempts(session)), 2)

	def test_an_unexpected_failure_leaves_an_attempt_to_query_and_no_number_in_the_log(self):
		def fail(attempt_id):
			raise RuntimeError("Integration Request could not be saved")

		session = make_session()
		state, _fake = self.start(session, fake=FakeInitiate(during_call=fail))

		(row,) = attempts(session)
		self.assertEqual(row.status, lc.INITIATED)
		self.assertLessEqual(get_datetime(row.next_check_on), now_datetime())
		self.assertEqual(state["attempt_status"], lc.INITIATED)
		log = frappe.get_last_doc("Error Log", filters={"method": "Local Payment initiation failed"})
		self.assertIn("could not be saved", log.error)
		self.assertNotIn(MSISDN, log.error)

	def test_a_late_refusal_does_not_undo_what_a_status_check_recorded(self):
		def status_check_meanwhile(attempt_id):
			frappe.db.set_value("Local Payment Attempt", {"attempt_id": attempt_id}, "status", lc.PENDING)

		session = make_session()
		fake = FakeInitiate(
			Initiated(lc.ERROR, "fake-integration-request"), during_call=status_check_meanwhile
		)
		self.start(session, fake=fake)

		(row,) = attempts(session)
		self.assertEqual(row.status, lc.PENDING)
		self.assertEqual(row.integration_request, "fake-integration-request")

	def test_no_status_check_reaches_the_provider_while_the_request_is_on_its_way(self):
		provider = FakeProvider()

		def check_from_another_tab(attempt_id):
			with patch.object(rc, "_provider_for", return_value=provider):
				self.assertIsNone(rc.reconcile(attempt_id))

		session = make_session()
		self.start(session, fake=FakeInitiate(during_call=check_from_another_tab))
		self.assertEqual(provider.calls, [])

	def test_an_attempt_whose_start_never_finished_is_checked_once_the_window_is_over(self):
		attempt_id = str(uuid.uuid4())
		make_session(
			{
				"attempt_id": attempt_id,
				"status": lc.INITIATED,
				"next_check_on": add_to_date(now_datetime(), seconds=-1),
			}
		)
		provider = FakeProvider(ProviderResult(lc.PENDING))
		with patch.object(rc, "_provider_for", return_value=provider):
			rc.reconcile(attempt_id)
		self.assertEqual(provider.calls, [attempt_id])


class TestMtnMomoCallback(CheckoutTestCase):
	def callback(self, attempt, body=None):
		# As MTN sends it: the attempt in the query string, a JSON body Frappe turns into form_dict.
		query = f"attempt={attempt}" if attempt is not None else ""
		set_request(
			method="PUT",
			path="/api/method/local_payments.api.mtn_momo_callback",
			query_string=query,
			data=json.dumps(body or {}),
			content_type="application/json",
		)
		frappe.local.request_ip = "127.0.0.1"
		frappe.local.form_dict = frappe._dict(body or {})
		provider = FakeProvider()
		with (
			patch.object(frappe, "enqueue") as enqueue,
			patch.object(rc, "reconcile") as reconcile,
			patch.object(rc, "_provider_for", return_value=provider),
		):
			answer = api.mtn_momo_callback()
		# Never inside the request: reconcile() commits and asks the provider.
		reconcile.assert_not_called()
		self.assertEqual(provider.calls, [])
		return answer, enqueue

	def test_a_known_running_attempt_queues_one_reconcile(self):
		for status in (lc.INITIATED, lc.PENDING, lc.UNRESOLVED):
			with self.subTest(status=status):
				attempt_id = str(uuid.uuid4())
				make_session({"attempt_id": attempt_id, "status": status})
				answer, enqueue = self.callback(attempt_id, body={"status": "SUCCESSFUL", "externalId": "x"})

				self.assertIsNone(answer)
				enqueue.assert_called_once_with(
					"local_payments.reconcile.reconcile",
					attempt_id=attempt_id,
					job_id=f"local_payments:reconcile:{attempt_id}",
					deduplicate=True,
				)

	def test_anything_else_gets_the_same_answer_and_queues_nothing(self):
		settled = str(uuid.uuid4())
		make_session({"attempt_id": settled, "status": lc.SUCCEEDED})
		for attempt in (str(uuid.uuid4()), settled, None, "", "not-an-attempt", UNKNOWN_TOKEN):
			with self.subTest(attempt=attempt):
				answer, enqueue = self.callback(attempt)
				self.assertIsNone(answer)
				enqueue.assert_not_called()
