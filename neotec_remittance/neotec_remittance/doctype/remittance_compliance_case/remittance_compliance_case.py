# Copyright (c) 2026, Neotec Integrated Solutions
# License: see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from neotec_remittance.utils.timeutil import utc_now

REVIEWERS = {"Remittance Compliance Officer", "Remittance Compliance Approver"}
APPROVERS = {"Remittance Compliance Approver"}


class RemittanceComplianceCase(Document):
	"""Maker-checker: an officer recommends; a different approver decides. Decisions are final."""

	def validate(self):
		if self.flags.engine and self.is_new():
			return
		before = self.get_doc_before_save()
		if not before:
			if not self.flags.engine:
				frappe.throw(_("Compliance cases are opened by the screening engine"))
			return
		roles = set(frappe.get_roles())
		if before.status == "Decided":
			frappe.throw(_("Decided cases are closed"))
		if self.recommendation != before.recommendation:
			if before.status != "Open":
				frappe.throw(_("Recommendation is already recorded"))
			if not (roles & REVIEWERS):
				frappe.throw(_("Only compliance reviewers can recommend"), frappe.PermissionError)
			if not self.recommendation_note:
				frappe.throw(_("Recommendation note is required"))
			self.recommended_by, self.recommended_at, self.status = frappe.session.user, utc_now(), "Recommended"
		if self.decision != before.decision:
			if self.status != "Recommended":
				frappe.throw(_("A recommendation is required before a decision"))
			if not (roles & APPROVERS):
				frappe.throw(_("Only a compliance approver can decide"), frappe.PermissionError)
			if frappe.session.user == self.recommended_by:
				frappe.throw(_("The approver must be different from the reviewer (maker-checker)"), frappe.PermissionError)
			if not self.decision_note:
				frappe.throw(_("Decision note is required"))
			self.decided_by, self.decided_at, self.status = frappe.session.user, utc_now(), "Decided"
			self.flags.apply_decision = True

	def on_update(self):
		if self.flags.apply_decision:
			from neotec_remittance.services.screening import apply_decision

			apply_decision(self)

	def on_trash(self):
		frappe.throw(_("Compliance cases cannot be deleted"))
