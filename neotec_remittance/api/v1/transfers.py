import frappe

from neotec_remittance.api.auth import current_customer
from neotec_remittance.api.common import endpoint, header
from neotec_remittance.services import quotes, refunds, transfers


@frappe.whitelist(allow_guest=True, methods=["POST"])
@endpoint
def create(quote_id=None, beneficiary_version_id=None, authorization_id=None):
	"""POST /v1/transfers  (Idempotency-Key header required)"""
	c = current_customer()
	return transfers.accept(c, quote_id, beneficiary_version_id, authorization_id, header("Idempotency-Key"))


@frappe.whitelist(allow_guest=True, methods=["GET"])
@endpoint
def get(transfer_id=None):
	"""GET /v1/transfers/{id}"""
	c = current_customer()
	return transfers.transfer_response(transfers.get_for_customer(c, transfer_id))


@frappe.whitelist(allow_guest=True, methods=["GET"])
@endpoint
def list(limit=20, cursor=None):  # noqa: A001
	"""GET /v1/transfers?cursor="""
	c = current_customer()
	return transfers.list_for_customer(c, limit, cursor)


@frappe.whitelist(allow_guest=True, methods=["POST"])
@endpoint
def cancel(transfer_id=None):
	"""POST /v1/transfers/{id}/cancel — never promises final cancellation."""
	c = current_customer()
	transfers.get_for_customer(c, transfer_id)
	result = refunds.request_cancel(transfer_id, f"customer {c.name}")
	return result, 202


@frappe.whitelist(allow_guest=True, methods=["POST"])
@endpoint
def reconfirm(transfer_id=None, quote_id=None, authorization_id=None):
	"""POST /v1/transfers/{id}/reconfirm — accept fresh FX terms after late funding."""
	c = current_customer()
	return transfers.reconfirm(c, transfer_id, quote_id, authorization_id)


@frappe.whitelist(allow_guest=True, methods=["GET"])
@endpoint
def receipt(transfer_id=None):
	"""GET /v1/receipts/{id} — customer receipt data (rendered by the channel)."""
	c = current_customer()
	t = transfers.get_for_customer(c, transfer_id)
	frappe.get_doc({"doctype": "Comment", "comment_type": "Info", "reference_doctype": "Remittance Transfer",
		"reference_name": t.name, "content": f"Receipt accessed by customer {c.name}"}).insert(ignore_permissions=True)
	from neotec_remittance.utils import db

	bv = db.get_row("Remittance Beneficiary Version", t.beneficiary_version, "full_name, account_masked, country")
	out = transfers.transfer_response(t)
	out.pop("funding_instructions", None)
	out["beneficiary"] = {"name": bv.full_name, "account": bv.account_masked, "country": bv.country}
	out["sender"] = c.customer_name
	return out
