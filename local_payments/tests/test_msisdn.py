# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

import unittest

from local_payments.providers.msisdn import MTN, NETWORK_PREFIXES, ORANGE, is_on_network, normalize_msisdn

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


class TestNetworks(unittest.TestCase):
	def number(self, national):
		return f"237{national}"

	def test_every_listed_prefix_belongs_to_its_network_only(self):
		for network, prefixes in NETWORK_PREFIXES.items():
			other = ORANGE if network == MTN else MTN
			for prefix in prefixes:
				national = (prefix + "00000000")[:9]
				with self.subTest(network=network, prefix=prefix):
					self.assertTrue(is_on_network(self.number(national), "237", network))
					self.assertFalse(is_on_network(self.number(national), "237", other))

	def test_the_prefixes_are_the_ones_cameroon_assigns(self):
		self.assertEqual(NETWORK_PREFIXES[ORANGE], ("69", "655", "656", "657", "658", "659"))
		self.assertEqual(NETWORK_PREFIXES[MTN], ("67", "650", "651", "652", "653", "654", "681"))

	def test_a_number_that_starts_like_no_listed_prefix_is_on_no_network(self):
		# 660-664 and 680, 682 are not in the lists, nor is 62 (Camtel) or 2.
		for national in ("660000000", "664000000", "680000000", "682000000", "620000000", "222000000"):
			with self.subTest(national=national):
				for network in (MTN, ORANGE):
					self.assertFalse(is_on_network(self.number(national), "237", network))

	def test_the_prefix_is_read_from_the_start_of_the_national_number(self):
		# 237 + 655...: the country code must not be mistaken for part of the prefix.
		self.assertTrue(is_on_network("237655000000", "237", ORANGE))
		self.assertFalse(is_on_network("237655000000", "237", MTN))
		self.assertTrue(is_on_network("237650000000", "237", MTN))
