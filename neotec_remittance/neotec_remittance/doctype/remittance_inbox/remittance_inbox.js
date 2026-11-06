// Copyright (c) 2026, Neotec Integrated Solutions
frappe.ui.form.on("Remittance Inbox", {
	refresh(frm) {
		if (frm.doc.status === "Quarantined") {
			frm.add_custom_button(__("Re-queue After Investigation"), () =>
				frappe.call({ method: "neotec_remittance.api.ops.requeue_inbox", args: { name: frm.doc.name } }).then(() => frm.reload_doc()));
		}
	},
});
