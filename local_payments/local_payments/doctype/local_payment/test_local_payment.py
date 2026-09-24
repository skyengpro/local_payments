# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

MANAGER_ROLE = "Local Payments Manager"
TEST_GATEWAY = "Test Local Payment Gateway"
TEST_USER = "lp-manager@example.com"
OTHER_USER = "lp-other@example.com"

READ_ONLY_FIELDS = (
	"status",
	"paid_amount",
	"provider_transaction_id",
	"paid_on",
	"authorization",
	"authorization_tries",
	"authorization_error",
	"authorization_next_retry_on",
	"authorization_alerted",
)

ATTEMPT_FIELDS = (
	"attempt_id",
	"status",
	"provider_status",
	"payer_msisdn",
	"provider_transaction_id",
	"confirmed_amount",
	"confirmed_currency",
	"provider_data",
	"duplicate",
	"amount_mismatch",
	"started_on",
	"expires_on",
	"last_checked_on",
	"next_check_on",
	"check_count",
	"integration_request",
	"alerted",
)


def make_user(email, roles):
	if frappe.db.exists("User", email):
		user = frappe.get_doc("User", email)
	else:
		user = frappe.get_doc({"doctype": "User", "email": email, "first_name": "LP Test"}).insert(
			ignore_permissions=True
		)
	user.roles = []
	user.add_roles(*roles)
	return user


def make_session(**overrides):
	doc = frappe.get_doc(
		{
			"doctype": "Local Payment",
			"payment_gateway": TEST_GATEWAY,
			"reference_doctype": "User",
			"reference_docname": "Administrator",
			"amount": 5000,
			"currency": "XAF",
			"title": "Test payment",
			"attempts": [
				{
					"attempt_id": frappe.generate_hash(length=36),
					"status": "Initiated",
					"payer_msisdn": "670000000",
					"started_on": frappe.utils.now_datetime(),
					"next_check_on": frappe.utils.now_datetime(),
				}
			],
			**overrides,
		}
	)
	return doc.insert(ignore_permissions=True)


class TestLocalPayment(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("Currency", "XAF"):
			frappe.get_doc({"doctype": "Currency", "currency_name": "XAF", "enabled": 1}).insert()
		if not frappe.db.exists("Payment Gateway", TEST_GATEWAY):
			frappe.get_doc({"doctype": "Payment Gateway", "gateway": TEST_GATEWAY}).insert()
		make_user(TEST_USER, [MANAGER_ROLE])
		make_user(OTHER_USER, ["Sales User"])

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_role_exists(self):
		self.assertTrue(frappe.db.exists("Role", MANAGER_ROLE))

	def test_session_with_one_attempt(self):
		doc = make_session()
		doc.reload()
		self.assertEqual(doc.status, "Open")
		self.assertEqual(len(doc.attempts), 1)
		self.assertEqual(doc.attempts[0].parenttype, "Local Payment")
		self.assertEqual(doc.attempts[0].parentfield, "attempts")

	def test_token_is_generated_and_independent_of_name(self):
		first, second = make_session(), make_session()
		for doc in (first, second):
			self.assertEqual(len(doc.token), 32)
			self.assertNotIn(doc.name, doc.token)
			self.assertNotIn(doc.token, doc.name)
		self.assertNotEqual(first.token, second.token)

	def test_caller_cannot_choose_token(self):
		doc = make_session(token="a" * 32)
		self.assertNotEqual(doc.token, "a" * 32)

	def test_token_and_attempt_id_are_unique(self):
		meta = frappe.get_meta("Local Payment")
		self.assertTrue(meta.get_field("token").unique)
		attempt_id = make_session().attempts[0].attempt_id
		with self.assertRaises(frappe.UniqueValidationError):
			doc = make_session()
			doc.attempts[0].attempt_id = attempt_id
			doc.save(ignore_permissions=True)

	def test_select_options_are_exact(self):
		meta = frappe.get_meta("Local Payment")
		self.assertEqual(meta.get_field("status").options.split("\n"), ["Open", "Paid", "Void"])
		self.assertEqual(
			meta.get_field("authorization").options.split("\n"), ["", "Pending", "Done", "Failed"]
		)

	def test_attempt_is_a_child_table_with_expected_fields(self):
		parent = frappe.get_meta("Local Payment")
		self.assertEqual(parent.get_field("attempts").options, "Local Payment Attempt")
		attempt = frappe.get_meta("Local Payment Attempt")
		self.assertTrue(attempt.istable)
		self.assertEqual(
			{f.fieldname for f in attempt.fields if f.fieldtype not in ("Section Break", "Column Break")},
			set(ATTEMPT_FIELDS),
		)
		self.assertEqual(
			attempt.get_field("status").options.split("\n"),
			["Initiated", "Pending", "Succeeded", "Failed", "Expired", "Unresolved", "Error"],
		)

	def test_attempt_indexes(self):
		attempt = frappe.get_meta("Local Payment Attempt")
		self.assertTrue(attempt.get_field("attempt_id").unique)
		self.assertTrue(attempt.get_field("attempt_id").search_index)
		self.assertTrue(attempt.get_field("next_check_on").search_index)
		# The 2-minute job narrows on status before it looks at the dates.
		self.assertTrue(attempt.get_field("status").search_index)

	def test_session_scheduler_indexes(self):
		# The hourly retry job reads this column on every run.
		session = frappe.get_meta("Local Payment")
		self.assertTrue(session.get_field("authorization_next_retry_on").search_index)

	def test_provider_data_is_read_only_code(self):
		field = frappe.get_meta("Local Payment Attempt").get_field("provider_data")
		self.assertEqual(field.fieldtype, "Code")
		self.assertTrue(field.read_only)

	def test_status_fields_are_read_only(self):
		meta = frappe.get_meta("Local Payment")
		for fieldname in READ_ONLY_FIELDS:
			self.assertTrue(meta.get_field(fieldname).read_only, fieldname)

	def test_no_provider_specific_field(self):
		names = {f.fieldname for f in frappe.get_meta("Local Payment").fields}
		names |= {f.fieldname for f in frappe.get_meta("Local Payment Attempt").fields}
		for name in names:
			self.assertFalse(name.startswith(("mtn", "orange", "momo", "om_")), name)

	def test_permission_rows(self):
		perms = frappe.get_meta("Local Payment").permissions
		self.assertEqual({p.role for p in perms}, {MANAGER_ROLE, "System Manager"})
		for perm in perms:
			self.assertTrue(perm.read)
			for right in ("create", "write", "delete", "submit", "cancel", "amend", "import"):
				self.assertFalse(perm.get(right), f"{perm.role} has {right}")

	def test_manager_can_read_but_not_create_or_delete(self):
		doc = make_session()
		frappe.set_user(TEST_USER)
		self.assertTrue(frappe.has_permission("Local Payment", "read", doc=doc))
		self.assertFalse(frappe.has_permission("Local Payment", "create"))
		self.assertFalse(frappe.has_permission("Local Payment", "write", doc=doc))
		self.assertFalse(frappe.has_permission("Local Payment", "delete", doc=doc))
		with self.assertRaises(frappe.PermissionError):
			frappe.delete_doc("Local Payment", doc.name)

	def test_unrelated_user_and_guest_cannot_read(self):
		doc = make_session()
		for user in (OTHER_USER, "Guest"):
			frappe.set_user(user)
			self.assertFalse(frappe.has_permission("Local Payment", "read", doc=doc), user)

	def test_only_administrator_can_delete(self):
		doc = make_session()
		frappe.delete_doc("Local Payment", doc.name)
		self.assertFalse(frappe.db.exists("Local Payment", doc.name))

	def test_amount_must_be_positive(self):
		with self.assertRaises(frappe.ValidationError):
			make_session(amount=0)
