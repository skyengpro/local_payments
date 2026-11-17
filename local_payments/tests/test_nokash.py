# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

import json
import traceback
import unittest
from dataclasses import dataclass, replace
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
	callback_signature_valid,
	sign,
)

RECORDED = json.loads((Path(__file__).parent / "fixtures" / "nokash.json").read_text())

CONFIG = NokashConfig(
	api_base_url="https://api.nokash.app/",
	i_space_key="fake-i-key",
	app_space_key="fake-app-key",
	integration_key="fake-integration-key",
)

# The same contract once Nokash recognises it by its address.
CONFIG_WITHOUT_INTEGRATION_KEY = replace(CONFIG, integration_key=None)

ATTEMPT_ID = "3f5c1c6e-8f4b-4a57-9d9a-3b1f5f0f2a11"
MSISDN = "237100000001"
NATIONAL = "100000001"
TRANSACTION_ID = "nk_tx_xm5ojhd01lkv06c0bjvu"
TRANSACTION_ID_611 = TRANSACTION_ID


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


def answering(**data):
	"""The recorded success, with some of its data replaced."""
	body = RECORDED["status_success"]["body"]
	return FakeResponse(200, {**body, "data": {**body["data"], **data}})


def client(answer, on_exchange=None, config=CONFIG):
	http = FakeHttp(answer)
	return NokashClient(config, http, on_exchange), http


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
		# A Currency field reads as a float; the payer may type the national number alone.
		nokash.payin(ATTEMPT_ID, 1000.0, NATIONAL, ORANGE_MONEY)

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
				"user_data": {"user_phone": MSISDN},
			},
		)
		self.assertIs(type(body["amount"]), int)
		self.assertNotIn("callback_url", body)
		self.assertEqual(
			call.kwargs["headers"]["hmac-signature"],
			sign(ATTEMPT_ID, 1000, MSISDN, CONFIG.app_space_key, CONFIG.i_space_key),
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
		signature = sign(ATTEMPT_ID, 1000, MSISDN, CONFIG.app_space_key, CONFIG.i_space_key)
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
		self.assertNotIn(CONFIG.integration_key, repr(CONFIG))

	def test_every_authenticated_call_carries_the_integration_key_only_while_it_is_set(self):
		"""Without the header Nokash answers 403 to the payin and to the status query alike."""
		for configured, expected in (
			(CONFIG, CONFIG.integration_key),
			(CONFIG_WITHOUT_INTEGRATION_KEY, None),
		):
			with self.subTest("set" if expected else "not set"):
				# One answer replayed for both calls, so it has to read as a status too.
				nokash, http = client(recorded("status_success"), config=configured)
				nokash.payin(ATTEMPT_ID, 1000, MSISDN, MTN_MOMO)
				nokash.check_status(TRANSACTION_ID, ATTEMPT_ID)

				self.assertEqual(
					[call.kwargs["headers"].get("X-NK-Integration") for call in http.calls],
					[expected, expected],
				)

	def test_the_integration_key_never_reaches_the_network_log(self):
		exchanges = []
		echoed = FakeResponse(400, None, text=f"Unauthorized {CONFIG.integration_key}")
		client(echoed, exchanges.append)[0].payin(ATTEMPT_ID, 1000, MSISDN, MTN_MOMO)
		self.assertNotIn(CONFIG.integration_key, repr(exchanges[0]))


class TestStatus(unittest.TestCase):
	def test_the_call_is_check_status_611_with_the_key_in_the_form(self):
		nokash, http = client(recorded("status_611_success"))
		nokash.check_status(TRANSACTION_ID_611, ATTEMPT_ID)

		call = http.calls[0]
		self.assertEqual(
			(call.method, call.url),
			("POST", "https://api.nokash.app/lapas-on-trans/trans/611/status-request"),
		)
		# In the body: a URL ends up in the message of a failed call, and so in a log.
		self.assertEqual(
			call.kwargs["data"], {"i_space_key": CONFIG.i_space_key, "transaction_id": TRANSACTION_ID_611}
		)
		self.assertNotIn("params", call.kwargs)
		self.assertNotIn("json", call.kwargs)
		self.assertEqual(call.kwargs["headers"], {"X-NK-Integration": CONFIG.integration_key})
		self.assertFalse(call.kwargs["allow_redirects"])

	def test_a_failed_call_leaves_the_key_out_of_the_traceback(self):
		# What requests says of a failed call: the address it went to, and what went with it.
		leaky = requests.ConnectionError(
			f"Max retries exceeded with url: /611?i_space_key={CONFIG.i_space_key}"
		)
		with self.assertRaises(NokashError) as failed:
			client(leaky)[0].check_status(TRANSACTION_ID_611, ATTEMPT_ID)

		logged = "".join(traceback.format_exception(failed.exception))
		self.assertNotIn(CONFIG.i_space_key, logged)
		self.assertNotIn(CONFIG.integration_key, logged)

	def test_a_403_is_a_refusal_of_the_key_and_any_other_failure_is_not(self):
		for name, refused in (
			("status_611_refused", True),
			("status_611_no_key", False),
			("server_error", False),
		):
			with self.subTest(name), self.assertRaises(NokashError) as failed:
				client(recorded(name))[0].check_status(TRANSACTION_ID_611, ATTEMPT_ID)
			self.assertEqual(failed.exception.refused, refused)
		with self.assertRaises(NokashError) as failed:
			client(requests.ReadTimeout())[0].check_status(TRANSACTION_ID_611, ATTEMPT_ID)
		self.assertFalse(failed.exception.refused)

	def test_a_recorded_611_success_is_read(self):
		result = client(recorded("status_611_success"))[0].check_status(TRANSACTION_ID_611, ATTEMPT_ID)
		self.assertEqual(
			(result.status, result.currency, result.transaction_id), (lc.SUCCEEDED, "XAF", TRANSACTION_ID_611)
		)
		# "1000.0", as Nokash writes it: the same amount as the session's 1000.
		self.assertTrue(lc.amounts_match(1000, result.amount))
		self.assertFalse(lc.amounts_match(1000, "1000.5"))

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
				result = client(recorded(name))[0].check_status(TRANSACTION_ID, ATTEMPT_ID)
				self.assertEqual((result.status, result.provider_status), (status, provider_status))

	def test_a_success_carries_what_the_session_is_compared_with(self):
		result = client(recorded("status_success"))[0].check_status(TRANSACTION_ID, ATTEMPT_ID)
		self.assertTrue(lc.amounts_match(1000.0, result.amount))
		self.assertEqual((result.currency, result.transaction_id), ("XAF", TRANSACTION_ID))

	def test_an_answer_that_names_no_transaction_or_no_attempt_proves_nothing(self):
		for field in ("id", "orderId"):
			for value in (None, ""):
				with self.subTest(field=field, value=value), self.assertRaises(NokashError) as failed:
					client(answering(**{field: value}))[0].check_status(TRANSACTION_ID, ATTEMPT_ID)
				self.assertEqual(failed.exception.code, f"{field} mismatch")

	def test_anything_else_is_a_failed_check(self):
		cases = {
			"unknown transaction": (recorded("status_unknown_transaction"), 404, "REQUEST_BAD_INFOS"),
			"any other value": (recorded("unknown_answer"), 200, "SOMETHING_ELSE"),
			"unknown transaction status": (recorded("status_unknown_value"), 200, "status REVERSED"),
			"unreadable body": (recorded("not_json"), 200, None),
			"a JSON list": (FakeResponse(200, ["REQUEST_OK"]), 200, None),
			"no data": (FakeResponse(200, {"status": "REQUEST_OK", "data": None}), 200, "id mismatch"),
			"server error": (recorded("server_error"), 503, "REQUEST_BAD_INFOS"),
			"REQUEST_OK under an error code": (
				FakeResponse(500, RECORDED["status_success"]["body"]),
				500,
				"REQUEST_OK",
			),
			"redirect": (FakeResponse(302, None), 302, None),
			"timeout": (requests.ReadTimeout(), None, None),
			"another transaction": (answering(id="nk_tx_other"), 200, "id mismatch"),
			"another attempt": (answering(orderId="another-attempt"), 200, "orderId mismatch"),
		}
		for case, (answer, http_status, code) in cases.items():
			with self.subTest(case), self.assertRaises(NokashError) as failed:
				client(answer)[0].check_status(TRANSACTION_ID, ATTEMPT_ID)
			self.assertEqual((failed.exception.http_status, failed.exception.code), (http_status, code))


CALLBACK_KEY = "fake-i-key"
# The body and signed string of the callback specification's example, signed with the key above.
CALLBACK_BODY = {
	"id": TRANSACTION_ID,
	"orderId": "CMD-2026-000123",
	"amount": "1000",
	"status": "SUCCESS",
	"statusReason": None,
	"initiatedAt": "2026-11-12T10:15:02",
	"phone": "237690000000",
}
CALLBACK_SIGNATURE = "221bf956177017dd1bef1dc5bc7903d54580073064c7cf3f05fd3a4ceda3ce8c"
CALLBACK_HEADER = f"t=1762956000,s={CALLBACK_SIGNATURE}"


class TestCallbackSignature(unittest.TestCase):
	def valid(self, body=CALLBACK_BODY, header=CALLBACK_HEADER, key=CALLBACK_KEY):
		return callback_signature_valid(body, header, key)

	def test_the_specifications_example_is_valid(self):
		self.assertTrue(self.valid())

	def test_only_s_is_the_signature_and_t_is_not_signed(self):
		for header in (
			f"s={CALLBACK_SIGNATURE}",
			f"t=1,s={CALLBACK_SIGNATURE}",
			f"t=999999999999,s={CALLBACK_SIGNATURE}",
			f" t=1762956000 , s={CALLBACK_SIGNATURE}",
		):
			with self.subTest(header=header):
				self.assertTrue(self.valid(header=header))

	def test_whatever_is_signed_cannot_be_changed(self):
		for field, forged in (
			("id", "nk_tx_other"),
			("amount", "5000"),
			("orderId", "CMD-2026-000999"),
			("status", "FAILED"),
			("phone", "237690000001"),
		):
			with self.subTest(field=field):
				self.assertFalse(self.valid(body={**CALLBACK_BODY, field: forged}))

	def test_what_is_not_signed_is_not_trusted_but_does_not_break_the_signature(self):
		body = {**CALLBACK_BODY, "statusReason": "changed", "initiatedAt": "2020-01-01T00:00:00"}
		self.assertTrue(self.valid(body=body))

	def test_a_missing_value_is_written_null(self):
		body = {**CALLBACK_BODY, "id": None}
		signature = "224ed669b8a274f4efbf8e2b6785fabd8623ac25c5f1a07828afba71d5c4dd5d"
		# Signed with phone null and id present: not this body.
		self.assertFalse(self.valid(body=body, header=f"s={signature}"))
		self.assertFalse(self.valid(body={**CALLBACK_BODY, "phone": None}, header=f"s={CALLBACK_SIGNATURE}"))
		self.assertTrue(self.valid(body={**CALLBACK_BODY, "phone": None}, header=f"s={signature}"))

	def test_a_number_is_signed_as_its_digits(self):
		self.assertTrue(self.valid(body={**CALLBACK_BODY, "amount": 1000}))
		self.assertFalse(self.valid(body={**CALLBACK_BODY, "amount": 1000.0}))
		self.assertFalse(self.valid(body={**CALLBACK_BODY, "amount": True}))
		self.assertFalse(self.valid(body={**CALLBACK_BODY, "amount": ["1000"]}))

	def test_anything_unexpected_is_invalid_and_never_raises(self):
		no_phone = {k: v for k, v in CALLBACK_BODY.items() if k != "phone"}
		cases = {
			"no header": dict(header=None),
			"empty header": dict(header=""),
			"no s": dict(header="t=1762956000"),
			"empty s": dict(header="t=1762956000,s="),
			"not a header": dict(header="garbage"),
			"upper-case hex": dict(header=f"s={CALLBACK_SIGNATURE.upper()}"),
			"non-ASCII s": dict(header="s=é" * 32),
			"another key": dict(key="another-key"),
			"no key": dict(key=""),
			"body is a list": dict(body=[CALLBACK_BODY]),
			"body is None": dict(body=None),
			"a bank callback, which is not offered": dict(body=no_phone),
		}
		for name, change in cases.items():
			with self.subTest(name):
				self.assertFalse(self.valid(**change))
