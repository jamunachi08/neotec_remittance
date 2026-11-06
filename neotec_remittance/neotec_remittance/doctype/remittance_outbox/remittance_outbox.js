// Copyright (c) 2026, Neotec Integrated Solutions
frappe.ui.form.on("Remittance Outbox", {
	refresh(frm) {
		if (frm.doc.status === "Exception" && frm.doc.command_type !== "create_payment") {
			frm.add_custom_button(__("Redrive (same command identity)"), () =>
				frappe.call({ method: "neotec_remittance.api.ops.redrive_outbox", args: { name: frm.doc.name } }).then(() => frm.reload_doc()));
		}
	},
});
