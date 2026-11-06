"""Funding receipts and allocation.

Every bank credit is recorded once (unique bank account + external entry id) and first lands in
Unallocated Receipts. Allocation to a transfer is a separate, uniquely keyed journal, so manual and
automatic allocation share one path. Matching is deterministic on the funding reference (or a
virtual IBAN mapped to it); there is no fuzzy auto-matching of customer money."""
import re
from datetime import timedelta

import frappe

from neotec_remittance.services import ledger, notify, refunds, state
from neotec_remittance.services.audit import note
from neotec_remittance.services.ledger import Cr, Dr
from neotec_remittance.services.settings import get_settings
from neotec_remittance.utils import db
from neotec_remittance.utils.errors import GuardFailed, ValidationFailed
from neotec_remittance.utils.hashing import digest
from neotec_remittance.utils.money import D, positive_amount
from neotec_remittance.utils.timeutil import to_dt, utc_now

REF_RE = re.compile(r"\bNR[A-HJ-NP-Z2-9]{10}\b")


def ingest_receipt(bank_account: str, external_entry_id: str, amount, currency: str, value_date=None,
		reference_text: str = "", payer_name: str = "", payer_account_masked: str = "", source: str = "Manual",
		received_at=None) -> dict:
	acc = db.get_row("Remittance Account", bank_account, "name, entity, currency, account_role")
	if not acc or acc.account_role != "Safeguarded Bank":
		raise ValidationFailed("Receipts must be recorded against a Safeguarded Bank account")
	if acc.currency != currency:
		raise ValidationFailed("Receipt currency does not match the bank account")
	if not external_entry_id:
		raise ValidationFailed("External bank entry id is required")
	amount = positive_amount(amount, currency)
	dedup_key = f"{bank_account}|{external_entry_id}"
	existing = db.find_row("Remittance Funding Receipt", {"dedup_key": dedup_key}, "name, transfer, status")
	if existing:
		return {"receipt": existing.name, "duplicate": True, "transfer": existing.transfer, "status": existing.status}
	evidence = {"bank_account": bank_account, "external_entry_id": external_entry_id, "amount": amount,
		"currency": currency, "value_date": str(value_date or ""), "reference_text": reference_text or ""}
	db.savepoint("nr_receipt")
	try:
		name = db.insert_row("Remittance Funding Receipt", {"bank_account": bank_account,
			"external_entry_id": external_entry_id, "dedup_key": dedup_key, "currency": currency, "amount": amount,
			"allocated_amount": 0, "status": "Unallocated", "value_date": value_date, "received_at": received_at or utc_now(),
			"reference_text": (reference_text or "")[:1000], "payer_name": payer_name, "payer_account_masked": payer_account_masked,
			"source": source, "evidence_hash": digest(evidence)})
	except Exception as e:
		if not db.is_duplicate(e):
			raise
		db.rollback_to("nr_receipt")
		existing = db.find_row("Remittance Funding Receipt", {"dedup_key": dedup_key}, "name, transfer, status")
		return {"receipt": existing.name, "duplicate": True, "transfer": existing.transfer, "status": existing.status}
	ledger.post_journal(f"receipt:{name}", "funds_received", acc.entity, currency,
		[{"account": bank_account, "debit": amount, "credit": 0}, Cr("Unallocated Receipts", amount)],
		narration=f"Bank entry {external_entry_id}")
	transfer = match_transfer(reference_text)
	result = {"receipt": name, "duplicate": False, "transfer": None, "status": "Unallocated"}
	if transfer:
		result.update(allocate(name, transfer, by="auto-match"))
	return result


def match_transfer(reference_text: str) -> str | None:
	for ref in REF_RE.findall((reference_text or "").upper()):
		t = db.find_row("Remittance Transfer", {"funding_reference": ref}, "name")
		if t:
			return t.name
	return None


def allocate(receipt_name: str, transfer_name: str, by: str) -> dict:
	t = state.lock_transfer(transfer_name)  # lock order: transfer, then receipt
	r = db.lock_row("Remittance Funding Receipt", receipt_name)
	if r.currency != t.send_currency:
		return {"transfer": None, "status": r.status, "reason": "CURRENCY_MISMATCH"}
	if t.funding_state not in ("Awaiting Funds", "Partially Funded") or t.payout_state == "Cancelled":
		return {"transfer": None, "status": r.status, "reason": "TRANSFER_NOT_ACCEPTING_FUNDS"}
	available = D(r.amount) - D(r.allocated_amount)
	need = D(t.total_payable) - D(t.funded_amount)
	alloc = min(available, need)
	if alloc <= 0:
		return {"transfer": None, "status": r.status, "reason": "NOTHING_TO_ALLOCATE"}
	ledger.post_journal(f"alloc:{r.name}:{t.name}", "funds_allocated", t.entity, t.send_currency,
		[Dr("Unallocated Receipts", alloc), Cr("Customer Liability", alloc)], transfer=t.name)
	new_alloc = D(r.allocated_amount) + alloc
	db.update_row("Remittance Funding Receipt", r.name, {"allocated_amount": new_alloc, "transfer": t.name,
		"status": "Allocated" if new_alloc == D(r.amount) else "Partially Allocated"})
	funded = D(t.funded_amount) + alloc
	changes = {"funded_amount": funded}
	late = to_dt(r.received_at) > to_dt(t.funding_deadline)
	if funded == D(t.total_payable):
		fee, vat = D(t.fee), D(t.vat)
		if fee + vat > 0:
			ledger.post_journal(f"funded:{t.name}", "fee_and_vat_on_funding", t.entity, t.send_currency,
				[Dr("Customer Liability", fee + vat), Cr("Deferred Fee", fee), Cr("VAT Payable", vat)], transfer=t.name)
		changes["funded_at"] = utc_now()
		policy = get_settings().late_funding_policy
		if late and policy == "Require Reconfirmation":
			changes["requote_required"] = 1
		state.save(t, changes, {"funding_state": "Funded"})
		notify.queue(t, "action_required" if t.requote_required else "transfer_funded")
		if late and policy == "Refund":
			refunds.request_cancel(t.name, "late funding policy")
		elif not t.requote_required:
			from neotec_remittance.services import execution, screening

			screening.screen(t)
			if t.compliance_state == "Approved":
				execution.try_release(t.name)
	else:
		state.save(t, changes, {"funding_state": "Partially Funded"})
	if by != "auto-match":
		note("Remittance Funding Receipt", r.name, f"Allocated {alloc} to {t.tracking_ref} by {by}")
	return {"transfer": t.name, "status": "Allocated", "allocated": str(alloc), "funding_state": t.funding_state}


def expire_unfunded(limit: int = 500):
	"""Scheduler: expire transfers not fully funded within the funding window; refund partial funds."""
	hours = get_settings().unfunded_expiry_hours or 24
	cutoff = utc_now() - timedelta(hours=hours)
	rows = frappe.db.sql("""SELECT name FROM `tabRemittance Transfer` WHERE funding_state IN ('Awaiting Funds','Partially Funded')
		AND payout_state='Not Ready' AND funding_deadline < %s LIMIT %s""", (cutoff, limit), as_dict=True)
	for r in rows:
		try:
			t = state.lock_transfer(r.name)
			if t.funding_state not in ("Awaiting Funds", "Partially Funded"):
				continue
			state.save(t, {"closed_at": utc_now()}, {"funding_state": "Expired", "payout_state": "Cancelled"})
			if D(t.funded_amount) > 0 and not t.legal_hold:
				refunds.initiate(t, "funding window expired")
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Neotec Remittance: expiry failed for {r.name}")
