# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

from local_payments import user_context as uc


class TestAsUser(IntegrationTestCase):
	def test_runs_the_block_as_that_user(self):
		with self.set_user("Guest"), uc.as_user("Administrator"):
			self.assertEqual(frappe.session.user, "Administrator")

	def test_puts_the_caller_back_after_the_block(self):
		with self.set_user("Guest"):
			frappe.local.form_dict = frappe._dict(token="t")
			session = frappe.local.session
			with uc.as_user("Administrator"):
				pass
			self.assertIs(frappe.local.session, session)
			self.assertEqual((frappe.session.user, frappe.session.sid), ("Guest", "Guest"))
			self.assertEqual(frappe.local.form_dict, {"token": "t"})

	def test_puts_the_caller_back_when_the_block_raises(self):
		with self.set_user("Guest"):
			with self.assertRaises(frappe.ValidationError), uc.as_user("Administrator"):
				frappe.throw("refused")
			self.assertEqual(frappe.session.user, "Guest")
