// Copyright (c) 2026, Neotec Integrated Solutions
frappe.ui.form.on("Remittance Funding Receipt", {
	refresh(frm) {
		const d = frm.doc;
		const call = (method, args, msg) =>
			frappe.call({ method: "neotec_remittance.api.ops." + method, args, freeze: true }).then(() => {
				frappe.show_alert({ message: __(msg), indicator: "green" });
				frm.reload_doc();
			});
		const unallocated = flt(d.amount) - flt(d.allocated_amount);
		if (unallocated > 0 && ["Unallocated", "Partially Allocated"].includes(d.status)) {
			frm.add_custom_button(__("Allocate to Transfer"), () =>
				frappe.prompt({ fieldname: "transfer", fieldtype: "Link", options: "Remittance Transfer", label: __("Transfer"), reqd: 1 },
					(v) => call("allocate_receipt", { receipt: d.name, transfer: v.transfer }, "Allocated")), __("Actions"));
			frm.add_custom_button(__("Refund Unallocated Amount"), () =>
				frappe.confirm(__("Refund {0} {1} to the payer?", [unallocated, d.currency]),
					() => call("refund_receipt_excess", { receipt: d.name }, "Refund pending")), __("Actions"));
		}
		if (d.status === "Refund Pending") {
			frm.add_custom_button(__("Confirm Refund Paid"), () =>
				frappe.prompt({ fieldname: "ref", fieldtype: "Data", label: __("Bank reference"), reqd: 1 },
					(v) => call("confirm_receipt_refund", { receipt: d.name, bank_reference: v.ref }, "Refund confirmed")), __("Actions"));
		}
	},
});
