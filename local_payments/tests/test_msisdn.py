# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

import unittest

from local_payments.providers.msisdn import normalize_msisdn

MSISDN = "237600000001"


class TestNormalizeMsisdn(unittest.TestCase):
	def test_the_usual_ways_of_typing_a_number_give_the_same_msisdn(self):
		for typed in (
			"600000001",
			"6 00 00 00 01",
			"600-000-001",
			"600.000.001",
			MSISDN,
			"+237 600 000 001",
			"00237600000001",
			"(237) 600000001",
		):
			with self.subTest(typed=typed):
				self.assertEqual(normalize_msisdn(typed, "237", 9), MSISDN)

	def test_anything_else_is_none(self):
		for typed in (
			"",
			None,
			600000001,
			"60000000",
			"6000000011",
			"abc600000001",
			"+33600000001",
			"0237600000001",
		):
			with self.subTest(typed=typed):
				self.assertIsNone(normalize_msisdn(typed, "237", 9))
