import frappe


def note(doctype: str, name: str, message: str):
	"""Human-readable audit note on the record timeline (exceptional and manual actions only)."""
	frappe.get_doc({"doctype": "Comment", "comment_type": "Info", "reference_doctype": doctype,
		"reference_name": name, "content": message[:2000]}).insert(ignore_permissions=True)
