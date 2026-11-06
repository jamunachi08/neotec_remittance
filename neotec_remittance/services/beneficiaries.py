"""Recipients are versioned. A version is immutable; any change creates a new version, and transfers
reference the exact version the customer authorised."""
import re

import frappe

from neotec_remittance.utils import crypto, db
from neotec_remittance.utils.errors import AccessDenied, NotFound, ValidationFailed
from neotec_remittance.utils.names import mask_tail, normalize_name

IBAN_RE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{10,30}$")


def _iban_ok(iban: str) -> bool:
	if not IBAN_RE.match(iban):
		return False
	moved = iban[4:] + iban[:4]
	digits = "".join(str(int(ch, 36)) for ch in moved)
	return int(digits) % 97 == 1


def validate_account(country: str, account: str, bank_code: str | None):
	acct = re.sub(r"\s", "", account or "").upper()
	if not acct or len(acct) > 40:
		raise ValidationFailed("Account number is required", code="BENEFICIARY_ACCOUNT_INVALID")
	if acct[:2].isalpha():
		if not _iban_ok(acct):
			raise ValidationFailed("IBAN check digits are invalid", code="BENEFICIARY_ACCOUNT_INVALID")
	elif not bank_code:
		raise ValidationFailed("Bank code is required for non-IBAN accounts", code="BENEFICIARY_BANK_CODE_REQUIRED")
	return acct


def create_or_version(customer, data: dict) -> dict:
	full_name = (data.get("full_name") or "").strip()
	if len(full_name) < 3:
		raise ValidationFailed("Beneficiary full legal name is required", code="BENEFICIARY_NAME_REQUIRED")
	country = data.get("country")
	currency = data.get("currency")
	if not (country and frappe.db.exists("Country", country) and currency and frappe.db.exists("Currency", currency)):
		raise ValidationFailed("Valid country and currency are required", code="BENEFICIARY_ROUTE_INVALID")
	account = validate_account(country, data.get("account_number"), data.get("bank_code"))

	beneficiary = data.get("beneficiary_id")
	if beneficiary:
		b = db.lock_row("Remittance Beneficiary", beneficiary)
		if not b:
			raise NotFound("Beneficiary not found")
		if b.customer != customer.name:
			raise AccessDenied("Beneficiary does not belong to this customer")
		version_no = (frappe.db.sql("""SELECT COALESCE(MAX(version_no),0) FROM `tabRemittance Beneficiary Version`
			WHERE beneficiary=%s""", beneficiary)[0][0] or 0) + 1
	else:
		beneficiary = db.insert_row("Remittance Beneficiary", {"customer": customer.name,
			"nickname": (data.get("nickname") or full_name)[:140], "country": country, "currency": currency,
			"status": "Active"})
		version_no = 1
	version = db.insert_row("Remittance Beneficiary Version", {
		"beneficiary": beneficiary, "customer": customer.name, "version_no": version_no,
		"full_name": full_name, "full_name_ar": data.get("full_name_ar"), "name_normalized": normalize_name(full_name),
		"country": country, "currency": currency, "payout_method": "Bank Account",
		"bank_code": (data.get("bank_code") or "").strip().upper() or None,
		"account_masked": mask_tail(account), "account_index": crypto.blind_index(account, "beneficiary_account"),
		"account_enc": crypto.encrypt(account), "relationship": data.get("relationship"),
		"address": data.get("address"),
	})
	db.update_row("Remittance Beneficiary", beneficiary, {"current_version": version, "country": country,
		"currency": currency})
	return {"beneficiary_id": beneficiary, "beneficiary_version_id": version, "version": version_no,
		"account_masked": mask_tail(account), "full_name": full_name}


def get_version_for_customer(version_name: str, customer_name: str):
	v = db.get_row("Remittance Beneficiary Version", version_name)
	if not v or v.customer != customer_name:
		raise NotFound("Beneficiary version not found", code="BENEFICIARY_NOT_FOUND")
	status = frappe.db.get_value("Remittance Beneficiary", v.beneficiary, "status")
	if status != "Active":
		raise ValidationFailed("Beneficiary is disabled", code="BENEFICIARY_DISABLED")
	return v


def list_for_customer(customer_name: str) -> list[dict]:
	rows = frappe.db.sql("""SELECT b.name AS beneficiary_id, b.nickname, b.country, b.currency,
		v.name AS beneficiary_version_id, v.full_name, v.account_masked, v.bank_code
		FROM `tabRemittance Beneficiary` b JOIN `tabRemittance Beneficiary Version` v ON v.name=b.current_version
		WHERE b.customer=%s AND b.status='Active' ORDER BY b.modified DESC""", customer_name, as_dict=True)
	return rows
