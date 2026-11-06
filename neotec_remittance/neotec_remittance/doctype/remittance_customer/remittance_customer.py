# Copyright (c) 2026, Neotec Integrated Solutions
# License: see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from neotec_remittance.utils import crypto
from neotec_remittance.utils.names import mask_tail

KYC_ROLES = {"Remittance Compliance Officer", "Remittance Compliance Approver", "System Manager"}


class RemittanceCustomer(Document):
	def validate(self):
		if self.id_number_input:
			value = "".join(self.id_number_input.split())
			self.id_number_enc = crypto.encrypt(value)
			self.id_number_index = crypto.blind_index(value, f"customer_id:{self.id_type or ''}")
			self.id_number_masked = mask_tail(value)
			self.id_number_input = None
			self.flags.id_input_applied = True
			dup = frappe.db.get_value("Remittance Customer", {"id_number_index": self.id_number_index,
				"name": ("!=", self.name or "")}, "name")
			if dup:
				frappe.throw(_("Another customer is registered with this identity document ({0})").format(dup))
		before = None if self.is_new() else frappe.db.get_value(self.doctype, self.name,
			["kyc_status", "kyc_expiry", "risk_tier", "id_number_enc", "id_number_index", "id_number_masked"], as_dict=True)
		if before and not self.flags.id_input_applied:
			# encrypted identity fields change only through id_number_input
			for f in ("id_number_enc", "id_number_index", "id_number_masked"):
				self.set(f, before.get(f))
		changed_kyc = (self.is_new() and self.kyc_status != "Pending") or (before and (
			before.kyc_status != self.kyc_status or str(before.kyc_expiry or "") != str(self.kyc_expiry or "")
			or before.risk_tier != self.risk_tier))
		if changed_kyc and not (KYC_ROLES & set(frappe.get_roles())) and not self.flags.ignore_kyc_roles:
			frappe.throw(_("Only compliance can change KYC status, expiry or risk tier"), frappe.PermissionError)
		if self.kyc_status == "Verified" and not (self.kyc_expiry and self.id_number_enc and self.kyc_provider):
			frappe.throw(_("Verified KYC requires an identity document, provider and expiry date"))
		if self.kyc_status == "Verified" and not self.kyc_verified_on:
			self.kyc_verified_on = frappe.utils.today()

	def on_trash(self):
		if frappe.db.exists("Remittance Transfer", {"customer": self.name}):
			frappe.throw(_("Customers with transfers cannot be deleted; close the account instead"))
