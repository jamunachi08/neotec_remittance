"""Release and dispatch. All release guards are re-evaluated from stored state under the transfer lock;
UI approvals are never authorisation. Dispatch writes the payout attempt, FX conversion journals and the
outbox command in the same transaction as the state change, then commits once."""
from datetime import timedelta

import frappe

from neotec_remittance.services import accounts, ledger, notify, outbox, state
from neotec_remittance.services.ledger import Cr, Dr
from neotec_remittance.services.settings import get_settings
from neotec_remittance.utils import crypto, db
from neotec_remittance.utils.errors import GuardFailed
from neotec_remittance.utils.hashing import digest
from neotec_remittance.utils.money import D, amount_str
from neotec_remittance.utils.timeutil import to_dt, utc_now


def guard_failures(t, stage: str) -> list[str]:
	s = get_settings()
	f = []
	if not s.enabled:
		f.append("PLATFORM_DISABLED")
	if s.dispatch_paused:
		f.append("DISPATCH_PAUSED")
	c = db.get_row("Remittance Customer", t.customer, "status, kyc_status, kyc_expiry")
	if not c or c.status != "Active":
		f.append("CUSTOMER_NOT_ACTIVE")
	elif c.kyc_status != "Verified" or (c.kyc_expiry and to_dt(c.kyc_expiry).date() < utc_now().date()):
		f.append("KYC_NOT_VALID")
	if t.funding_state != "Funded":
		f.append("NOT_FUNDED")
	if t.compliance_state != "Approved":
		f.append("COMPLIANCE_NOT_APPROVED")
	if not screening_fresh(t, s):
		f.append("SCREENING_STALE")
	if t.cancel_requested:
		f.append("CANCEL_REQUESTED")
	if t.requote_required:
		f.append("REQUOTE_REQUIRED")
	if t.legal_hold:
		f.append("LEGAL_HOLD")
	if not frappe.db.get_value("Remittance Partner", t.partner, "enabled"):
		f.append("PARTNER_DISABLED")
	if not frappe.db.get_value("Remittance Corridor", t.corridor, "enabled"):
		f.append("CORRIDOR_DISABLED")
	return f


def screening_fresh(t, s=None) -> bool:
	s = s or get_settings()
	if not s.screening_list_updated_at or not t.screened_at:
		return False
	if (t.screening_list_version or "") != (s.screening_list_version or ""):
		return False
	max_age = timedelta(hours=s.screening_max_list_age_hours or 24)
	return to_dt(s.screening_list_updated_at) + max_age >= utc_now()


def try_release(transfer_name: str) -> str:
	"""Funded + Approved -> Ready (liquidity reserved) -> Dispatching (if auto dispatch)."""
	t = state.lock_transfer(transfer_name)
	if t.payout_state != "Not Ready":
		return t.payout_state
	failures = guard_failures(t, "release")
	if failures:
		state.save(t, {"last_error_code": failures[0]})
		return t.payout_state
	acc = accounts.resolve(t.entity, t.receive_currency, "Partner Settlement Asset", t.partner, for_update=True)
	amount = D(t.payout_amount)
	if D(acc.balance) - D(acc.reserved) < amount:
		state.save(t, {"last_error_code": "INSUFFICIENT_LIQUIDITY"})
		return t.payout_state
	key = f"{t.name}|liquidity"
	if not db.find_row("Remittance Reservation", {"reservation_key": key}, "name"):
		db.insert_row("Remittance Reservation", {"reservation_key": key, "transfer": t.name, "account": acc.name,
			"currency": t.receive_currency, "amount": amount, "state": "Held"})
		frappe.db.sql("""UPDATE `tabRemittance Account` SET reserved = reserved + %s, row_version=row_version+1
			WHERE name=%s""", (amount, acc.name))
	state.save(t, {"released_at": utc_now(), "last_error_code": None}, {"payout_state": "Ready"})
	if get_settings().auto_dispatch:
		dispatch(t)
	return t.payout_state


def release_reservation(t, consume: bool = False):
	res = db.find_row("Remittance Reservation", {"reservation_key": f"{t.name}|liquidity"}, "name, account, amount, state",
		for_update=True)
	if not res or res.state != "Held":
		return
	frappe.db.sql("""UPDATE `tabRemittance Account` SET reserved = reserved - %s, row_version=row_version+1
		WHERE name=%s""", (D(res.amount), res.account))
	db.update_row("Remittance Reservation", res.name, {"state": "Consumed" if consume else "Released"},
		where={"state": "Held"})


def dispatch(t):
	"""Ready -> Dispatching. Caller holds the transfer lock (t from state.lock_transfer)."""
	if t.payout_state != "Ready":
		raise GuardFailed(f"Transfer is {t.payout_state}, not Ready")
	failures = guard_failures(t, "dispatch")
	if failures:
		raise GuardFailed("Dispatch guards failed: " + ", ".join(failures), details={"failures": failures})
	attempt_no = (frappe.db.sql("SELECT COUNT(*) FROM `tabRemittance Payout Attempt` WHERE transfer=%s", t.name)[0][0]) + 1
	instruction_id = db.new_name()
	payload = {"instruction_id": instruction_id, "transfer": t.name, "tracking_ref": t.tracking_ref,
		"amount": amount_str(t.payout_amount, t.receive_currency), "currency": t.receive_currency,
		"beneficiary_version": t.beneficiary_version}
	try:
		db.savepoint("nr_attempt")
		attempt = db.insert_row("Remittance Payout Attempt", {"transfer": t.name, "partner": t.partner,
			"attempt_no": attempt_no, "instruction_id": instruction_id, "open_key": t.name, "status": "Dispatched",
			"request_hash": digest(payload)})
	except Exception as e:
		if db.is_duplicate(e):
			db.rollback_to("nr_attempt")
			raise GuardFailed("An unresolved payout attempt already exists", code="OPEN_ATTEMPT_EXISTS")
		raise
	post_conversion(t, attempt_no)
	outbox.enqueue("create_payment", t.name, t.partner, payload, instruction_id, payout_attempt=attempt)
	state.save(t, {"dispatch_right_consumed": 1, "payout_attempt": attempt, "dispatched_at": utc_now()},
		{"payout_state": "Dispatching"})
	return attempt


def post_conversion(t, attempt_no):
	"""Move the obligation from the customer liability (send ccy) to the payout liability (receive ccy)."""
	principal, payout = D(t.principal), D(t.payout_amount)
	s, r = t.send_currency, t.receive_currency
	if s == r:
		ledger.post_journal(f"convert:{t.name}:{attempt_no}", "payout_obligation", t.entity, s,
			[Dr("Customer Liability", principal), Cr("Payout Liability", principal)], transfer=t.name)
		return
	ledger.post_journal(f"convert:{t.name}:{attempt_no}:{s}", "fx_conversion", t.entity, s,
		[Dr("Customer Liability", principal), Cr("FX Bridge", principal)], transfer=t.name,
		narration=f"FX {s}->{r} at {t.customer_rate}")
	ledger.post_journal(f"convert:{t.name}:{attempt_no}:{r}", "fx_conversion", t.entity, r,
		[Dr("FX Bridge", payout), Cr("Payout Liability", payout)], transfer=t.name,
		narration=f"FX {s}->{r} at {t.customer_rate}")


def reverse_conversion(t, attempt_no, reason):
	for j in frappe.db.sql("""SELECT name, event_id FROM `tabRemittance Journal` WHERE event_id LIKE %s
			AND reverses IS NULL""", (f"convert:{t.name}:{attempt_no}%",), as_dict=True):
		ledger.reverse_journal(j.name, f"reverse:{j.event_id}", narration=reason)


def build_payment_request(payload: dict) -> dict:
	"""Decrypts beneficiary details at send time only; they are never stored in the outbox."""
	v = db.get_row("Remittance Beneficiary Version", payload["beneficiary_version"])
	return {"instruction_id": payload["instruction_id"], "end_to_end_id": payload["tracking_ref"],
		"amount": payload["amount"], "currency": payload["currency"],
		"beneficiary": {"name": v.full_name, "country": v.country, "bank_code": v.bank_code,
			"account": crypto.decrypt(v.account_enc)}}


def release_due(limit: int = 200):
	"""Scheduler: screen/release funded transfers that are waiting (liquidity, stale screening, etc.)."""
	from neotec_remittance.services import screening

	rows = frappe.db.sql("""SELECT name, compliance_state, screened_at FROM `tabRemittance Transfer`
		WHERE funding_state='Funded' AND payout_state IN ('Not Ready','Ready') AND compliance_state IN ('Pending','Approved')
		AND cancel_requested=0 AND requote_required=0 AND legal_hold=0 ORDER BY funded_at LIMIT %s""", limit, as_dict=True)
	for r in rows:
		try:
			t = state.lock_transfer(r.name)
			if t.compliance_state == "Pending" or not screening_fresh(t):
				screening.screen(t)
			if t.compliance_state == "Approved":
				if t.payout_state == "Ready":
					if not guard_failures(t, "dispatch"):
						dispatch(t)
				else:
					try_release(t.name)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Neotec Remittance: release failed for {r.name}")
