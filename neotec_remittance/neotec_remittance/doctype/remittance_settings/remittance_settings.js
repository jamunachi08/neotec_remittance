// Copyright (c) 2026, Neotec Integrated Solutions
frappe.ui.form.on("Remittance Settings", {
	refresh(frm) {
		const call = (method, args) => frappe.call({ method: "neotec_remittance.api.ops." + method, args, freeze: true });
		frm.add_custom_button(__("Verify Ledger Chain"), () =>
			call("verify_ledger_chain").then((r) => {
				frappe.msgprint(r.message.ok ? __("Ledger chain verified: {0} journals", [r.message.checked])
					: __("Integrity problems:") + "<br>" + r.message.problems.slice(0, 10).join("<br>"));
				frm.reload_doc();
			}), __("Controls"));
		frm.add_custom_button(__("Run Safeguarding Check"), () =>
			call("run_safeguarding_check").then((r) => frappe.msgprint((r.message || []).length ? r.message.join("<br>") : __("No shortfall"))),
			__("Controls"));
		if (frm.doc.dispatch_paused) {
			frm.dashboard.set_headline_alert(__("Dispatch is PAUSED: {0}", [frm.doc.paused_reason || ""]), "red");
			frm.add_custom_button(__("Resume Dispatch"), () =>
				frappe.confirm(__("Resume sending bank instructions?"), () => call("set_dispatch_pause", { paused: 0 }).then(() => frm.reload_doc())),
				__("Controls"));
		} else {
			frm.add_custom_button(__("Emergency Stop"), () =>
				frappe.prompt({ fieldname: "reason", fieldtype: "Small Text", label: __("Reason"), reqd: 1 },
					(v) => call("set_dispatch_pause", { paused: 1, reason: v.reason }).then(() => frm.reload_doc())),
				__("Controls"));
		}
	},
});
