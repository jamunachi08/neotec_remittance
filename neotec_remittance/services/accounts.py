"""Subledger chart of accounts.

Balance convention: balance = sum(debit) - sum(credit). Liability accounts therefore carry negative balances.
Synchronous accounts keep a locked running balance (used for reservations, e.g. partner liquidity);
Deferred accounts are refreshed from ledger lines by a background job to avoid hot-row contention."""
import frappe

from neotec_remittance.utils import db
from neotec_remittance.utils.errors import ConfigurationError

DEBIT_NORMAL = {"Safeguarded Bank", "Operating Bank", "Partner Settlement Asset", "Partner Charges Expense",
	"Rounding", "FX Bridge"}
CUSTOMER_MONEY = {"Customer Liability", "Unallocated Receipts", "Refund Payable"}
SYNCHRONOUS_DEFAULT = {"Partner Settlement Asset"}
SIGN_ENFORCED_DEFAULT = {"Partner Settlement Asset"}
ALL_ROLES = ["Safeguarded Bank", "Operating Bank", "Customer Liability", "Payout Liability", "Deferred Fee",
	"Fee Revenue", "VAT Payable", "Unallocated Receipts", "Refund Payable", "Partner Settlement Asset",
	"Partner Charges Expense", "FX Bridge", "Rounding", "Return Suspense", "Treasury Funding"]
PARTNER_SCOPED = {"Partner Settlement Asset", "Partner Charges Expense"}


def resolution_key(entity, currency, role, partner=None) -> str:
	return f"{entity}|{currency}|{role}|{partner or '-'}"


def resolve(entity, currency, role, partner=None, for_update=False):
	if role not in PARTNER_SCOPED:
		partner = None
	row = db.find_row("Remittance Account", {"resolution_key": resolution_key(entity, currency, role, partner)},
		fields="name, account_role, currency, entity, balance_mode, enforce_sign, normal_side, balance, reserved, row_version, disabled",
		for_update=for_update)
	if not row:
		raise ConfigurationError(f"No Remittance Account for {role} / {currency} / {entity}" + (f" / {partner}" if partner else ""),
			code="ACCOUNT_NOT_CONFIGURED")
	if row.disabled:
		raise ConfigurationError(f"Remittance Account {row.name} is disabled", code="ACCOUNT_DISABLED")
	return row


def ensure_account(entity, currency, role, partner=None, code=None):
	if role not in PARTNER_SCOPED:
		partner = None
	key = resolution_key(entity, currency, role, partner)
	existing = frappe.db.get_value("Remittance Account", {"resolution_key": key})
	if existing:
		return existing
	abbr = frappe.db.get_value("Company", entity, "abbr") or entity[:4]
	code = code or "-".join(filter(None, ["RA", abbr, currency, role.replace(" ", ""), (partner or "").replace(" ", "")]))
	doc = frappe.get_doc({"doctype": "Remittance Account", "account_code": code[:140], "entity": entity,
		"currency": currency, "account_role": role, "partner": partner,
		"balance_mode": "Synchronous" if role in SYNCHRONOUS_DEFAULT else "Deferred",
		"enforce_sign": 1 if role in SIGN_ENFORCED_DEFAULT else 0})
	doc.insert(ignore_permissions=True)
	return doc.name


def ensure_chart(entity, send_currency, receive_currency, partner):
	"""Idempotently creates the accounts a corridor needs."""
	for role in ["Safeguarded Bank", "Operating Bank", "Customer Liability", "Deferred Fee", "Fee Revenue",
			"VAT Payable", "Unallocated Receipts", "Refund Payable", "FX Bridge", "Rounding", "Treasury Funding"]:
		ensure_account(entity, send_currency, role)
	for role in ["Payout Liability", "FX Bridge", "Return Suspense", "Treasury Funding"]:
		ensure_account(entity, receive_currency, role)
	ensure_account(entity, receive_currency, "Partner Settlement Asset", partner)
	ensure_account(entity, receive_currency, "Partner Charges Expense", partner)
