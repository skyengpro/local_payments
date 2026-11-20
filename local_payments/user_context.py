# Copyright (c) 2026, SkyEngPro and contributors
# For license information, please see license.txt

"""Run a block as another user, then put the caller's request state back exactly as it was.

`frappe.set_user()` does not only change the user: it also replaces the session (and its `sid`), the
form data, the permission caches and the Jinja environments. Calling it a second time to go back
leaves the caller with an empty `form_dict` and a `sid` equal to the user name. `as_user()` saves
what `set_user()` replaces and restores it, even when the block raises.

This is the only place in the app that calls `frappe.set_user()`. Both callers (`erpnext.py` for
Administrator, `reconcile.py` for the session owner) are reached only after the provider has
confirmed the payment to the merchant's credentials (D3).
"""

from contextlib import contextmanager

import frappe

# What frappe.set_user() replaces on frappe.local, apart from the cache.
USER_STATE = (
	"session",
	"form_dict",
	"role_permissions",
	"new_doc_templates",
	"user_perms",
	"jenv_restricted",
	"jenv_unrestricted",
)


@contextmanager
def as_user(user: str):
	"""Run the block as `user`, then put the caller's user and request state back as they were."""
	saved = {name: getattr(frappe.local, name, None) for name in USER_STATE}
	# set_user() writes into the session object, so give it a copy and keep the original untouched.
	frappe.local.session = frappe._dict(frappe.local.session)
	try:
		# nosemgrep: frappe-setuser -- scoped to this block, caller's state restored in finally
		frappe.set_user(user)
		yield
	finally:
		for name, value in saved.items():
			setattr(frappe.local, name, value)
		# Start empty: the caller's cache may be stale, and this user's must not outlive the block.
		frappe.local.cache = {}
