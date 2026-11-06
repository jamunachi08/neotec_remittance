"""Transfer acceptance (quote + beneficiary version + customer authorisation) and customer read models."""
import secrets
from datetime import timedelta

import frappe

from neotec_remittance.services import beneficiaries, idempotency, notify, state
from neotec_remittance.services.audit import note
from neotec_remittance.services.settings import get_settings, require_enabled
from neotec_remittance.utils import db
from neotec_remittance.utils.errors import AccessDenied, GuardFailed, NotFound, ValidationFailed
from neotec_remittance.utils.money import D, amount_str
from neotec_remittance.utils.timeutil import iso, to_dt, utc_now

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
REF_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # matches funding.REF_RE (no I, L, O, 0, 1)


def _code(alphabet, n):
	return "".join(secrets.choice(alphabet) for _ in range(n))


def check_customer_eligible(c):
	if c.status != "Active":
		raise AccessDenied("Customer account is not active", code="CUSTOMER_NOT_ACTIVE")
	if c.kyc_status != "Verified" or (c.kyc_expiry and to_dt(c.kyc_expiry).date() < utc_now().date()):
		raise GuardFailed("Identity verification is not valid", code="KYC_NOT_VALID")


def check_limits(c, principal, corridor_max):
	s = get_settings()
	if D(s.per_transfer_limit) and principal > D(s.per_transfer_limit):
		raise GuardFailed("Amount exceeds the per-transfer limit", code="LIMIT_PER_TRANSFER")
	now = utc_now()
	def used(days):
		return D(frappe.db.sql("""SELECT COALESCE(SUM(principal),0) FROM `tabRemittance Transfer`
			WHERE customer=%s AND accepted_at>=%s AND funding_state!='Expired'
			AND NOT (payout_state='Cancelled' AND funded_amount=0) AND compliance_state!='Rejected'""",
			(c.name, now - timedelta(days=days)))[0][0])
	if D(s.daily_limit) and used(1) + principal > D(s.daily_limit):
		raise GuardFailed("Amount exceeds the 24-hour limit", code="LIMIT_DAILY")
	if D(s.monthly_limit) and used(30) + principal > D(s.monthly_limit):
		raise GuardFailed("Amount exceeds the 30-day limit", code="LIMIT_MONTHLY")


def verify_authorization(customer, authorization_id, terms: dict):
	s = get_settings()
	if s.require_authorization_id and not authorization_id:
		raise ValidationFailed("Step-up authorization is required", code="AUTHORIZATION_REQUIRED")
	for path in frappe.get_hooks("neotec_remittance_verify_authorization") or []:
		if not frappe.get_attr(path)(customer, authorization_id, terms):
			raise AccessDenied("Authorization does not match these terms", code="AUTHORIZATION_INVALID")


def accept(customer, quote_id: str, beneficiary_version_id: str, authorization_id: str | None, idem_key: str):
	require_enabled()
	body = {"quote_id": quote_id, "beneficiary_version_id": beneficiary_version_id, "authorization_id": authorization_id}
	idem = idempotency.begin(customer.entity, customer.name, "transfer.create", idem_key, body)
	if idem.replay:
		return idem.replay, 200
	c = db.lock_row("Remittance Customer", customer.name)  # lock order: customer -> quote -> transfer
	check_customer_eligible(c)
	qt = db.lock_row("Remittance Quote", quote_id)
	if not qt or qt.customer != c.name:
		raise NotFound("Quote not found", code="QUOTE_NOT_FOUND")
	if qt.status != "Open":
		raise GuardFailed("Quote was already used", code="QUOTE_NOT_OPEN")
	if to_dt(qt.expires_at) < utc_now():
		db.update_row("Remittance Quote", qt.name, {"status": "Expired"})
		raise GuardFailed("Quote has expired; request a new quote", code="QUOTE_EXPIRED")
	bv = beneficiaries.get_version_for_customer(beneficiary_version_id, c.name)
	corridor = frappe.get_cached_doc("Remittance Corridor", qt.corridor)
	if bv.currency != qt.receive_currency or bv.country != corridor.receive_country:
		raise ValidationFailed("Beneficiary does not match the quoted route", code="BENEFICIARY_ROUTE_MISMATCH")
	verify_authorization(c, authorization_id, {"quote": qt.name, "beneficiary_version": bv.name,
		"total": amount_str(qt.total_payable, qt.send_currency), "payout": amount_str(qt.payout_amount, qt.receive_currency)})
	check_limits(c, D(qt.principal), D(corridor.max_amount))

	now = utc_now()
	grace = timedelta(minutes=get_settings().funding_grace_minutes or 0)
	values = {
		"tracking_ref": "NRT-" + _code(ALPHABET, 10), "customer": c.name, "entity": qt.entity, "quote": qt.name,
		"beneficiary_version": bv.name, "corridor": qt.corridor, "partner": qt.partner,
		"send_currency": qt.send_currency, "principal": qt.principal, "fee": qt.fee, "vat": qt.vat,
		"total_payable": qt.total_payable, "receive_currency": qt.receive_currency, "customer_rate": qt.customer_rate,
		"payout_amount": qt.payout_amount, "authorization_id": authorization_id,
		"funding_state": "Awaiting Funds", "compliance_state": "Pending", "payout_state": "Not Ready",
		"refund_state": "None", "recon_state": "Unreconciled", "customer_status": "Awaiting Payment", "row_version": 0,
		"cancel_requested": 0, "dispatch_right_consumed": 0, "requote_required": 0, "legal_hold": 0,
		"funding_reference": "NR" + _code(REF_ALPHABET, 10), "funded_amount": 0,
		"funding_deadline": to_dt(qt.expires_at) + grace, "accepted_at": now, "refund_amount": 0,
	}
	name = db.insert_row("Remittance Transfer", values)
	db.update_row("Remittance Quote", qt.name, {"status": "Accepted", "transfer": name}, where={"status": "Open"})
	t = db.get_row("Remittance Transfer", name)
	notify.queue(t, "transfer_accepted")
	response = transfer_response(t)
	idempotency.complete(idem, response, resource=name, http_status=201)
	return response, 201


def reconfirm(customer, transfer_name: str, quote_id: str, authorization_id: str | None):
	"""Late funding: customer accepts fresh FX terms for the same amounts before anything is dispatched."""
	t = state.lock_transfer(transfer_name)
	if t.customer != customer.name:
		raise NotFound("Transfer not found")
	if not t.requote_required or t.payout_state != "Not Ready":
		raise GuardFailed("Transfer does not need reconfirmation", code="RECONFIRM_NOT_REQUIRED")
	qt = db.lock_row("Remittance Quote", quote_id)
	if not qt or qt.customer != customer.name or qt.status != "Open" or to_dt(qt.expires_at) < utc_now():
		raise GuardFailed("Quote is not valid", code="QUOTE_NOT_OPEN")
	if (qt.corridor != t.corridor or D(qt.principal) != D(t.principal) or D(qt.fee) != D(t.fee)
			or D(qt.vat) != D(t.vat)):
		raise GuardFailed("New terms change the amount paid; cancel for a refund instead", code="REQUOTE_TOTAL_CHANGED")
	verify_authorization(customer, authorization_id, {"quote": qt.name, "transfer": t.name})
	old = f"{t.quote} rate {t.customer_rate} payout {t.payout_amount}"
	state.save(t, {"quote": qt.name, "customer_rate": qt.customer_rate, "payout_amount": qt.payout_amount,
		"requote_required": 0, "authorization_id": authorization_id})
	db.update_row("Remittance Quote", qt.name, {"status": "Accepted", "transfer": t.name}, where={"status": "Open"})
	note("Remittance Transfer", t.name, f"Customer reconfirmed terms. Previous: {old}. New: {qt.name}.")
	frappe.db.after_commit.add(lambda: frappe.enqueue("neotec_remittance.services.execution.release_due", queue="long"))
	return transfer_response(t)


def get_for_customer(customer, transfer_name: str):
	t = db.get_row("Remittance Transfer", transfer_name)
	if not t or t.customer != customer.name:
		raise NotFound("Transfer not found", code="TRANSFER_NOT_FOUND")
	return t


def transfer_response(t) -> dict:
	s = get_settings()
	status = t.customer_status or state.derive_customer_status(t)
	out = {"transfer_id": t.name, "tracking_ref": t.tracking_ref, "status": status,
		"status_ar": state.CUSTOMER_STATUS_AR.get(status), "status_mapping": state.CUSTOMER_STATUS_MAPPING_VERSION,
		"send": {"currency": t.send_currency, "principal": amount_str(t.principal, t.send_currency),
			"fee": amount_str(t.fee, t.send_currency), "vat": amount_str(t.vat, t.send_currency),
			"total": amount_str(t.total_payable, t.send_currency), "paid": amount_str(t.funded_amount, t.send_currency)},
		"receive": {"currency": t.receive_currency, "amount": amount_str(t.payout_amount, t.receive_currency)},
		"rate": t.customer_rate, "accepted_at": iso(t.accepted_at), "paid_at": iso(t.paid_at),
		"action_required": "RECONFIRM_TERMS" if t.requote_required else None}
	if t.funding_state in ("Awaiting Funds", "Partially Funded") and t.payout_state == "Not Ready":
		out["funding_instructions"] = {"reference": t.funding_reference, "bank": s.collection_bank_name,
			"iban": s.collection_iban, "pay_by": iso(t.funding_deadline),
			"amount_due": amount_str(D(t.total_payable) - D(t.funded_amount), t.send_currency),
			"note_en": s.funding_instructions_en, "note_ar": s.funding_instructions_ar}
	return out


def list_for_customer(customer, limit=20, cursor=None) -> dict:
	limit = max(1, min(int(limit or 20), 100))
	params = [customer.name]
	cond = ""
	if cursor:
		cond = " AND accepted_at < %s"
		params.append(to_dt(cursor.replace("T", " ").replace("Z", "")))
	rows = frappe.db.sql(f"""SELECT * FROM `tabRemittance Transfer` WHERE customer=%s{cond}
		ORDER BY accepted_at DESC LIMIT {limit + 1}""", tuple(params), as_dict=True)
	items = [transfer_response(r) for r in rows[:limit]]
	next_cursor = iso(rows[limit - 1].accepted_at) if len(rows) > limit else None
	return {"items": items, "next_cursor": next_cursor}
