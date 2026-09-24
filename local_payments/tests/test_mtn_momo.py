# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

import base64
import json
import unittest
from dataclasses import dataclass, replace
from pathlib import Path

import requests

from local_payments import lifecycle as lc
from local_payments.providers.mtn_momo import (
	ErrorKind,
	InitiationOutcome,
	InvalidRequest,
	MtnMomoClient,
	MtnMomoConfig,
	MtnMomoError,
	clean_text,
)

RECORDED = json.loads((Path(__file__).parent / "fixtures" / "mtn_momo.json").read_text())

CONFIG = MtnMomoConfig(
	api_base_url="https://sandbox.momodeveloper.mtn.com/",
	target_environment="mtncameroon",
	subscription_key="fake-subscription-key",
	api_user="fake-api-user",
	api_key="fake-api-key",
	currency="XAF",
	msisdn_prefix="237",
	msisdn_national_length=9,
)
SANDBOX = replace(CONFIG, target_environment="sandbox", sandbox=True)

ATTEMPT_ID = "3f5c1c6e-8f4b-4a57-9d9a-3b1f5f0f2a11"
MSISDN = "237000000001"


class FakeResponse:
	def __init__(self, status, body):
		self.status_code = status
		self.body = body

	def json(self):
		if self.body is None:
			raise requests.JSONDecodeError("Expecting value", "", 0)
		return self.body


def recorded(name):
	return FakeResponse(RECORDED[name]["status"], RECORDED[name]["body"])


@dataclass
class Call:
	method: str
	url: str
	headers: dict
	body: dict | None


class FakeHttp:
	"""Replays recorded responses, or raises a transport error, in order."""

	def __init__(self, *replies):
		self.replies = list(replies)
		self.calls = []

	def request(self, method, url, headers=None, json=None, timeout=None):
		self.calls.append(Call(method, url, headers or {}, json))
		reply = self.replies.pop(0)
		if isinstance(reply, Exception):
			raise reply
		return reply


class MemoryTokenStore:
	def __init__(self, token=None):
		self.tokens = {"key": token} if token else {}
		self.ttl = None

	def get(self, key):
		return self.tokens.get(key)

	def set(self, key, token, ttl_seconds):
		self.tokens[key] = token
		self.ttl = ttl_seconds

	def delete(self, key):
		self.tokens.pop(key, None)


def client(*replies, token="cached-token", config=CONFIG):
	http = FakeHttp(*replies)
	return MtnMomoClient(config, MemoryTokenStore(token), "key", http=http), http


def pay(mtn, **overrides):
	values = {
		"attempt_id": ATTEMPT_ID,
		"amount": 5000,
		"msisdn": MSISDN,
		"external_id": "LPAY-2026-00001",
		"payer_message": "Facture n°12 réglée",
		"payee_note": "Merci d'avoir payé",
		**overrides,
	}
	return mtn.request_to_pay(**values)


class TestAccessToken(unittest.TestCase):
	def test_token_is_requested_once_and_cached_until_expires_in_minus_60(self):
		mtn, http = client(
			recorded("token"),
			recorded("requesttopay_accepted"),
			recorded("requesttopay_accepted"),
			token=None,
		)
		pay(mtn)
		pay(mtn)

		token_call, first, second = http.calls
		basic = base64.b64encode(b"fake-api-user:fake-api-key").decode()
		self.assertEqual(token_call.url, "https://sandbox.momodeveloper.mtn.com/collection/token/")
		self.assertEqual(token_call.headers["Authorization"], f"Basic {basic}")
		self.assertEqual(token_call.headers["Ocp-Apim-Subscription-Key"], "fake-subscription-key")
		self.assertEqual(first.headers["Authorization"], "Bearer fake-access-token")
		self.assertEqual(second.headers["Authorization"], "Bearer fake-access-token")
		self.assertEqual(mtn.token_store.ttl, 3540)

	def test_401_renews_the_token_once_and_retries_once(self):
		mtn, http = client(recorded("unauthorized"), recorded("token"), recorded("requesttopay_accepted"))
		self.assertEqual(pay(mtn).outcome, InitiationOutcome.ACCEPTED)
		self.assertEqual(
			[call.headers["Authorization"] for call in (http.calls[0], http.calls[2])],
			["Bearer cached-token", "Bearer fake-access-token"],
		)

	def test_a_second_401_is_reported_as_unauthorized(self):
		mtn, http = client(recorded("unauthorized"), recorded("token"), recorded("unauthorized"))
		self.assertEqual(pay(mtn).outcome, InitiationOutcome.UNAUTHORIZED)
		self.assertEqual(len(http.calls), 3)

		mtn, _http = client(recorded("unauthorized"), recorded("token"), recorded("unauthorized"))
		with self.assertRaises(MtnMomoError) as raised:
			mtn.check_status(ATTEMPT_ID)
		self.assertEqual(raised.exception.kind, ErrorKind.UNAUTHORIZED)


class TestRequestToPay(unittest.TestCase):
	def test_request_carries_the_attempt_id_and_mtn_headers(self):
		mtn, http = client(recorded("requesttopay_accepted"))
		pay(mtn, callback_url="https://example.com/callback")

		(call,) = http.calls
		self.assertEqual(
			(call.method, call.url),
			("POST", "https://sandbox.momodeveloper.mtn.com/collection/v1_0/requesttopay"),
		)
		self.assertEqual(call.headers["X-Reference-Id"], ATTEMPT_ID)
		self.assertEqual(call.headers["X-Target-Environment"], "mtncameroon")
		self.assertEqual(call.headers["Ocp-Apim-Subscription-Key"], "fake-subscription-key")
		self.assertEqual(call.headers["X-Callback-Url"], "https://example.com/callback")
		self.assertEqual(
			call.body,
			{
				"amount": "5000",
				"currency": "XAF",
				"externalId": "LPAY-2026-00001",
				"payer": {"partyIdType": "MSISDN", "partyId": MSISDN},
				"payerMessage": "Facture n 12 reglee",
				"payeeNote": "Merci d avoir paye",
			},
		)

	def test_no_callback_header_without_a_callback_url(self):
		mtn, http = client(recorded("requesttopay_accepted"))
		pay(mtn)
		self.assertNotIn("X-Callback-Url", http.calls[0].headers)

	def test_each_answer_is_a_distinct_outcome(self):
		cases = {
			"202": ((recorded("requesttopay_accepted"),), "cached-token", InitiationOutcome.ACCEPTED),
			"409": (
				(recorded("requesttopay_duplicate"),),
				"cached-token",
				InitiationOutcome.DUPLICATE_REFERENCE,
			),
			"400": ((recorded("bad_request"),), "cached-token", InitiationOutcome.REJECTED),
			"500": ((recorded("internal_error"),), "cached-token", InitiationOutcome.UNKNOWN),
			"timeout": ((requests.ReadTimeout(),), "cached-token", InitiationOutcome.UNKNOWN),
			"connection": ((requests.ConnectionError(),), "cached-token", InitiationOutcome.UNKNOWN),
			"token 500": ((recorded("internal_error"),), None, InitiationOutcome.NOT_SENT),
			"token timeout": ((requests.ConnectTimeout(),), None, InitiationOutcome.NOT_SENT),
			"token 401": ((recorded("token_unauthorized"),), None, InitiationOutcome.UNAUTHORIZED),
		}
		for case, (replies, token, outcome) in cases.items():
			with self.subTest(case):
				mtn, _http = client(*replies, token=token)
				self.assertEqual(pay(mtn).outcome, outcome)

	def test_409_keeps_mtn_error_code(self):
		mtn, _http = client(recorded("requesttopay_duplicate"))
		self.assertEqual(pay(mtn).code, "RESOURCE_ALREADY_EXIST")

	def test_bad_number_or_amount_is_refused_before_any_call(self):
		cases = {
			"plus sign": {"msisdn": "+237000000001"},
			"national only": {"msisdn": "000000001"},
			"too short": {"msisdn": "23700000001"},
			"space": {"msisdn": "237 00000001"},
			"other prefix": {"msisdn": "225000000001"},
			"Arabic-Indic digits": {"msisdn": "237" + chr(0x0660) * 8 + chr(0x0661)},
			"fraction": {"amount": "5000.5"},
			"zero": {"amount": 0},
			"negative": {"amount": -5},
			"not a number": {"amount": "abc"},
		}
		for case, overrides in cases.items():
			with self.subTest(case):
				mtn, http = client()
				with self.assertRaises(InvalidRequest):
					pay(mtn, **overrides)
				self.assertEqual(http.calls, [])

	def test_sandbox_sends_eur(self):
		mtn, http = client(recorded("requesttopay_accepted"), config=SANDBOX)
		pay(mtn)
		self.assertEqual(http.calls[0].body["currency"], "EUR")


class TestCheckStatus(unittest.TestCase):
	def test_status_query_url_and_headers(self):
		mtn, http = client(recorded("status_pending"))
		mtn.check_status(ATTEMPT_ID)

		(call,) = http.calls
		self.assertEqual(call.method, "GET")
		self.assertEqual(
			call.url, f"https://sandbox.momodeveloper.mtn.com/collection/v1_0/requesttopay/{ATTEMPT_ID}"
		)
		self.assertEqual(call.headers["X-Target-Environment"], "mtncameroon")
		self.assertNotIn("X-Callback-Url", call.headers)

	def test_mtn_answers_map_to_provider_results(self):
		cases = {
			"status_successful": (lc.SUCCEEDED, "5000", "XAF", "23503452", "SUCCESSFUL"),
			"status_successful_numbers": (lc.SUCCEEDED, "5000", "XAF", "23503452", "SUCCESSFUL"),
			"status_pending": (lc.PENDING, "5000", "XAF", None, "PENDING"),
			"status_failed_reason_object": (lc.FAILED, "5000", "XAF", None, "FAILED: PAYER_NOT_FOUND"),
			"status_failed_reason_string": (lc.FAILED, "5000", "XAF", None, "FAILED: NOT_ENOUGH_FUNDS"),
			"status_not_found": (lc.FAILED, None, None, None, "404: RESOURCE_NOT_FOUND"),
		}
		for name, expected in cases.items():
			with self.subTest(name):
				mtn, _http = client(recorded(name))
				result = mtn.check_status(ATTEMPT_ID)
				self.assertEqual(
					(
						result.status,
						result.amount,
						result.currency,
						result.transaction_id,
						result.provider_status,
					),
					expected,
				)

	def test_unusable_answers_raise_a_typed_error(self):
		cases = {
			"400": ((recorded("bad_request"),), "cached-token", ErrorKind.REJECTED),
			"500": ((recorded("internal_error"),), "cached-token", ErrorKind.UNAVAILABLE),
			"timeout": ((requests.ReadTimeout(),), "cached-token", ErrorKind.UNAVAILABLE),
			"token 500": ((recorded("internal_error"),), None, ErrorKind.UNAVAILABLE),
			"unknown status": ((recorded("status_unknown_value"),), "cached-token", ErrorKind.UNEXPECTED),
			"not JSON": ((recorded("status_not_json"),), "cached-token", ErrorKind.UNEXPECTED),
			"409 on a GET": ((recorded("requesttopay_duplicate"),), "cached-token", ErrorKind.UNEXPECTED),
		}
		for case, (replies, token, kind) in cases.items():
			with self.subTest(case):
				mtn, _http = client(*replies, token=token)
				with self.assertRaises(MtnMomoError) as raised:
					mtn.check_status(ATTEMPT_ID)
				self.assertEqual(raised.exception.kind, kind)

	def test_sandbox_eur_is_read_back_as_the_contract_currency(self):
		mtn, _http = client(recorded("status_successful_sandbox"), config=SANDBOX)
		self.assertEqual(mtn.check_status(ATTEMPT_ID).currency, "XAF")

	def test_production_currency_is_passed_through_for_lifecycle_to_compare(self):
		mtn, _http = client(recorded("status_successful_sandbox"))
		self.assertEqual(mtn.check_status(ATTEMPT_ID).currency, "EUR")


class TestCleanText(unittest.TestCase):
	def test_refused_characters_go_and_length_is_capped(self):
		self.assertEqual(clean_text("  L'été  <b>payé</b> ! "), "L ete b paye /b")
		self.assertEqual(len(clean_text("a" * 200)), 160)
		self.assertEqual(clean_text(None), "")


class TestSecrets(unittest.TestCase):
	def test_config_repr_hides_the_keys(self):
		self.assertNotIn("fake-api-key", repr(CONFIG))
		self.assertNotIn("fake-subscription-key", repr(CONFIG))
