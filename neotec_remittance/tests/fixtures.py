"""Shared fixtures for the Neotec Remittance test suite (sandbox partner, SAR->INR corridor)."""
from datetime import timedelta

import frappe

from neotec_remittance.services import accounts, funding, ledger, outbox, transfers
from neotec_remittance.services.ledger import Cr, Dr
from neotec_remittance.services.settings import set_values
from neotec_remittance.utils.timeutil import utc_now

COMPANY = "Neotec Remit Test Co"
PARTNER = "NR Sandbox Bank"
CORRIDOR = "NR-TEST-SA-IN"
CALLBACK_SECRET = "test-callback-secret-0123456789"
PREFUND = "100000000.00"


def setup_platform():
	if not frappe.db.exists("Company", COMPANY):
		frappe.get_doc({"doctype": "Company", "company_name": COMPANY, "abbr": "NRTC", "default_currency": "SAR",
			"country": "Saudi Arabia", "create_chart_of_accounts_based_on": "Standard Template",
			"chart_of_accounts": "Standard"}).insert(ignore_permissions=True)
	year = str(frappe.utils.getdate().year)
	if not frappe.db.exists("Fiscal Year", year):
		frappe.get_doc({"doctype": "Fiscal Year", "year": year, "year_start_date": f"{year}-01-01",
			"year_end_date": f"{year}-12-31"}).insert(ignore_permissions=True)
	if not frappe.db.exists("Remittance Partner", PARTNER):
		frappe.get_doc({"doctype": "Remittance Partner", "partner_name": PARTNER, "adapter": "Sandbox",
			"environment": "Sandbox", "enabled": 1, "callback_secret": CALLBACK_SECRET, "cap_status_inquiry": 1,
			"cap_recall": 1, "cap_webhook": 1, "cap_not_found_is_final": 1}).insert(ignore_permissions=True)
	if not frappe.db.exists("Remittance Corridor", CORRIDOR):
		frappe.get_doc({"doctype": "Remittance Corridor", "corridor_name": CORRIDOR, "entity": COMPANY, "enabled": 1,
			"priority": 1, "effective_from": utc_now() - timedelta(days=1), "partner": PARTNER,
			"payout_method": "Bank Account", "send_country": "Saudi Arabia", "send_currency": "SAR",
			"receive_country": "India", "receive_currency": "INR", "min_amount": 10, "max_amount": 50000,
			"fx_markup_percent": "0.50", "quote_ttl_seconds": 900, "fee_fixed": 15, "fee_percent": "0",
			"fee_min": 0, "fee_max": 0, "fee_vat_applicable": 1, "charge_bearer": "OUR"}).insert(ignore_permissions=True)
	publish_rate("SAR", "INR", "23.450000")
	safeguarded = accounts.resolve(COMPANY, "SAR", "Safeguarded Bank").name
	set_values({"enabled": 1, "default_entity": COMPANY, "functional_currency": "SAR", "identity_mode": "Frappe Session",
		"require_authorization_id": 1, "per_transfer_limit": 50000, "daily_limit": 10000000, "monthly_limit": 100000000,
		"edd_threshold": 15000, "edd_risk_tiers": "High", "vat_rate": 15, "funding_grace_minutes": 60,
		"unfunded_expiry_hours": 24, "late_funding_policy": "Require Reconfirmation", "auto_dispatch": 1,
		"dispatch_paused": 0, "notify_email": 0, "notify_sms": 0, "collection_account": safeguarded,
		"screening_max_list_age_hours": 24, "max_dispatch_attempts": 8, "status_poll_schedule": "30,120,300",
		"refund_fee_on_cancel": 1, "erp_posting_enabled": 0, "zatca_mode": "Off", "safeguarding_tolerance": 0})
	from neotec_remittance.services.screening import on_watchlist_change

	on_watchlist_change()
	psa = accounts.resolve(COMPANY, "INR", "Partner Settlement Asset", PARTNER)
	if not ledger.journal_for_event("test:prefund:v1"):
		ledger.post_journal("test:prefund:v1", "treasury_prefunding", COMPANY, "INR",
			[Dr("Partner Settlement Asset", PREFUND, PARTNER), Cr("Treasury Funding", PREFUND)])
	frappe.db.commit()
	return psa


def publish_rate(base, quote, rate):
	frappe.get_doc({"doctype": "Remittance Rate", "base_currency": base, "quote_currency": quote, "mid_rate": rate,
		"valid_from": utc_now() - timedelta(seconds=5), "stale_after_seconds": 86400, "source": "test"}).insert(
		ignore_permissions=True)


def make_customer(label=None, risk="Low", kyc="Verified"):
	label = label or frappe.generate_hash(length=8)
	email = f"nr-{label}@example.com"
	if not frappe.db.exists("User", email):
		frappe.get_doc({"doctype": "User", "email": email, "first_name": f"Cust {label}", "user_type": "Website User",
			"send_welcome_email": 0}).insert(ignore_permissions=True)
	c = frappe.get_doc({"doctype": "Remittance Customer", "customer_name": f"Test Customer {label}", "entity": COMPANY,
		"status": "Active", "language": "en", "user": email, "id_type": "National ID",
		"id_number_input": "1" + frappe.generate_hash(length=9).translate(str.maketrans("abcdef", "123456")),
		"kyc_status": kyc, "kyc_expiry": frappe.utils.add_days(frappe.utils.today(), 365), "kyc_provider": "test",
		"risk_tier": risk})
	c.flags.ignore_kyc_roles = True
	c.insert(ignore_permissions=True)
	return frappe.db.get_value("Remittance Customer", c.name, ["name", "entity", "status", "kyc_status", "kyc_expiry",
		"customer_name"], as_dict=True)


def make_beneficiary(customer, mode="SANDBOX-PAID", name="Ravi Kumar Sharma"):
	from neotec_remittance.services import beneficiaries

	return beneficiaries.create_or_version(customer, {"full_name": name, "country": "India", "currency": "INR",
		"account_number": "123456789012", "bank_code": mode})


def new_transfer(customer=None, amount="1000.00", mode="SANDBOX-PAID", ben_name="Ravi Kumar Sharma", key=None):
	from neotec_remittance.services import quotes

	customer = customer or make_customer()
	ben = make_beneficiary(customer, mode, ben_name)
	q = quotes.create_quote(customer, amount, "India", "INR")
	resp, _ = transfers.accept(customer, q["quote_id"], ben["beneficiary_version_id"], "auth-test-0001",
		key or frappe.generate_hash(length=20))
	return customer, resp


def fund(transfer_id, amount=None, entry_id=None):
	t = frappe.db.get_value("Remittance Transfer", transfer_id, ["total_payable", "funding_reference"], as_dict=True)
	safeguarded = accounts.resolve(COMPANY, "SAR", "Safeguarded Bank").name
	return funding.ingest_receipt(safeguarded, entry_id or frappe.generate_hash(length=16), amount or t.total_payable,
		"SAR", frappe.utils.today(), f"Transfer {t.funding_reference}", "Payer")


def drain(rounds=6):
	"""Run outbox + inbox until quiet, forcing delayed polls due."""
	for _ in range(rounds):
		frappe.db.sql("UPDATE `tabRemittance Outbox` SET next_attempt_at=%s WHERE status='Pending'", utc_now())
		frappe.db.commit()
		outbox.process_due()
		frappe.db.commit()


def T(name):
	return frappe.db.get_value("Remittance Transfer", name, "*", as_dict=True)
