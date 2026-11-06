# Copyright (c) 2026, Neotec Integrated Solutions
# License: see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from neotec_remittance.utils.money import parse_rate


class RemittanceRate(Document):
	"""Rates are append-only: publish a new row to change a rate; history is never edited."""

	def validate(self):
		if not self.is_new():
			frappe.throw(_("Rates are append-only. Publish a new rate instead of editing."))
		if self.base_currency == self.quote_currency:
			frappe.throw(_("Base and quote currency must differ"))
		self.mid_rate = format(parse_rate(self.mid_rate, "Mid rate"), "f")

	def on_trash(self):
		if frappe.db.exists("Remittance Quote", {"rate_record": self.name}):
			frappe.throw(_("Rates used by quotes cannot be deleted"))
