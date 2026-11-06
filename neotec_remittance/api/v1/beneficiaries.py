import frappe

from neotec_remittance.api.auth import current_customer
from neotec_remittance.api.common import endpoint
from neotec_remittance.services import beneficiaries
from neotec_remittance.services.settings import require_enabled
from neotec_remittance.services.transfers import check_customer_eligible


@frappe.whitelist(allow_guest=True, methods=["POST"])
@endpoint
def create(full_name=None, country=None, currency=None, account_number=None, bank_code=None, nickname=None,
		full_name_ar=None, relationship=None, address=None, beneficiary_id=None):
	"""POST /v1/beneficiaries — creates a recipient or a new immutable version of one."""
	require_enabled()
	c = current_customer()
	check_customer_eligible(c)
	return beneficiaries.create_or_version(c, {"full_name": full_name, "country": country, "currency": currency,
		"account_number": account_number, "bank_code": bank_code, "nickname": nickname, "full_name_ar": full_name_ar,
		"relationship": relationship, "address": address, "beneficiary_id": beneficiary_id}), 201


@frappe.whitelist(allow_guest=True, methods=["GET"])
@endpoint
def list():  # noqa: A001
	c = current_customer()
	return beneficiaries.list_for_customer(c.name)
