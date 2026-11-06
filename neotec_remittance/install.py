"""Idempotent installation and migration seeders."""
import frappe

ROLES = ["Remittance Manager", "Remittance Operator", "Remittance Compliance Officer",
	"Remittance Compliance Approver", "Remittance Finance", "Remittance Auditor"]

INDEXES = {
	"Remittance Outbox": [("status", "next_attempt_at")],
	"Remittance Inbox": [("status", "received_at")],
	"Remittance Transfer": [("customer", "accepted_at"), ("funding_state", "payout_state"), ("payout_state", "dispatched_at")],
	"Remittance Ledger Line": [("entity", "currency", "account_role")],
	"Remittance Journal": [("sealed", "creation"), ("erp_status", "sealed", "chain_seq")],
	"Remittance Rate": [("base_currency", "quote_currency", "valid_from")],
	"Remittance Corridor": [("entity", "send_currency", "receive_country", "receive_currency")],
}


def after_install():
	ensure_roles()
	ensure_index_key()
	after_migrate()


def after_migrate():
	ensure_roles()
	ensure_indexes()
	ensure_custom_fields()
	from neotec_remittance.services import integrity

	integrity.install_triggers()
	frappe.db.commit()


def ensure_roles():
	for role in ROLES:
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(ignore_permissions=True)


def ensure_index_key():
	if not frappe.conf.get("neotec_remittance_index_key"):
		from frappe.installer import update_site_config

		update_site_config("neotec_remittance_index_key", frappe.generate_hash(length=64))


def ensure_indexes():
	for doctype, groups in INDEXES.items():
		for fields in groups:
			name = "nr_" + "_".join(f[:12] for f in fields)
			try:
				frappe.db.add_index(doctype, list(fields), index_name=name)
			except Exception:
				frappe.log_error(title=f"Neotec Remittance: index {name} on {doctype}")


def ensure_custom_fields():
	"""Adds the export-id field to Journal Entry.

	ignore_validate: adding a field normally re-validates every existing field on Journal Entry, so an
	unrelated broken customisation on the site (e.g. a Link to a DocType that no longer exists) would abort
	this install. The field is optional: duplicate ERP posting is prevented by the unique export_id on
	Remittance ERP Posting, so a failure here is logged and never blocks install or migrate."""
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	try:
		create_custom_fields({"Journal Entry": [{"fieldname": "neotec_remittance_export_id",
			"label": "Neotec Remittance Export ID", "fieldtype": "Data", "unique": 1, "read_only": 1, "no_copy": 1,
			"insert_after": "user_remark", "print_hide": 1}]}, ignore_validate=True, update=True)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="Neotec Remittance: could not add export id field to Journal Entry")
	report_broken_links("Journal Entry")


def report_broken_links(doctype: str):
	"""Warn (do not fail) about Link/Table fields on a DocType whose target DocType does not exist."""
	broken = []
	for df in frappe.get_meta(doctype).fields:
		if df.fieldtype in ("Link", "Table", "Table MultiSelect") and df.options and not frappe.db.exists("DocType", df.options):
			broken.append(f"{df.label or df.fieldname} ({df.fieldname}) -> missing DocType '{df.options}'")
	if broken:
		msg = (f"{doctype} has fields linking to DocTypes that do not exist on this site. Neotec Remittance installed "
			f"anyway, but Customize Form and other apps will fail on {doctype} until these are fixed:\n" + "\n".join(broken))
		frappe.log_error(title=f"Neotec Remittance: broken links on {doctype}", message=msg)
		print(msg)
