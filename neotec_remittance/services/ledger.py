"""Append-only double-entry subledger.

Rules enforced here (and again by database triggers where the DB user allows them):
- one journal per business event_id (unique); replay with identical content is a no-op, different content is an error
- each journal is a single legal entity and a single currency, balanced exactly in Decimal
- each line has exactly one positive side, expressed in the currency's minor unit
- posted rows are never updated or deleted; corrections are reversing journals
- each journal carries a content hash; a sealer job links journals into a hash chain off the hot path"""
from decimal import Decimal

import frappe

from neotec_remittance.services import accounts
from neotec_remittance.utils import db
from neotec_remittance.utils.errors import LedgerError
from neotec_remittance.utils.hashing import digest
from neotec_remittance.utils.money import D, amount_str, is_exact
from neotec_remittance.utils.timeutil import riyadh_date, utc_now

ZERO = Decimal(0)


def Dr(role, amount, partner=None):
	return {"role": role, "debit": D(amount), "credit": ZERO, "partner": partner}


def Cr(role, amount, partner=None):
	return {"role": role, "debit": ZERO, "credit": D(amount), "partner": partner}


def journal_content(event_id, event_type, entity, currency, transfer, accounting_date, lines) -> dict:
	return {
		"event_id": event_id, "event_type": event_type, "entity": entity, "currency": currency,
		"transfer": transfer or "", "accounting_date": str(accounting_date),
		"lines": [[l["account"], amount_str(l["debit"], currency), amount_str(l["credit"], currency)] for l in lines],
	}


def post_journal(event_id: str, event_type: str, entity: str, currency: str, lines: list[dict],
		transfer: str | None = None, accounting_date=None, narration: str | None = None,
		reverses: str | None = None) -> str:
	if not event_id or not lines:
		raise LedgerError("event_id and lines are required")
	accounting_date = accounting_date or riyadh_date()
	resolved = []
	total_dr = total_cr = ZERO
	for line in [l for l in lines if D(l.get("debit")) or D(l.get("credit"))]:
		dr, cr = D(line.get("debit")), D(line.get("credit"))
		if dr < 0 or cr < 0 or (dr and cr):
			raise LedgerError(f"Line for {line.get('role') or line.get('account')} must have exactly one positive side")
		if not (is_exact(dr, currency) and is_exact(cr, currency)):
			raise LedgerError(f"Amount precision exceeds {currency} minor unit in event {event_id}")
		if line.get("account"):
			acc = db.get_row("Remittance Account", line["account"],
				"name, account_role, currency, entity, balance_mode, enforce_sign, normal_side, disabled")
			if not acc:
				raise LedgerError(f"Unknown account {line['account']}")
		else:
			acc = accounts.resolve(entity, currency, line["role"], line.get("partner"))
		if acc.currency != currency or acc.entity != entity:
			raise LedgerError(f"Account {acc.name} does not belong to {entity}/{currency}")
		resolved.append({"account": acc.name, "role": acc.account_role, "debit": dr, "credit": cr,
			"mode": acc.balance_mode, "enforce_sign": acc.enforce_sign, "normal_side": acc.normal_side})
		total_dr += dr
		total_cr += cr
	if len(resolved) < 2:
		raise LedgerError(f"Journal {event_id} needs at least two lines")
	if total_dr != total_cr or total_dr <= 0:
		raise LedgerError(f"Imbalanced journal {event_id}: debit {total_dr} credit {total_cr}")

	content = journal_content(event_id, event_type, entity, currency, transfer, accounting_date, resolved)
	content_hash = digest(content)

	existing = db.find_row("Remittance Journal", {"event_id": event_id}, "name, content_hash")
	if existing:
		return _same_or_conflict(existing, content_hash, event_id)

	now = utc_now()
	name = db.new_name()
	db.savepoint("nr_journal")
	try:
		db.insert_row("Remittance Journal", {
			"event_id": event_id, "event_type": event_type, "entity": entity, "currency": currency,
			"total": total_dr, "transfer": transfer, "posted_at": now, "accounting_date": accounting_date,
			"reverses": reverses, "narration": (narration or "")[:500], "content_hash": content_hash,
			"sealed": 0, "erp_status": "Pending",
		}, name=name)
	except Exception as e:
		if not db.is_duplicate(e):
			raise
		db.rollback_to("nr_journal")
		existing = db.find_row("Remittance Journal", {"event_id": event_id}, "name, content_hash")
		return _same_or_conflict(existing, content_hash, event_id)

	for i, line in enumerate(resolved, start=1):
		db.insert_row("Remittance Ledger Line", {
			"journal": name, "event_id": event_id, "line_no": i, "entity": entity, "account": line["account"],
			"account_role": line["role"], "currency": currency, "debit": line["debit"], "credit": line["credit"],
			"transfer": transfer, "posted_at": now,
		})
	_apply_synchronous_balances(resolved)
	return name


def _same_or_conflict(existing, content_hash, event_id):
	if existing and existing.content_hash == content_hash:
		return existing.name
	raise LedgerError(f"Event {event_id} was already posted with different content", code="LEDGER_EVENT_CONFLICT")


def _apply_synchronous_balances(lines):
	deltas: dict[str, Decimal] = {}
	meta = {}
	for l in lines:
		if l["mode"] != "Synchronous":
			continue
		deltas[l["account"]] = deltas.get(l["account"], ZERO) + l["debit"] - l["credit"]
		meta[l["account"]] = l
	for account in sorted(deltas):  # canonical lock order
		frappe.db.sql("""UPDATE `tabRemittance Account` SET balance = balance + %s, row_version = row_version + 1
			WHERE name=%s""", (deltas[account], account))
		if meta[account]["enforce_sign"]:
			bal = frappe.db.sql("SELECT balance FROM `tabRemittance Account` WHERE name=%s", account)[0][0]
			if (meta[account]["normal_side"] == "Debit" and bal < 0) or (meta[account]["normal_side"] == "Credit" and bal > 0):
				raise LedgerError(f"Posting would push {account} across zero", code="BALANCE_SIGN_VIOLATION")


def reverse_journal(journal_name: str, event_id: str, narration: str | None = None) -> str:
	j = db.get_row("Remittance Journal", journal_name, "name, event_type, entity, currency, transfer")
	if not j:
		raise LedgerError(f"Journal {journal_name} not found")
	lines = frappe.db.sql("""SELECT account, debit, credit FROM `tabRemittance Ledger Line`
		WHERE journal=%s ORDER BY line_no""", journal_name, as_dict=True)
	reversed_lines = [{"account": l.account, "debit": D(l.credit), "credit": D(l.debit)} for l in lines]
	return post_journal(event_id, f"reversal:{j.event_type}", j.entity, j.currency, reversed_lines,
		transfer=j.transfer, narration=narration or f"Reversal of {journal_name}", reverses=journal_name)


def journal_for_event(event_id: str):
	return db.find_row("Remittance Journal", {"event_id": event_id}, "name")


def account_balance(account: str) -> Decimal:
	v = frappe.db.sql("""SELECT COALESCE(SUM(debit),0) - COALESCE(SUM(credit),0)
		FROM `tabRemittance Ledger Line` WHERE account=%s""", account)[0][0]
	return D(v)


def role_balance(entity, currency, roles) -> Decimal:
	roles = list(roles)
	v = frappe.db.sql(f"""SELECT COALESCE(SUM(l.debit),0) - COALESCE(SUM(l.credit),0)
		FROM `tabRemittance Ledger Line` l
		WHERE l.entity=%s AND l.currency=%s AND l.account_role IN ({', '.join(['%s'] * len(roles))})""",
		(entity, currency, *roles))[0][0]
	return D(v)


def refresh_deferred_balances():
	"""Background job: recompute running balances for Deferred accounts from ledger lines."""
	from neotec_remittance.utils.timeutil import utc_now as _now

	frappe.db.sql("""UPDATE `tabRemittance Account` a
		LEFT JOIN (SELECT account, SUM(debit) - SUM(credit) AS bal FROM `tabRemittance Ledger Line` GROUP BY account) x
		ON x.account = a.name
		SET a.balance = COALESCE(x.bal, 0), a.balance_refreshed_at = %s
		WHERE a.balance_mode = 'Deferred'""", (_now(),))
	frappe.db.commit()
