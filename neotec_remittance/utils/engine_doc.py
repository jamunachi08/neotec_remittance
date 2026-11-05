import frappe
from frappe import _
from frappe.model.document import Document


class EngineDocument(Document):
	"""Engine-owned records are created and changed only by service code through parameterised SQL.
	Any attempt to save or delete them as documents (Desk, REST, data import, console) is refused."""

	def validate(self):
		frappe.throw(_("{0} records are managed by the remittance engine and cannot be edited").format(_(self.doctype)),
			frappe.PermissionError)

	def on_trash(self):
		frappe.throw(_("{0} records cannot be deleted").format(_(self.doctype)), frappe.PermissionError)
