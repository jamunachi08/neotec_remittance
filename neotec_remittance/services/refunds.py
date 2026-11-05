"""Refunds, recalls and cancellation. No refund is released while payout execution is uncertain."""
import frappe

from neotec_remittance.services import execution, ledger, notify, outbox, state
from neotec_remittance.services.audit import note
from neotec_remittance.services.ledger import Cr, Dr
from neotec_remittance.services.settings import get_settings
from neotec_remittance.utils import db
from neotec_remittance.utils.errors import GuardFailed, ValidationFailed
from neotec_remittance.utils.money import D, positive_amount
from neotec_remittance.utils.timeutil import utc_now


def request_cancel(transfer_name: str, by: str) -> dict:
	"""Cancellation and dispatch contend for the same transfer lock: whoever commits first wins."""
	t = state.lock_transfer(transfer_name)
	ps = t.payout_state
	if ps in ("Paid", "Returned"):
		return {"result": "NOT_CANCELLABLE", "status": t.customer_status}
	if ps in ("Cancelled", "Rejected", "Recalled"):
		return {"result": "ALREADY_CLOSED", "status": t.customer_status}
	if ps in ("Not Ready", "Ready") and not t.dispatch_right_consumed:
		execution.release_reservation(t)
		changes = {"cancel_requested": 1, "closed_at": utc_now()}
		state.save(t, changes, {"payout_state": "Cancelled"})
		if D(t.funded_amount) > 0 and not t.legal_hold:
			initiate(t, f"cancelled by {by}")
		note("Remittance Transfer", t.name, f"Cancelled before dispatch by {by}")
		return {"result": "CANCELLED", "status": t.customer_status}
	# Dispatch already won: cancellation becomes a recall request.
	state.save(t, {"cancel_requested": 1})
	if ps in ("Accepted", "Unknown"):
		requested = request_recall(t, f"customer cancellation by {by}")
		return {"result": "RECALL_REQUESTED" if requested else "RECALL_NOT_SUPPORTED", "status": t.customer_status}
	return {"result": "RECALL_PENDING", "status": t.customer_status}


def request_recall(t, reason) -> bool:
	partner = frappe.get_cached_doc("Remittance Partner", t.partner)
	if not partner.cap_recall:
		note("Remittance Transfer", t.name, "Cancellation requested after dispatch; partner has no recall service. Operations follow-up required.")
		return False
	if t.payout_state not in ("Accepted", "Unknown"):
		return False
	a = db.get_row("Remittance Payout Attempt", t.payout_attempt, "instruction_id, bank_reference")
	outbox.enqueue("recall_payment", t.name, t.partner, {"instruction_id": a.instruction_id,
		"bank_reference": a.bank_reference or t.bank_reference, "reason": reason}, a.instruction_id,
		payout_attempt=t.payout_attempt)
	state.save(t, {}, {"payout_state": "Recall Requested"})
	return True


def initiate(t, reason: str):
	"""Caller holds the transfer lock. Moves customer money to Refund Payable."""
	if t.legal_hold:
		raise GuardFailed("Funds are under legal hold", code="LEGAL_HOLD")
	if t.payout_state in state.UNCERTAIN_PAYOUT:
		raise GuardFailed("Refund is blocked while payout execution is uncertain", code="REFUND_WHILE_UNCERTAIN")
	if t.payout_state in ("Paid", "Returned"):
		raise GuardFailed("Delivered transfers are refunded through the return workflow", code="REFUND_NOT_APPLICABLE")
	if t.refund_state not in ("None", "Eligible"):
		return
	funded = D(t.funded_amount)
	if funded <= 0:
		return
	cur = t.send_currency
	if t.funding_state == "Funded":
		principal, fee, vat = D(t.principal), D(t.fee), D(t.vat)
		if get_settings().refund_fee_on_cancel:
			lines = [Dr("Customer Liability", principal), Dr("Deferred Fee", fee), Dr("VAT Payable", vat),
				Cr("Refund Payable", principal + fee + vat)]
			amount = principal + fee + vat
		else:
			lines = [Dr("Customer Liability", principal), Cr("Refund Payable", principal)]
			amount = principal
			if fee > 0:
				ledger.post_journal(f"fee_retained:{t.name}", "fee_retained", t.entity, cur,
					[Dr("Deferred Fee", fee), Cr("Fee Revenue", fee)], transfer=t.name)
	else:
		lines = [Dr("Customer Liability", funded), Cr("Refund Payable", funded)]
		amount = funded
	ledger.post_journal(f"refund_payable:{t.name}", "refund_payable", t.entity, cur, lines, transfer=t.name,
		narration=reason)
	if t.refund_state == "None":
		state.save(t, {}, {"refund_state": "Eligible"})
	state.save(t, {"refund_amount": amount, "refund_reference": None}, {"refund_state": "Pending"})


def confirm(transfer_name: str, bank_reference: str, by: str):
	if not bank_reference:
		raise ValidationFailed("Bank reference of the refund payment is required")
	t = state.lock_transfer(transfer_name)
	if t.refund_state != "Pending":
		raise GuardFailed(f"Refund is {t.refund_state}, not Pending")
	amount = D(t.refund_amount)
	collection = get_settings().collection_account
	bank_line = {"account": collection, "debit": 0, "credit": amount} if collection else Cr("Safeguarded Bank", amount)
	ledger.post_journal(f"refund_paid:{t.name}", "refund_paid", t.entity, t.send_currency,
		[Dr("Refund Payable", amount), bank_line], transfer=t.name, narration=f"Refund bank ref {bank_reference}")
	state.save(t, {"refund_reference": bank_reference, "closed_at": utc_now()}, {"refund_state": "Refunded"})
	note("Remittance Transfer", t.name, f"Refund of {amount} {t.send_currency} confirmed by {by} (ref {bank_reference})")
	notify.queue(t, "refund_completed")


def initiate_return_refund(transfer_name: str, refund_amount, by: str):
	"""After a post-payment return: Finance converts the returned payout-currency funds back and sets the
	send-currency amount owed to the customer (treasury rate, approved by Finance)."""
	t = state.lock_transfer(transfer_name)
	if t.payout_state != "Returned" or t.refund_state != "Eligible":
		raise GuardFailed("Transfer is not an eligible returned payment")
	amount = positive_amount(refund_amount, t.send_currency, "refund_amount")
	returned = D(frappe.db.sql("""SELECT COALESCE(SUM(credit),0) FROM `tabRemittance Ledger Line`
		WHERE transfer=%s AND account_role='Return Suspense'""", t.name)[0][0])
	ledger.post_journal(f"return_convert:{t.name}:{t.receive_currency}", "return_conversion", t.entity, t.receive_currency,
		[Dr("Return Suspense", returned), Cr("FX Bridge", returned)], transfer=t.name)
	ledger.post_journal(f"return_convert:{t.name}:{t.send_currency}", "return_conversion", t.entity, t.send_currency,
		[Dr("FX Bridge", amount), Cr("Refund Payable", amount)], transfer=t.name)
	state.save(t, {"refund_amount": amount}, {"refund_state": "Pending"})
	note("Remittance Transfer", t.name, f"Return refund of {amount} {t.send_currency} set by {by}")


def refund_receipt_excess(receipt_name: str, by: str):
	r = db.lock_row("Remittance Funding Receipt", receipt_name)
	excess = D(r.amount) - D(r.allocated_amount)
	if excess <= 0 or r.status in ("Refund Pending", "Refunded"):
		raise GuardFailed("Nothing to refund on this receipt")
	entity = frappe.db.get_value("Remittance Account", r.bank_account, "entity")
	ledger.post_journal(f"receipt_refund_payable:{r.name}", "refund_payable", entity, r.currency,
		[Dr("Unallocated Receipts", excess), Cr("Refund Payable", excess)], narration=f"Unallocated/excess receipt refund by {by}")
	db.update_row("Remittance Funding Receipt", r.name, {"status": "Refund Pending"})


def confirm_receipt_refund(receipt_name: str, bank_reference: str, by: str):
	r = db.lock_row("Remittance Funding Receipt", receipt_name)
	if r.status != "Refund Pending":
		raise GuardFailed("Receipt refund is not pending")
	excess = D(r.amount) - D(r.allocated_amount)
	entity = frappe.db.get_value("Remittance Account", r.bank_account, "entity")
	ledger.post_journal(f"receipt_refund_paid:{r.name}", "refund_paid", entity, r.currency,
		[Dr("Refund Payable", excess), {"account": r.bank_account, "debit": 0, "credit": excess}],
		narration=f"Refund bank ref {bank_reference}")
	db.update_row("Remittance Funding Receipt", r.name, {"status": "Refunded"})
	note("Remittance Funding Receipt", r.name, f"Refund confirmed by {by} (ref {bank_reference})")
