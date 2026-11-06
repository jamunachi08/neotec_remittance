# Copyright (c) 2026, Neotec Integrated Solutions
# License: see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from neotec_remittance.utils.money import D, is_exact


class RemittanceAdjustment(Document):
	"""Controlled manual subledger postings (treasury prefunding, fee sweeps, corrections).
	Maker-checker: the submitter must differ from the creator. Posting is a new journal; nothing is edited."""

	def validate(self):
		dr = cr = D(0)
		for row in self.lines:
			acc = frappe.db.get_value("Remittance Account", row.account, ["entity", "currency", "disabled"], as_dict=True)
			if acc.entity != self.entity or acc.currency != self.currency:
				frappe.throw(_("Row {0}: account belongs to {1}/{2}").format(row.idx, acc.entity, acc.currency))
			d, c = D(row.debit), D(row.credit)
			if (d and c) or (d < 0 or c < 0) or not (d or c):
				frappe.throw(_("Row {0}: enter exactly one positive amount").format(row.idx))
			if not (is_exact(d, self.currency) and is_exact(c, self.currency)):
				frappe.throw(_("Row {0}: too many decimals for {1}").format(row.idx, self.currency))
			dr, cr = dr + d, cr + c
		if dr != cr:
			frappe.throw(_("Adjustment is not balanced: debit {0}, credit {1}").format(dr, cr))

	def before_submit(self):
		if frappe.session.user == self.owner and not self.flags.ignore_maker_checker:
			frappe.throw(_("The approver must be different from the preparer (maker-checker)"), frappe.PermissionError)
		self.approved_by = frappe.session.user

	def on_submit(self):
		from neotec_remittance.services.ledger import post_journal

		journal = post_journal(f"adjustment:{self.name}", f"adjustment:{self.adjustment_type}", self.entity, self.currency,
			[{"account": r.account, "debit": D(r.debit), "credit": D(r.credit)} for r in self.lines],
			transfer=self.transfer, accounting_date=self.accounting_date, narration=self.justification)
		self.db_set("journal", journal)

	def on_cancel(self):
		from neotec_remittance.services.ledger import reverse_journal

		reverse_journal(self.journal, f"adjustment_reversal:{self.name}", narration=f"Cancellation of {self.name}")
