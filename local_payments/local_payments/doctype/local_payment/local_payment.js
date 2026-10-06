// Copyright (c) 2026, SkyEngPro and contributors
// For license information, please see license.txt

frappe.ui.form.on("Local Payment", {
	refresh(frm) {
		if (!frm.doc.__onload?.can_retry_authorization) return;

		frm.add_custom_button(__("Retry authorization"), () =>
			frm
				.call({
					method: "retry_authorization",
					args: { name: frm.doc.name },
					freeze: true,
				})
				.then((r) => {
					if (r.message === "Done") {
						frappe.show_alert({
							message: __("Authorization done."),
							indicator: "green",
						});
					} else if (r.message === "Failed") {
						frappe.show_alert({
							message: __(
								"Authorization failed again. See the error on the session."
							),
							indicator: "red",
						});
					}
					frm.reload_doc();
				})
		);
	},
});
