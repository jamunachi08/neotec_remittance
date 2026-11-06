# Copyright (c) 2026, Neotec Integrated Solutions
# License: see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from neotec_remittance.services.accounts import CUSTOMER_MONEY, DEBIT_NORMAL, PARTNER_SCOPED, resolution_key

IDENTITY = ("entity", "currency", "account_role", "partner")
ENGINE_FIELDS = ("balance", "reserved", "row_version", "balance_refreshed_at")


class RemittanceAccount(Document):
	def validate(self):
		if self.account_role not in PARTNER_SCOPED:
			self.partner = None
		elif not self.partner:
			frappe.throw(_("Partner is required for {0}").format(self.account_role))
		self.resolution_key = resolution_key(self.entity, self.currency, self.account_role, self.partner)
		self.normal_side = "Debit" if self.account_role in DEBIT_NORMAL else "Credit"
		self.is_customer_money = 1 if self.account_role in CUSTOMER_MONEY else 0
		if self.erp_account:
			acc = frappe.db.get_value("Account", self.erp_account, ["company", "account_currency", "is_group"], as_dict=True)
			if acc.company != self.entity:
				frappe.throw(_("ERPNext account must belong to {0}").format(self.entity))
			if acc.is_group:
				frappe.throw(_("ERPNext account cannot be a group account"))
			if acc.account_currency and acc.account_currency != self.currency:
				frappe.throw(_("ERPNext account currency {0} differs from {1}").format(acc.account_currency, self.currency))
		if not self.is_new():
			before = frappe.db.get_value(self.doctype, self.name, list(IDENTITY) + list(ENGINE_FIELDS), as_dict=True)
			for f in ENGINE_FIELDS:
				self.set(f, before.get(f))  # engine-maintained; never accepted from the form or REST
			if frappe.db.exists("Remittance Ledger Line", {"account": self.name}):
				for f in IDENTITY:
					if (self.get(f) or None) != (before.get(f) or None):
						frappe.throw(_("{0} cannot change after postings exist").format(self.meta.get_label(f)))

	def on_trash(self):
		if frappe.db.exists("Remittance Ledger Line", {"account": self.name}):
			frappe.throw(_("Accounts with postings cannot be deleted; disable instead"))
