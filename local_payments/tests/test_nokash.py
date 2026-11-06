# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

import json
import unittest
from dataclasses import dataclass
from pathlib import Path

import requests

from local_payments import lifecycle as lc
from local_payments.providers.common import InvalidRequest
from local_payments.providers.nokash import (
	MTN_MOMO,
	ORANGE_MONEY,
	InitiationOutcome,
	NokashClient,
	NokashConfig,
	NokashError,
	sign,
)

RECORDED = json.loads((Path(__file__).parent / "fixtures" / "nokash.json").read_text())

CONFIG = NokashConfig(
	api_base_url="https://api.nokash.app/",
	i_space_key="fake-i-key",
	app_space_key="fake-app-key",
)

ATTEMPT_ID = "3f5c1c6e-8f4b-4a57-9d9a-3b1f5f0f2a11"
MSISDN = "237000000001"
NATIONAL = "000000001"
TRANSACTION_ID = "nk_tx_xm5ojhd01lkv06c0bjvu"


class FakeResponse:
	def __init__(self, status, body, text=None):
		self.status_code = status
		self.body = body
		self._text = text

	@property
	def text(self):
		if self._text is not None:
			return self._text
		return "" if self.body is None else json.dumps(self.body)

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
	kwargs: dict


class FakeHttp:
	"""Replays one recorded response, or raises a transport error."""

	def __init__(self, answer):
		self.answer = answer
		self.calls = []

	def request(self, method, url, **kwargs):
		self.calls.append(Call(method, url, kwargs))
		if isinstance(self.answer, Exception):
			raise self.answer
		return self.answer


def client(answer, on_exchange=None):
	http = FakeHttp(answer)
	return NokashClient(CONFIG, http, on_exchange), http


class TestPayin(unittest.TestCase):
	def test_signature_matches_the_specification(self):
		vector = RECORDED["signature_vector"]
		self.assertEqual(
			sign(
				vector["order_id"],
				vector["amount"],
				vector["user_phone"],
				vector["app_space_key"],
				vector["i_space_key"],
			),
			vector["hmac_signature"],
		)

	def test_the_request_signs_what_it_sends(self):
		nokash, http = client(recorded("payin_accepted"))
		# A Currency field reads as a float.
		nokash.payin(ATTEMPT_ID, 1000.0, MSISDN, ORANGE_MONEY)

		call = http.calls[0]
		body = call.kwargs["json"]
		self.assertEqual(
			(call.method, call.url),
			("POST", "https://api.nokash.app/lapas-on-trans/trans/api-payin-request/407"),
		)
		self.assertEqual(
			{key: body[key] for key in ("order_id", "payment_method", "user_data")},
			{
				"order_id": ATTEMPT_ID,
				"payment_method": ORANGE_MONEY,
				"user_data": {"user_phone": NATIONAL},
			},
		)
		self.assertIs(type(body["amount"]), int)
		self.assertNotIn("callback_url", body)
		self.assertEqual(
			call.kwargs["headers"]["hmac-signature"],
			sign(ATTEMPT_ID, 1000, NATIONAL, CONFIG.app_space_key, CONFIG.i_space_key),
		)
		self.assertIs(call.kwargs["allow_redirects"], False)

	def test_outcome_comes_from_the_answer_and_never_decides_the_payment(self):
		cases = {
			"accepted": ("payin_accepted", InitiationOutcome.ACCEPTED, "nk_tx_z7iky89lkg9iso9hk86u"),
			"already failed, only the id counts": (
				"payin_accepted_already_failed",
				InitiationOutcome.ACCEPTED,
				"nk_tx_z7iky89lkg9iso9hk86u",
			),
			"missing field": ("payin_missing_field", InitiationOutcome.REJECTED, None),
			"below minimum": ("payin_below_minimum", InitiationOutcome.REJECTED, None),
			"unsupported operator": ("payin_unsupported_operator", InitiationOutcome.REJECTED, None),
			"any other value": ("unknown_answer", InitiationOutcome.UNKNOWN, None),
			"server error, whatever its body says": ("server_error", InitiationOutcome.UNKNOWN, None),
			"unreadable body": ("not_json", InitiationOutcome.UNKNOWN, None),
			"timeout": (requests.ReadTimeout(), InitiationOutcome.UNKNOWN, None),
			"no connection": (requests.ConnectionError(), InitiationOutcome.UNKNOWN, None),
			"redirect": (FakeResponse(307, None), InitiationOutcome.UNKNOWN, None),
			"accepted without an id": (
				FakeResponse(200, {"status": "REQUEST_OK", "data": {}}),
				InitiationOutcome.UNKNOWN,
				None,
			),
			"accepted without data": (
				FakeResponse(200, {"status": "REQUEST_OK", "data": None}),
				InitiationOutcome.UNKNOWN,
				None,
			),
			"a JSON list": (FakeResponse(200, ["REQUEST_OK"]), InitiationOutcome.UNKNOWN, None),
		}
		for case, (answer, outcome, transaction_id) in cases.items():
			with self.subTest(case):
				answer = recorded(answer) if isinstance(answer, str) else answer
				initiation = client(answer)[0].payin(ATTEMPT_ID, 1000, MSISDN, MTN_MOMO)
				self.assertEqual((initiation.outcome, initiation.transaction_id), (outcome, transaction_id))

	def test_a_bad_request_never_leaves(self):
		cases = {
			"fraction": (1000.5, MSISDN, MTN_MOMO),
			"zero": (0, MSISDN, MTN_MOMO),
			"foreign number": (1000, "33600000001", MTN_MOMO),
		}
		for case, (amount, msisdn, operator) in cases.items():
			with self.subTest(case):
				nokash, http = client(recorded("payin_accepted"))
				with self.assertRaises(InvalidRequest) as refused:
					nokash.payin(ATTEMPT_ID, amount, msisdn, operator)
				self.assertEqual(http.calls, [])
				self.assertNotIn(msisdn, str(refused.exception))

	def test_a_callback_url_goes_out_only_when_given(self):
		nokash, http = client(recorded("payin_accepted"))
		nokash.payin(ATTEMPT_ID, 1000, MSISDN, MTN_MOMO, callback_url="https://pay.example.com/hook")
		self.assertEqual(http.calls[0].kwargs["json"]["callback_url"], "https://pay.example.com/hook")

	def test_no_secret_reaches_the_network_log(self):
		signature = sign(ATTEMPT_ID, 1000, NATIONAL, CONFIG.app_space_key, CONFIG.i_space_key)
		echoed = FakeResponse(
			400,
			{"status": "REQUEST_BAD_INFOS"},
			text=json.dumps(
				{
					"status": "REQUEST_BAD_INFOS",
					"received": {
						"i_space_key": CONFIG.i_space_key,
						"app_space_key": CONFIG.app_space_key,
						"hmac-signature": signature,
					},
				}
			),
		)
		cases = {
			"answered": (recorded("payin_accepted"), 200),
			"answered with an echo": (echoed, 400),
			"no answer": (requests.ReadTimeout(), None),
		}
		for case, (answer, status_code) in cases.items():
			with self.subTest(case):
				exchanges = []
				client(answer, exchanges.append)[0].payin(ATTEMPT_ID, 1000, MSISDN, MTN_MOMO)

				self.assertEqual(len(exchanges), 1)
				self.assertEqual(exchanges[0].status_code, status_code)
				self.assertEqual(exchanges[0].request_body["order_id"], ATTEMPT_ID)
				logged = repr(exchanges[0])
				for secret in (CONFIG.i_space_key, CONFIG.app_space_key, signature):
					self.assertNotIn(secret, logged)

	def test_a_long_answer_is_cut_only_after_its_secrets_are_gone(self):
		exchanges = []
		answer = FakeResponse(500, None, text="x" * 1990 + CONFIG.app_space_key + "y" * 100)
		client(answer, exchanges.append)[0].payin(ATTEMPT_ID, 1000, MSISDN, MTN_MOMO)
		self.assertEqual(exchanges[0].response_body, "x" * 1990 + "***" + "y" * 7)

	def test_the_keys_stay_out_of_the_config_repr(self):
		self.assertNotIn(CONFIG.i_space_key, repr(CONFIG))
		self.assertNotIn(CONFIG.app_space_key, repr(CONFIG))


class TestStatus(unittest.TestCase):
	def test_the_call_asks_by_nokash_id_with_no_body_and_no_keys(self):
		nokash, http = client(recorded("status_success"))
		nokash.check_status(TRANSACTION_ID)

		call = http.calls[0]
		self.assertEqual(
			(call.method, call.url, call.kwargs["params"]),
			(
				"POST",
				"https://api.nokash.app/lapas-on-trans/trans/310/status-request",
				{"transaction_id": TRANSACTION_ID},
			),
		)
		self.assertNotIn("json", call.kwargs)
		self.assertEqual(call.kwargs["headers"], {"Content-Type": "application/json"})

	def test_transaction_statuses(self):
		cases = {
			"status_pending": (lc.PENDING, "PENDING"),
			"status_success": (lc.SUCCEEDED, "SUCCESS"),
			"status_failed": (lc.FAILED, "FAILED: Insufficient customer balance"),
			"status_canceled": (lc.FAILED, "CANCELED: Canceled by the user"),
			"status_timeout": (lc.EXPIRED, "TIMEOUT: No response from the operator"),
		}
		for name, (status, provider_status) in cases.items():
			with self.subTest(name):
				result = client(recorded(name))[0].check_status(TRANSACTION_ID)
				self.assertEqual((result.status, result.provider_status), (status, provider_status))

	def test_a_success_carries_what_the_session_is_compared_with(self):
		result = client(recorded("status_success"))[0].check_status(TRANSACTION_ID)
		self.assertTrue(lc.amounts_match(1000.0, result.amount))
		self.assertEqual((result.currency, result.transaction_id), ("XAF", TRANSACTION_ID))

	def test_anything_else_is_a_failed_check(self):
		cases = {
			"unknown transaction": (recorded("status_unknown_transaction"), 404, "REQUEST_BAD_INFOS"),
			"any other value": (recorded("unknown_answer"), 200, "SOMETHING_ELSE"),
			"unknown transaction status": (recorded("status_unknown_value"), 200, "status REVERSED"),
			"unreadable body": (recorded("not_json"), 200, None),
			"a JSON list": (FakeResponse(200, ["REQUEST_OK"]), 200, None),
			"no data": (FakeResponse(200, {"status": "REQUEST_OK", "data": None}), 200, "status None"),
			"server error": (recorded("server_error"), 503, "REQUEST_BAD_INFOS"),
			"REQUEST_OK under an error code": (
				FakeResponse(500, RECORDED["status_success"]["body"]),
				500,
				"REQUEST_OK",
			),
			"redirect": (FakeResponse(302, None), 302, None),
			"timeout": (requests.ReadTimeout(), None, None),
		}
		for case, (answer, http_status, code) in cases.items():
			with self.subTest(case), self.assertRaises(NokashError) as failed:
				client(answer)[0].check_status(TRANSACTION_ID)
			self.assertEqual((failed.exception.http_status, failed.exception.code), (http_status, code))
