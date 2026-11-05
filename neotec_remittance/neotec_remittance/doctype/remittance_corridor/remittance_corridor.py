# Copyright (c) 2026, Neotec Integrated Solutions
# License: see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from neotec_remittance.utils.money import D, parse_percent

MATERIAL = ("entity", "partner", "payout_method", "send_country", "send_currency", "receive_country", "receive_currency",
	"min_amount", "max_amount", "fx_markup_percent", "quote_ttl_seconds", "fee_fixed", "fee_percent", "fee_min",
	"fee_max", "fee_vat_applicable", "charge_bearer", "effective_from", "effective_to", "priority")


class RemittanceCorridor(Document):
	def validate(self):
		parse_percent(self.fx_markup_percent, "FX markup %")
		parse_percent(self.fee_percent, "Fee %")
		if D(self.min_amount) <= 0 or D(self.max_amount) < D(self.min_amount):
			frappe.throw(_("Amount range is invalid"))
		if D(self.fee_max) and D(self.fee_max) < D(self.fee_min):
			frappe.throw(_("Fee maximum is below fee minimum"))
		if (self.quote_ttl_seconds or 0) < 30:
			frappe.throw(_("Quote validity must be at least 30 seconds"))
		if self.effective_to and self.effective_to <= self.effective_from:
			frappe.throw(_("Effective To must be after Effective From"))
		if self.send_currency == self.receive_currency and D(self.fx_markup_percent):
			frappe.throw(_("FX markup does not apply to same-currency corridors"))
		if not self.is_new():
			before = self.get_doc_before_save()
			if before and any(str(before.get(f) or "") != str(self.get(f) or "") for f in MATERIAL):
				self.version = (before.version or 1) + 1
		if self.enabled:
			from neotec_remittance.services.accounts import ensure_chart

			ensure_chart(self.entity, self.send_currency, self.receive_currency, self.partner)
