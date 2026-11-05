import frappe

from neotec_remittance.api.auth import current_customer
from neotec_remittance.api.common import endpoint
from neotec_remittance.services import quotes
from neotec_remittance.services.settings import require_enabled
from neotec_remittance.services.transfers import check_customer_eligible


@frappe.whitelist(allow_guest=True, methods=["POST"])
@endpoint
def create(send_amount=None, receive_country=None, receive_currency=None, payout_method="Bank Account"):
	"""POST /v1/quotes"""
	require_enabled()
	c = current_customer()
	check_customer_eligible(c)
	return quotes.create_quote(c, send_amount, receive_country, receive_currency, payout_method), 201
