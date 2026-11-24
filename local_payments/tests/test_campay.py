# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

import base64
import hashlib
import hmac
import json
import traceback
import unittest
from decimal import Decimal
from pathlib import Path

import requests

from local_payments import lifecycle as lc
from local_payments.providers.campay import (
	CamPayClient,
	CamPayConfig,
	CamPayError,
	InitiationOutcome,
	webhook_signature_valid,
)
from local_payments.providers.common import InvalidRequest
from local_payments.tests.test_nokash import FakeHttp, FakeResponse

RECORDED = json.loads((Path(__file__).parent / "fixtures" / "campay.json").read_text())

CONFIG = CamPayConfig(api_base_url="https://demo.campay.net/api/", access_token="fake-access-token")

ATTEMPT_ID = "3f5c1c6e-8f4b-4a57-9d9a-3b1f5f0f2a11"
REFERENCE = "0b8f6c2e-5a3d-4e1f-9c7b-2d4e6f8a1b3c"
MSISDN = "237670000001"

VECTOR = RECORDED["jwt_vector"]
VALID_FROM, VALID_UNTIL = VECTOR["valid_between_unix"]


def recorded(name):
	return FakeResponse(RECORDED[name]["status"], RECORDED[name]["body"])


def status_answer(**fields):
	"""The recorded success, with some of its fields replaced."""
	return FakeResponse(200, {**RECORDED["status_successful"]["body"], **fields})


def client(answer, on_exchange=None):
	http = FakeHttp(answer)
	return CamPayClient(CONFIG, http, on_exchange), http


def collect(answer, on_exchange=None, amount=100, msisdn=MSISDN):
	campay, http = client(answer, on_exchange)
	return campay.collect(ATTEMPT_ID, amount, msisdn, "lp test"), http


def b64(data: bytes) -> str:
	return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwt(header=None, claims=None, key=VECTOR["webhook_key"], alg="sha256"):
	"""A token signed like CamPay's, with whatever header and claims a test needs."""
	header = {"typ": "JWT", "alg": "HS256"} if header is None else header
	claims = {"iat": VALID_FROM, "nbf": VALID_FROM, "exp": VALID_UNTIL} if claims is None else claims
	signed = f"{b64(json.dumps(header).encode())}.{b64(json.dumps(claims).encode())}"
	signature = hmac.new(key.encode(), signed.encode(), getattr(hashlib, alg)).digest()
	return f"{signed}.{b64(signature)}"


class TestCollect(unittest.TestCase):
	def test_the_request_sends_the_attempt_id_and_a_whole_amount_as_text(self):
		initiation, http = collect(recorded("collect_accepted"))

		call = http.calls[0]
		self.assertEqual((call.method, call.url), ("POST", "https://demo.campay.net/api/collect/"))
		self.assertEqual(
			call.kwargs["json"],
			{
				"amount": "100",
				"currency": "XAF",
				"from": MSISDN,
				"description": "lp test",
				"external_reference": ATTEMPT_ID,
			},
		)
		self.assertEqual(call.kwargs["headers"], {"Authorization": "Token fake-access-token"})
		self.assertIs(call.kwargs["allow_redirects"], False)
		self.assertEqual(initiation.outcome, InitiationOutcome.ACCEPTED)
		self.assertEqual(initiation.reference, REFERENCE)

	def test_outcomes(self):
		cases = {
			"collect_invalid_number": (InitiationOutcome.REJECTED, False),
			"collect_unsupported_carrier": (InitiationOutcome.REJECTED, False),
			"collect_invalid_amount": (InitiationOutcome.REJECTED, False),
			# A transaction may already exist under this external_reference.
			"collect_duplicate": (InitiationOutcome.UNKNOWN, False),
		}
		for name, (outcome, refused) in cases.items():
			with self.subTest(name):
				initiation, _ = collect(recorded(name))
				self.assertEqual(initiation.outcome, outcome)
				self.assertEqual(initiation.credentials_refused, refused)
				self.assertIsNone(initiation.reference)

	def test_a_refused_token_is_a_rejection_that_points_at_the_credentials(self):
		for status in (401, 403):
			with self.subTest(status):
				initiation, _ = collect(FakeResponse(status, {"detail": "Invalid token."}))
				self.assertEqual(initiation.outcome, InitiationOutcome.REJECTED)
				self.assertTrue(initiation.credentials_refused)

	def test_anything_else_leaves_the_outcome_unknown(self):
		cases = {
			"timeout": requests.ReadTimeout(),
			"connection": requests.ConnectionError(),
			"server error": FakeResponse(500, None, text="<html>oops</html>"),
			"429 without the duplicate code": FakeResponse(429, {"detail": "Throttled"}),
			"400 with an undocumented code": FakeResponse(400, {"error_code": "ER999", "message": "?"}),
			"400 without a body": FakeResponse(400, None),
			"200 without a reference": FakeResponse(200, {"ussd_code": "*126#"}),
			"200 with a reference that is not text": FakeResponse(200, {"reference": 12}),
			"200 with a reference unfit for a URL": FakeResponse(200, {"reference": "../balance"}),
			"200 that is not an object": FakeResponse(200, ["reference"]),
			"redirect": FakeResponse(307, None),
		}
		for name, answer in cases.items():
			with self.subTest(name):
				initiation, _ = collect(answer)
				self.assertEqual(initiation.outcome, InitiationOutcome.UNKNOWN)
				self.assertIsNone(initiation.reference)
				self.assertFalse(initiation.credentials_refused)

	def test_a_bad_number_or_amount_never_leaves(self):
		cases = {
			"short number": {"msisdn": "67000000"},
			"fraction": {"amount": "100.5"},
			"zero": {"amount": 0},
			"not a number": {"amount": "abc"},
		}
		for name, kwargs in cases.items():
			with self.subTest(name):
				campay, http = client(recorded("collect_accepted"))
				with self.assertRaises(InvalidRequest):
					campay.collect(ATTEMPT_ID, kwargs.get("amount", 100), kwargs.get("msisdn", MSISDN), "x")
				self.assertEqual(http.calls, [])

	def test_a_national_number_is_sent_with_the_country_code(self):
		_, http = collect(recorded("collect_accepted"), msisdn="670000001")
		self.assertEqual(http.calls[0].kwargs["json"]["from"], MSISDN)

	def test_the_network_log_holds_no_token(self):
		exchanges = []
		echo = FakeResponse(401, None, text="Invalid token fake-access-token")
		collect(echo, on_exchange=exchanges.append)
		collect(requests.ReadTimeout(), on_exchange=exchanges.append)

		self.assertEqual(len(exchanges), 2)
		for exchange in exchanges:
			self.assertEqual(exchange.request_headers, {})
			self.assertNotIn(CONFIG.access_token, json.dumps(exchange.__dict__))
		self.assertEqual(exchanges[0].response_body, "Invalid token ***")
		self.assertEqual(exchanges[1].error, "ReadTimeout")

	def test_the_token_stays_out_of_the_config_repr(self):
		self.assertNotIn(CONFIG.access_token, repr(CONFIG))


class TestStatus(unittest.TestCase):
	def test_the_call_reads_the_transaction_by_its_reference(self):
		campay, http = client(recorded("status_pending"))
		campay.check_status(REFERENCE, ATTEMPT_ID)

		call = http.calls[0]
		self.assertEqual(
			(call.method, call.url), ("GET", f"https://demo.campay.net/api/transaction/{REFERENCE}/")
		)
		self.assertEqual(call.kwargs["headers"], {"Authorization": "Token fake-access-token"})
		self.assertIs(call.kwargs["allow_redirects"], False)

	def test_transaction_statuses(self):
		cases = {
			"status_pending": (lc.PENDING, "PENDING"),
			"status_successful": (lc.SUCCEEDED, "SUCCESSFUL"),
			"status_failed": (lc.FAILED, "FAILED: TESTING"),
		}
		for name, (status, provider_status) in cases.items():
			with self.subTest(name):
				result = client(recorded(name))[0].check_status(REFERENCE, ATTEMPT_ID)
				self.assertEqual(result.status, status)
				self.assertEqual(result.provider_status, provider_status)
				self.assertEqual(result.transaction_id, REFERENCE)

	def test_amount_and_currency_are_reported_as_received_for_the_session_check(self):
		def resolve(result):
			return lc.resolve(
				session_status=lc.OPEN,
				session_amount=Decimal("100"),
				session_currency="XAF",
				attempt_status=lc.PENDING,
				result=result,
			)

		paid = client(recorded("status_successful"))[0].check_status(REFERENCE, ATTEMPT_ID)
		self.assertEqual((paid.amount, paid.currency), ("100.00", "XAF"))
		self.assertTrue(resolve(paid).session_paid)

		cases = {
			# The sandbox reports 0.00 even for a success: it must never pay a session.
			"sandbox success": recorded("status_successful_sandbox"),
			"other amount": status_answer(amount="99.00"),
			"other currency": status_answer(currency="EUR"),
			"no amount": status_answer(amount=None),
			"no currency": status_answer(currency=""),
		}
		for name, answer in cases.items():
			with self.subTest(name):
				result = client(answer)[0].check_status(REFERENCE, ATTEMPT_ID)
				resolution = resolve(result)
				self.assertTrue(resolution.amount_mismatch)
				self.assertFalse(resolution.session_paid)

	def test_an_answer_about_another_transaction_proves_nothing(self):
		cases = {
			"other reference": status_answer(reference="9d9a9d9a-0000-4000-8000-000000000000"),
			"other attempt": status_answer(external_reference="11111111-1111-4111-8111-111111111111"),
			"no attempt": status_answer(external_reference=""),
		}
		for name, answer in cases.items():
			with self.subTest(name), self.assertRaises(CamPayError) as failed:
				client(answer)[0].check_status(REFERENCE, ATTEMPT_ID)
			self.assertIn("mismatch", failed.exception.code)

	def test_anything_else_is_a_failed_check(self):
		cases = {
			"404": (recorded("status_not_found"), 404),
			"401": (FakeResponse(401, {"detail": "Invalid token."}), 401),
			"500": (FakeResponse(500, None, text="<html>oops</html>"), 500),
			"unknown status": (status_answer(status="REVERSED"), 200),
			"status not text": (status_answer(status=None), 200),
			"unreadable body": (FakeResponse(200, None, text="oops"), 200),
			"timeout": (requests.ReadTimeout(), None),
		}
		for name, (answer, http_status) in cases.items():
			with self.subTest(name), self.assertRaises(CamPayError) as failed:
				client(answer)[0].check_status(REFERENCE, ATTEMPT_ID)
			self.assertEqual(failed.exception.http_status, http_status)

	def test_only_401_and_403_are_a_refusal_of_the_token(self):
		for status, refused in ((401, True), (403, True), (404, False), (500, False)):
			with self.subTest(status), self.assertRaises(CamPayError) as failed:
				client(FakeResponse(status, None))[0].check_status(REFERENCE, ATTEMPT_ID)
			self.assertEqual(failed.exception.refused, refused)

	def test_a_reference_unfit_for_a_url_is_never_sent(self):
		for reference in ("../balance", "a/b", "", None, "x" * 65):
			with self.subTest(reference=reference):
				campay, http = client(recorded("status_pending"))
				with self.assertRaises(CamPayError):
					campay.check_status(reference, ATTEMPT_ID)
				self.assertEqual(http.calls, [])

	def test_a_failed_call_leaves_the_token_out_of_the_traceback(self):
		leaky = requests.ConnectionError("Authorization: Token fake-access-token")
		with self.assertRaises(CamPayError) as failed:
			client(leaky)[0].check_status(REFERENCE, ATTEMPT_ID)
		logged = "".join(traceback.format_exception(failed.exception))
		self.assertNotIn(CONFIG.access_token, logged)


class TestWebhookSignature(unittest.TestCase):
	def test_the_specifications_vector_is_valid_within_its_window_and_the_leeway(self):
		for now in (VALID_FROM - 60, VALID_FROM, VALID_UNTIL, VALID_UNTIL + 60):
			with self.subTest(now=now):
				self.assertTrue(webhook_signature_valid(VECTOR["jwt"], VECTOR["webhook_key"], now))

	def test_outside_the_window_it_is_refused(self):
		for name, now in (("not yet valid", VALID_FROM - 61), ("expired", VALID_UNTIL + 61)):
			with self.subTest(name):
				self.assertFalse(webhook_signature_valid(VECTOR["jwt"], VECTOR["webhook_key"], now))

	def test_another_key_is_refused(self):
		for key in ("other_key", "", None):
			with self.subTest(key=key):
				self.assertFalse(webhook_signature_valid(VECTOR["jwt"], key, VALID_FROM))

	def test_only_hs256_is_accepted(self):
		cases = {
			"none": jwt(header={"typ": "JWT", "alg": "none"}),
			"HS512 header and signature": jwt(header={"typ": "JWT", "alg": "HS512"}, alg="sha512"),
			"HS256 header, HS512 signature": jwt(alg="sha512"),
			"no alg": jwt(header={"typ": "JWT"}),
		}
		for name, token in cases.items():
			with self.subTest(name):
				self.assertFalse(webhook_signature_valid(token, VECTOR["webhook_key"], VALID_FROM))
		# The same builder, with a sound header, is accepted: the refusals above come from the algorithm.
		self.assertTrue(webhook_signature_valid(jwt(), VECTOR["webhook_key"], VALID_FROM))

	def test_unsigned_none_token_is_refused(self):
		header = b64(json.dumps({"alg": "none"}).encode())
		claims = b64(json.dumps({"nbf": VALID_FROM, "exp": VALID_UNTIL}).encode())
		self.assertFalse(webhook_signature_valid(f"{header}.{claims}.", VECTOR["webhook_key"], VALID_FROM))

	def test_changed_claims_break_the_signature(self):
		header, _, signature = VECTOR["jwt"].split(".")
		later = b64(json.dumps({"nbf": VALID_FROM, "exp": VALID_UNTIL + 86400}).encode())
		self.assertFalse(
			webhook_signature_valid(
				f"{header}.{later}.{signature}", VECTOR["webhook_key"], VALID_UNTIL + 3600
			)
		)

	def test_claims_without_a_usable_window_are_refused(self):
		cases = {
			"no exp": {"nbf": VALID_FROM},
			"no nbf": {"exp": VALID_UNTIL},
			"text times": {"nbf": str(VALID_FROM), "exp": str(VALID_UNTIL)},
			"boolean times": {"nbf": True, "exp": True},
		}
		for name, claims in cases.items():
			with self.subTest(name):
				self.assertFalse(webhook_signature_valid(jwt(claims=claims), VECTOR["webhook_key"], 1))

	def test_anything_malformed_is_invalid_and_never_raises(self):
		header, claims, signature = VECTOR["jwt"].split(".")
		signed_list = jwt(claims=[1, 2])
		cases = {
			"two parts": f"{header}.{claims}",
			"four parts": f"{VECTOR['jwt']}.x",
			"bad base64": f"{header}.{claims}.!!!",
			"header not base64": f"%%%.{claims}.{signature}",
			"header not JSON": f"{b64(b'not json')}.{claims}.{signature}",
			"header not UTF-8": f"{b64(bytes([0xFF, 0xFE]))}.{claims}.{signature}",
			"header not an object": f"{b64(b'[1]')}.{claims}.{signature}",
			"claims not an object": signed_list,
			"empty": "",
			"not text": 12,
			"None": None,
		}
		for name, token in cases.items():
			with self.subTest(name):
				self.assertFalse(webhook_signature_valid(token, VECTOR["webhook_key"], VALID_FROM))


if __name__ == "__main__":
	unittest.main()
