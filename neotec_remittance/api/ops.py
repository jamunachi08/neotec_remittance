"""Staff operations (Desk). Every method checks roles explicitly; none is reachable by customers."""
import frappe

from neotec_remittance.services import funding, outbox, refunds, safeguarding
from neotec_remittance.services.audit import note
from neotec_remittance.utils.money import D

FINANCE = ("Remittance Finance", "Remittance Manager", "System Manager")
OPS = ("Remittance Operator", "Remittance Manager", "System Manager")
MANAGER = ("Remittance Manager", "System Manager")


def _only(roles):
	frappe.only_for(roles)


@frappe.whitelist(methods=["POST"])
def record_receipt(bank_account, external_entry_id, amount, currency, value_date=None, reference_text=None,
		payer_name=None):
	_only(FINANCE)
	return funding.ingest_receipt(bank_account, external_entry_id, D(amount), currency, value_date, reference_text or "",
		payer_name or "", source=f"Manual by {frappe.session.user}")


@frappe.whitelist(methods=["POST"])
def allocate_receipt(receipt, transfer):
	_only(FINANCE)
	return funding.allocate(receipt, transfer, by=frappe.session.user)


@frappe.whitelist(methods=["POST"])
def refund_receipt_excess(receipt):
	_only(FINANCE)
	refunds.refund_receipt_excess(receipt, frappe.session.user)


@frappe.whitelist(methods=["POST"])
def confirm_receipt_refund(receipt, bank_reference):
	_only(FINANCE)
	refunds.confirm_receipt_refund(receipt, bank_reference, frappe.session.user)


@frappe.whitelist(methods=["POST"])
def confirm_refund(transfer, bank_reference):
	_only(FINANCE)
	refunds.confirm(transfer, bank_reference, frappe.session.user)


@frappe.whitelist(methods=["POST"])
def set_return_refund(transfer, refund_amount):
	_only(FINANCE)
	refunds.initiate_return_refund(transfer, refund_amount, frappe.session.user)


@frappe.whitelist(methods=["POST"])
def staff_cancel(transfer, reason):
	_only(OPS)
	if not reason:
		frappe.throw("Reason is required")
	return refunds.request_cancel(transfer, f"{frappe.session.user}: {reason}")


@frappe.whitelist(methods=["POST"])
def initiate_refund(transfer):
	_only(FINANCE)
	from neotec_remittance.services import state

	t = state.lock_transfer(transfer)
	refunds.initiate(t, f"manual by {frappe.session.user}")


@frappe.whitelist(methods=["POST"])
def redrive_outbox(name):
	_only(MANAGER)
	outbox.redrive(name)


@frappe.whitelist(methods=["POST"])
def requeue_inbox(name):
	_only(MANAGER)
	frappe.db.sql("UPDATE `tabRemittance Inbox` SET status='Received', attempts=0 WHERE name=%s AND status='Quarantined'", name)
	note("Remittance Inbox", name, f"Re-queued from quarantine by {frappe.session.user}")


@frappe.whitelist(methods=["POST"])
def set_dispatch_pause(paused, reason=None):
	_only(MANAGER)
	if int(paused):
		safeguarding.pause(reason or f"Paused by {frappe.session.user}")
	else:
		from neotec_remittance.services.settings import set_values

		set_values({"dispatch_paused": 0, "paused_reason": None, "paused_at": None})
	note("Remittance Settings", "Remittance Settings", f"Dispatch {'paused' if int(paused) else 'resumed'} by {frappe.session.user}: {reason or ''}")


@frappe.whitelist(methods=["POST"])
def run_safeguarding_check():
	_only(FINANCE)
	return safeguarding.run_checks()


@frappe.whitelist(methods=["POST"])
def verify_ledger_chain():
	frappe.only_for(("Remittance Auditor", "Remittance Finance", "Remittance Manager", "System Manager"))
	from neotec_remittance.services import integrity

	integrity.seal_chain()
	return integrity.verify_chain()
