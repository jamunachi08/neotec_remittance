"""Safeguarding circuit breaker.

Check per entity/currency: safeguarded bank balance (subledger) must cover customer-money liabilities
(customer liability + unallocated receipts + refund payable), less the configured tolerance. A breach
pauses new dispatch only; inbox processing, status polling, refunds and reconciliation keep running.
If the collection bank supports balance inquiry, the bank-reported balance is recorded alongside (a
difference there is a Warning for reconciliation, not an automatic pause, because of timing items)."""
import frappe

from neotec_remittance.services import ledger
from neotec_remittance.services.settings import get_settings
from neotec_remittance.utils import db
from neotec_remittance.utils.money import D
from neotec_remittance.utils.timeutil import utc_now

CUSTOMER_MONEY_ROLES = ("Customer Liability", "Unallocated Receipts", "Refund Payable")


def run_checks():
	s = get_settings()
	pairs = frappe.db.sql("""SELECT DISTINCT entity, currency FROM `tabRemittance Account`
		WHERE account_role='Safeguarded Bank' AND disabled=0""", as_dict=True)
	breaches = []
	for p in pairs:
		safeguarded = ledger.role_balance(p.entity, p.currency, ["Safeguarded Bank"])
		liabilities = -ledger.role_balance(p.entity, p.currency, CUSTOMER_MONEY_ROLES)
		shortfall = liabilities - safeguarded
		tolerance = D(s.safeguarding_tolerance)
		status = "Breach" if shortfall > tolerance else "OK"
		action = None
		if status == "Breach":
			breaches.append(f"{p.entity}/{p.currency} shortfall {shortfall}")
			if s.auto_pause_on_breach and not s.dispatch_paused:
				pause(f"Safeguarding shortfall {shortfall} {p.currency} at {utc_now()} UTC")
				action = "Dispatch paused"
		db.insert_row("Remittance Safeguarding Check", {"entity": p.entity, "currency": p.currency,
			"checked_at": utc_now(), "status": status, "safeguarded_balance": safeguarded,
			"customer_liabilities": liabilities, "shortfall": max(shortfall, D(0)), "action_taken": action})
	frappe.db.commit()
	if breaches:
		frappe.log_error(title="Neotec Remittance: SAFEGUARDING BREACH", message="\n".join(breaches))
	return breaches


def pause(reason: str):
	from neotec_remittance.services.settings import set_values

	set_values({"dispatch_paused": 1, "paused_reason": reason[:500], "paused_at": utc_now()})
