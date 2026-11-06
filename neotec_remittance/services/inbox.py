"""Durable inbox. Callbacks are acknowledged only after the event row is committed; business effects are
applied asynchronously, exactly once per (partner, event_id), under the transfer lock."""
import frappe

from neotec_remittance.utils import crypto, db
from neotec_remittance.utils.hashing import sha256_hex
from neotec_remittance.utils.timeutil import to_dt, utc_after, utc_now

JOB = "neotec_remittance.services.inbox.process_due"


def record(partner, event_id, event_type, source, instruction_id, result=None, event=None) -> tuple[str, bool]:
	"""Persist one canonical event. Returns (name, created). Duplicate (partner, event_id) is a no-op."""
	dedup_key = f"{partner}|{event_id}"
	existing = db.find_row("Remittance Inbox", {"dedup_key": dedup_key}, "name")
	if existing:
		return existing.name, False
	if event is not None:
		raw = event.raw
		values = {"bank_reference": event.bank_reference, "raw_status": event.raw_status, "currency": event.currency,
			"amount": event.amount, "charges_amount": event.charges_amount, "source_sequence": event.source_sequence,
			"occurred_at": to_dt(event.occurred_at.replace("Z", "").replace("T", " ")) if event.occurred_at else None}
	else:
		raw = frappe.as_json(result.as_dict()) if result else ""
		values = {"bank_reference": getattr(result, "bank_reference", None), "raw_status": getattr(result, "raw_status", None),
			"charges_amount": getattr(result, "charges_amount", None), "amount": getattr(result, "delivered_amount", None)}
	from neotec_remittance.adapters import get_adapter

	try:
		mapping_version = get_adapter(partner).mapping_version
	except Exception:
		mapping_version = None
	db.savepoint("nr_inbox")
	try:
		name = db.insert_row("Remittance Inbox", {"partner": partner, "event_id": event_id, "dedup_key": dedup_key,
			"source": source, "event_type": event_type, "instruction_id": instruction_id, "status": "Received",
			"received_at": utc_now(), "evidence_hash": sha256_hex(raw or ""), "payload_enc": crypto.encrypt(raw) if raw else None,
			"mapping_version": mapping_version, "attempts": 0, **values})
	except Exception as e:
		if not db.is_duplicate(e):
			raise
		db.rollback_to("nr_inbox")
		return db.find_row("Remittance Inbox", {"dedup_key": dedup_key}, "name").name, False
	frappe.db.after_commit.add(kick)
	return name, True


def kick():
	try:
		frappe.enqueue(JOB, queue="short", job_id="neotec_remittance_inbox", deduplicate=True)
	except TypeError:
		frappe.enqueue(JOB, queue="short")
	except Exception:
		pass


def process_due(limit: int = 100):
	frappe.db.sql("""UPDATE `tabRemittance Inbox` SET status='Received' WHERE status='Leased' AND lease_until<%s""", utc_now())
	frappe.db.commit()
	rows = frappe.db.sql("""SELECT name FROM `tabRemittance Inbox` WHERE status='Received'
		ORDER BY received_at, creation LIMIT %s""", limit, as_dict=True)
	for r in rows:
		frappe.db.sql("""UPDATE `tabRemittance Inbox` SET status='Leased', lease_until=%s, attempts=attempts+1
			WHERE name=%s AND status='Received'""", (utc_after(120), r.name))
		if db.affected_rows() != 1:
			frappe.db.commit()
			continue
		frappe.db.commit()
		process_one(r.name)


def process_one(name: str):
	from neotec_remittance.services import events

	ev = db.get_row("Remittance Inbox", name)
	try:
		status, result, transfer = events.apply(ev)
		db.update_row("Remittance Inbox", name, {"status": status, "result": (result or "")[:500], "transfer": transfer})
		frappe.db.commit()
	except Exception as e:
		frappe.db.rollback()
		frappe.log_error(title=f"Neotec Remittance: inbox event {name} failed")
		attempts = (ev.attempts or 0) + 1
		db.update_row("Remittance Inbox", name, {"status": "Quarantined" if attempts >= 5 else "Received",
			"result": f"error: {type(e).__name__}: {str(e)[:300]}"})
		frappe.db.commit()
	if frappe.flags.get("nr_after_event"):
		for fn in frappe.flags.pop("nr_after_event"):
			try:
				fn()
				frappe.db.commit()
			except Exception:
				frappe.db.rollback()
				frappe.log_error(title="Neotec Remittance: post-event follow-up failed")
