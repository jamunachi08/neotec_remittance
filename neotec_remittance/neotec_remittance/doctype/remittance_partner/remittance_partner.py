# Copyright (c) 2026, Neotec Integrated Solutions
# License: see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class RemittancePartner(Document):
	def validate(self):
		if self.environment == "Production" and self.adapter == "Sandbox":
			frappe.throw(_("The Sandbox adapter cannot be used in Production"))
		if self.adapter == "Gateway" and self.enabled and not (self.gateway_url or "").startswith("https://"):
			frappe.throw(_("Gateway URL must use https://"))
		if self.adapter == "Custom" and not self.adapter_path:
			frappe.throw(_("Custom adapter class path is required"))
		if self.cap_not_found_is_final and not self.cap_status_inquiry:
			frappe.throw(_("'Not Found is final' requires status inquiry support"))
