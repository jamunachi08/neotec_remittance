// Copyright (c) 2026, Neotec Integrated Solutions
frappe.ui.form.on("Remittance Transfer", {
	refresh(frm) {
		const call = (method, args, msg) =>
			frappe.call({ method: "neotec_remittance.api.ops." + method, args, freeze: true }).then(() => {
				frappe.show_alert({ message: __(msg), indicator: "green" });
				frm.reload_doc();
			});
		const d = frm.doc;
		if (["Not Ready", "Ready", "Accepted", "Unknown"].includes(d.payout_state)) {
			frm.add_custom_button(__("Cancel / Recall"), () =>
				frappe.prompt({ fieldname: "reason", fieldtype: "Small Text", label: __("Reason"), reqd: 1 },
					(v) => call("staff_cancel", { transfer: d.name, reason: v.reason }, "Cancellation requested"),
					__("Cancel transfer")), __("Actions"));
		}
		if (d.refund_state === "Eligible" && d.payout_state !== "Returned") {
			frm.add_custom_button(__("Initiate Refund"), () => call("initiate_refund", { transfer: d.name }, "Refund initiated"), __("Actions"));
		}
		if (d.refund_state === "Eligible" && d.payout_state === "Returned") {
			frm.add_custom_button(__("Set Return Refund Amount"), () =>
				frappe.prompt({ fieldname: "amount", fieldtype: "Data", label: __("Refund amount ({0})", [d.send_currency]), reqd: 1 },
					(v) => call("set_return_refund", { transfer: d.name, refund_amount: v.amount }, "Return refund set")), __("Actions"));
		}
		if (d.refund_state === "Pending") {
			frm.add_custom_button(__("Confirm Refund Paid"), () =>
				frappe.prompt({ fieldname: "ref", fieldtype: "Data", label: __("Bank reference"), reqd: 1 },
					(v) => call("confirm_refund", { transfer: d.name, bank_reference: v.ref }, "Refund confirmed")), __("Actions"));
		}
		frm.set_intro(__("Customer status: {0}", [d.customer_status || "-"]), d.payout_state === "Unknown" ? "orange" : "blue");
	},
});
