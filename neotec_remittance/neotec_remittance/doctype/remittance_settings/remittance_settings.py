# Copyright (c) 2026, Neotec Integrated Solutions
# License: see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

PROTECTED = ("screening_list_updated_at", "screening_list_version", "db_triggers_installed", "last_chain_verification",
	"chain_verification_status", "paused_at")


class RemittanceSettings(Document):
	def validate(self):
		before = self.get_doc_before_save()
		if before:
			for f in PROTECTED:
				self.set(f, before.get(f))
			if before.dispatch_paused != self.dispatch_paused:
				from neotec_remittance.utils.timeutil import utc_now

				self.paused_at = utc_now() if self.dispatch_paused else None
				if self.dispatch_paused and not self.paused_reason:
					frappe.throw(_("Pause reason is required"))
		if self.collection_account:
			role = frappe.db.get_value("Remittance Account", self.collection_account, "account_role")
			if role != "Safeguarded Bank":
				frappe.throw(_("Collection account must be a Safeguarded Bank account"))
		if self.zatca_mode == "Per Transfer Invoice" and not (self.fee_item and self.fee_customer and self.fee_receivable_account):
			frappe.throw(_("ZATCA fee invoicing needs fee item, fee customer and fee receivable account"))
		for raw in (self.status_poll_schedule or "").split(","):
			if raw.strip() and not raw.strip().isdigit():
				frappe.throw(_("Status poll schedule must be comma-separated seconds"))

	def on_update(self):
		frappe.clear_document_cache(self.doctype, self.name)
