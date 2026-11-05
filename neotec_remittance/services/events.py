"""Applies canonical bank evidence to transfers.

- terminal outcomes are never overwritten by late non-terminal messages (stale -> Ignored)
- contradictory terminal evidence (paid vs rejected) is Quarantined for investigation, never auto-resolved
- unknown references, partner mismatches and amount/currency mismatches are Quarantined
- every financial effect is a uniquely keyed journal, so redelivery cannot double-post"""
import frappe

from neotec_remittance.services import execution, ledger, notify, outbox, refunds, state
from neotec_remittance.services.ledger import Cr, Dr
from neotec_remittance.utils import db
from neotec_remittance.utils.money import D
from neotec_remittance.utils.timeutil import utc_now

PROCESSED, IGNORED, QUARANTINED = "Processed", "Ignored", "Quarantined"


def apply(ev):
	attempt = None
	if ev.instruction_id:
		attempt = db.find_row("Remittance Payout Attempt", {"instruction_id": ev.instruction_id}, "name, transfer, partner")
	if not attempt and ev.bank_reference:
		attempt = db.find_row("Remittance Payout Attempt", {"bank_reference": ev.bank_reference, "partner": ev.partner},
			"name, transfer, partner")
	if not attempt:
		return QUARANTINED, "UNKNOWN_REFERENCE", None
	if attempt.partner != ev.partner:
		return QUARANTINED, "PARTNER_MISMATCH", attempt.transfer

	t = state.lock_transfer(attempt.transfer)
	a = db.lock_row("Remittance Payout Attempt", attempt.name)
	if ev.currency and ev.currency != t.receive_currency:
		return QUARANTINED, "CURRENCY_MISMATCH", t.name
	if t.payout_attempt and t.payout_attempt != a.name:
		return QUARANTINED, "EVIDENCE_FOR_SUPERSEDED_ATTEMPT", t.name

	et = ev.event_type
	ps = t.payout_state
	if et == "accepted":
		if ps not in ("Dispatching", "Unknown"):
			return IGNORED, f"STALE_ACCEPTED_IN_{ps}", t.name
		ref = ev.bank_reference or a.bank_reference
		state.save(t, {"bank_reference": ref}, {"payout_state": "Accepted"})
		db.update_row("Remittance Payout Attempt", a.name, {"status": "Accepted", "bank_reference": ref,
			"last_status_at": utc_now()})
		if t.cancel_requested:
			refunds.request_recall(t, "customer cancellation")
		return PROCESSED, "ACCEPTED", t.name

	if et == "submission_uncertain":
		if ps != "Dispatching":
			return IGNORED, f"STALE_UNCERTAIN_IN_{ps}", t.name
		state.save(t, {"last_error_code": "SUBMISSION_UNCERTAIN"}, {"payout_state": "Unknown"})
		db.update_row("Remittance Payout Attempt", a.name, {"status": "Unknown", "last_status_at": utc_now()})
		return PROCESSED, "UNKNOWN", t.name

	if et == "paid":
		if ps == "Paid":
			return IGNORED, "DUPLICATE_PAID", t.name
		if ps in ("Rejected", "Recalled", "Returned", "Cancelled"):
			_contradiction(t, ev, ps)
			return QUARANTINED, f"CONTRADICTORY_PAID_AFTER_{ps}", t.name
		if ps not in state.UNCERTAIN_PAYOUT:
			return QUARANTINED, f"PAID_IN_{ps}", t.name
		charges = D(ev.charges_amount)
		corridor_bearer = frappe.db.get_value("Remittance Corridor", t.corridor, "charge_bearer") or "OUR"
		if ev.amount:
			delivered = D(ev.amount)
			expected = D(t.payout_amount) - (charges if corridor_bearer in ("SHA", "BEN") else 0)
			if delivered != expected:
				return QUARANTINED, f"AMOUNT_MISMATCH delivered={delivered} expected={expected}", t.name
		_settle_paid(t, a, ev, charges if corridor_bearer == "OUR" else D(0))
		return PROCESSED, "PAID", t.name

	if et in ("rejected", "not_found_final", "recalled"):
		if ps in ("Rejected", "Recalled"):
			return IGNORED, f"DUPLICATE_{et.upper()}", t.name
		if ps in ("Paid", "Returned"):
			_contradiction(t, ev, ps)
			return QUARANTINED, f"CONTRADICTORY_{et.upper()}_AFTER_{ps}", t.name
		if ps not in state.UNCERTAIN_PAYOUT:
			return QUARANTINED, f"{et.upper()}_IN_{ps}", t.name
		target = "Recalled" if (et == "recalled" and ps == "Recall Requested") else "Rejected"
		_settle_not_executed(t, a, target, ev)
		return PROCESSED, target.upper(), t.name

	if et == "recall_refused":
		if ps != "Recall Requested":
			return IGNORED, f"STALE_RECALL_REFUSED_IN_{ps}", t.name
		state.save(t, {"last_error_code": "RECALL_REFUSED"}, {"payout_state": "Accepted"})
		return PROCESSED, "RECALL_REFUSED", t.name

	if et == "returned":
		if ps == "Returned":
			return IGNORED, "DUPLICATE_RETURNED", t.name
		if ps != "Paid":
			return QUARANTINED, f"RETURN_BEFORE_PAID_IN_{ps}", t.name
		returned = D(ev.amount) if ev.amount else D(t.payout_amount)
		ledger.post_journal(f"returned:{t.name}:{a.name}", "payout_returned", t.entity, t.receive_currency,
			[Dr("Partner Settlement Asset", returned, t.partner), Cr("Return Suspense", returned)], transfer=t.name)
		state.save(t, {"last_error_code": None}, {"payout_state": "Returned", "refund_state": "Eligible"})
		db.update_row("Remittance Payout Attempt", a.name, {"status": "Returned", "last_status_at": utc_now()})
		return PROCESSED, "RETURNED", t.name

	return QUARANTINED, f"UNSUPPORTED_EVENT_{et}", t.name


def _settle_paid(t, a, ev, our_charges):
	payout = D(t.payout_amount)
	ledger.post_journal(f"paid:{t.name}", "payout_confirmed", t.entity, t.receive_currency,
		[Dr("Payout Liability", payout), Cr("Partner Settlement Asset", payout, t.partner)], transfer=t.name)
	if our_charges > 0:
		ledger.post_journal(f"charges:{t.name}", "partner_charges", t.entity, t.receive_currency,
			[Dr("Partner Charges Expense", our_charges, t.partner), Cr("Partner Settlement Asset", our_charges, t.partner)],
			transfer=t.name)
	fee = D(t.fee)
	if fee > 0:
		ledger.post_journal(f"fee_earned:{t.name}", "fee_earned", t.entity, t.send_currency,
			[Dr("Deferred Fee", fee), Cr("Fee Revenue", fee)], transfer=t.name)
	execution.release_reservation(t, consume=True)
	ref = ev.bank_reference or a.bank_reference
	db.update_row("Remittance Payout Attempt", a.name, {"status": "Paid", "open_key": None, "bank_reference": ref,
		"last_status_at": utc_now()})
	state.save(t, {"paid_at": utc_now(), "bank_reference": ref, "closed_at": utc_now(), "last_error_code": None},
		{"payout_state": "Paid"})
	outbox.cancel_polls(a.instruction_id)
	notify.queue(t, "transfer_paid")


def _settle_not_executed(t, a, target, ev):
	execution.reverse_conversion(t, a.attempt_no, f"{target}: {ev.raw_status or ev.event_type}")
	execution.release_reservation(t, consume=False)
	db.update_row("Remittance Payout Attempt", a.name, {"status": target, "open_key": None, "last_status_at": utc_now()})
	state.save(t, {"last_error_code": f"PAYOUT_{target.upper()}"}, {"payout_state": target})
	outbox.cancel_polls(a.instruction_id)
	if not t.legal_hold:
		refunds.initiate(t, f"payout {target.lower()}")


def _contradiction(t, ev, ps):
	frappe.log_error(title="Neotec Remittance: CONTRADICTORY bank evidence",
		message=f"Transfer {t.name} is {ps}; received {ev.event_type} ({ev.name}). Investigation required.")
	state.save(t, {"last_error_code": "CONTRADICTORY_EVIDENCE"}, {"recon_state": "Exception"} if t.recon_state != "Exception" else None)
