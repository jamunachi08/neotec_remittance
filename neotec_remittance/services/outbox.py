"""Durable transactional outbox.

A command row is written in the same DB transaction as the state change that requires it. Workers claim
rows with an atomic conditional UPDATE (lease), commit the claim, and only then call the bank, so no DB
transaction is open during network I/O. Redis/RQ only wakes workers; the table is the source of truth,
and a scheduler sweep recovers anything that was committed but never queued, or whose lease expired.

Side-effecting commands (create_payment, create_refund, recall_payment) are never resubmitted after an
uncertain outcome unless the partner guarantees duplicate prevention on the same key. Exhausted retries go
to the Exception queue; they never become a financial failure state."""
import os
import socket
from datetime import timedelta

import frappe

from neotec_remittance.adapters import get_adapter
from neotec_remittance.adapters.base import NOT_FOUND, NOT_SENT, OUTCOME_TO_EVENT, PENDING, UNCERTAIN
from neotec_remittance.services.settings import get_settings, poll_schedule
from neotec_remittance.utils import crypto, db
from neotec_remittance.utils.hashing import canonical_json, digest
from neotec_remittance.utils.timeutil import utc_after, utc_now

SIDE_EFFECTING = {"create_payment", "create_refund", "recall_payment"}
JOB = "neotec_remittance.services.outbox.process_due"


def enqueue(command_type: str, transfer: str | None, partner: str, payload: dict, idempotency_key: str,
		payout_attempt: str | None = None, delay_seconds: int = 0, command_id: str | None = None) -> str:
	command_id = command_id or f"{command_type}:{idempotency_key}"
	existing = db.find_row("Remittance Outbox", {"command_id": command_id}, "name")
	if existing:
		return existing.name
	name = db.insert_row("Remittance Outbox", {
		"command_id": command_id, "command_type": command_type, "transfer": transfer, "partner": partner,
		"payout_attempt": payout_attempt, "idempotency_key": idempotency_key,
		"side_effecting": 1 if command_type in SIDE_EFFECTING else 0, "status": "Pending", "attempts": 0,
		"max_attempts": get_settings().max_dispatch_attempts or 8, "next_attempt_at": utc_after(delay_seconds),
		"command_hash": digest(payload), "payload_enc": crypto.encrypt(canonical_json(payload)),
	})
	if not delay_seconds:
		frappe.db.after_commit.add(kick)
	return name


def kick():
	try:
		frappe.enqueue(JOB, queue="short", job_id="neotec_remittance_outbox", deduplicate=True)
	except TypeError:
		frappe.enqueue(JOB, queue="short")
	except Exception:
		pass  # the scheduler sweep will pick it up


def _owner():
	return f"{socket.gethostname()}:{os.getpid()}:{db.new_name()[:8]}"


def claim(name: str) -> str | None:
	owner = _owner()
	lease = get_settings().dispatch_lease_seconds or 120
	frappe.db.sql("""UPDATE `tabRemittance Outbox` SET status='Leased', lease_owner=%s, lease_until=%s,
		attempts=attempts+1 WHERE name=%s AND status='Pending' AND next_attempt_at<=%s""",
		(owner, utc_after(lease), name, utc_now()))
	got = db.affected_rows() == 1
	frappe.db.commit()
	return owner if got else None


def _finish(name, owner, values: dict):
	n = db.update_row("Remittance Outbox", name, values, where={"lease_owner": owner, "status": "Leased"})
	if n != 1:
		frappe.log_error(title="Neotec Remittance: outbox lease lost", message=f"{name} result not recorded by {owner}")
	return n == 1


def process_due(limit: int = 50):
	settings = get_settings()
	if not settings.enabled:
		return
	recover_expired_leases()
	rows = frappe.db.sql("""SELECT name, command_type FROM `tabRemittance Outbox`
		WHERE status='Pending' AND next_attempt_at<=%s ORDER BY next_attempt_at LIMIT %s""",
		(utc_now(), limit), as_dict=True)
	for r in rows:
		if r.command_type in SIDE_EFFECTING and r.command_type != "recall_payment" and get_settings().dispatch_paused:
			continue
		owner = claim(r.name)
		if owner:
			try:
				process_one(r.name, owner)
			except Exception:
				frappe.db.rollback()
				frappe.log_error(title=f"Neotec Remittance: outbox processing error {r.name}")
			frappe.db.commit()
	process_inbox()


def process_inbox():
	from neotec_remittance.services import inbox

	inbox.process_due()


def process_one(name: str, owner: str):
	row = db.get_row("Remittance Outbox", name)
	payload = frappe.parse_json(crypto.decrypt(row.payload_enc))
	adapter = get_adapter(row.partner)
	partner = adapter.partner
	try:
		if row.command_type == "create_payment":
			from neotec_remittance.services.execution import build_payment_request

			result = adapter.create_payment(build_payment_request(payload))
		elif row.command_type == "get_payment_status":
			result = adapter.get_payment_status(payload["instruction_id"], payload.get("bank_reference"))
		elif row.command_type == "recall_payment":
			result = adapter.recall_payment(payload["instruction_id"], payload.get("bank_reference"), payload.get("reason"))
		elif row.command_type == "create_refund":
			result = adapter.create_refund(payload)
		else:
			raise ValueError(f"Unknown command {row.command_type}")
	except Exception as e:
		from neotec_remittance.adapters.base import AdapterResult

		frappe.log_error(title=f"Neotec Remittance: adapter exception ({row.command_type})", message=type(e).__name__)
		result = AdapterResult(UNCERTAIN, reason=f"adapter exception {type(e).__name__}")

	_record_outcome(row, owner, payload, partner, result)


def _backoff(attempts: int) -> int:
	sched = poll_schedule()
	return sched[min(attempts - 1, len(sched) - 1)]


def _record_outcome(row, owner, payload, partner, result):
	from neotec_remittance.services import inbox

	attempts = row.attempts
	exhausted = attempts >= (row.max_attempts or 8)
	err = {"last_error_code": result.outcome if result.outcome in (NOT_SENT, UNCERTAIN, NOT_FOUND) else None,
		"last_error": (result.reason or "")[:300] or None}

	if row.command_type == "create_payment":
		if result.outcome in OUTCOME_TO_EVENT:
			# PENDING from a create call means the bank accepted the instruction.
			inbox.record(row.partner, f"resp:{row.command_id}:{attempts}", OUTCOME_TO_EVENT[result.outcome],
				"Dispatch Response", payload["instruction_id"], result)
			_finish(row.name, owner, {"status": "Completed", "completed_at": utc_now(), "disposition": result.outcome, **err})
			if result.outcome in ("ACCEPTED", PENDING):
				# Fallback polling even when callbacks are expected; cancelled once a final outcome lands.
				schedule_status_poll(row.transfer, row.partner, row.payout_attempt, payload["instruction_id"],
					result.bank_reference, 0)
		elif result.outcome == NOT_SENT and not exhausted:
			_finish(row.name, owner, {"status": "Pending", "next_attempt_at": utc_after(_backoff(attempts)), **err})
		elif result.outcome == NOT_SENT:
			_finish(row.name, owner, {"status": "Exception", "disposition": "NOT_SENT_EXHAUSTED", **err})
		elif partner.cap_idempotent_create and not exhausted:
			# Bank guarantees duplicate prevention for the same instruction id: safe to resubmit.
			_finish(row.name, owner, {"status": "Pending", "next_attempt_at": utc_after(_backoff(attempts)), **err})
		else:
			inbox.record(row.partner, f"uncertain:{row.command_id}", "submission_uncertain", "Dispatch Response",
				payload["instruction_id"], result)
			_finish(row.name, owner, {"status": "Awaiting Status", "disposition": "UNCERTAIN", **err})
			schedule_status_poll(row.transfer, row.partner, row.payout_attempt, payload["instruction_id"], None, 0)
	elif row.command_type == "get_payment_status":
		poll_no = payload.get("poll_no", 0)
		if result.outcome in OUTCOME_TO_EVENT and result.outcome != PENDING:
			inbox.record(row.partner, f"poll:{payload['instruction_id']}:{result.outcome}:{result.bank_reference or ''}",
				OUTCOME_TO_EVENT[result.outcome], "Status Poll", payload["instruction_id"], result)
			_finish(row.name, owner, {"status": "Completed", "completed_at": utc_now(), "disposition": result.outcome, **err})
		elif result.outcome == NOT_FOUND and partner.cap_not_found_is_final:
			inbox.record(row.partner, f"poll:{payload['instruction_id']}:NOT_FOUND", "not_found_final", "Status Poll",
				payload["instruction_id"], result)
			_finish(row.name, owner, {"status": "Completed", "completed_at": utc_now(), "disposition": "NOT_FOUND_FINAL", **err})
		else:
			if result.outcome == PENDING and result.bank_reference:
				inbox.record(row.partner, f"poll:{payload['instruction_id']}:ACCEPTED:{result.bank_reference}", "accepted",
					"Status Poll", payload["instruction_id"], result)
			_finish(row.name, owner, {"status": "Completed", "completed_at": utc_now(),
				"disposition": f"STILL_{result.outcome}", **err})
			if poll_no + 1 < 50:
				schedule_status_poll(row.transfer, row.partner, row.payout_attempt, payload["instruction_id"],
					result.bank_reference or payload.get("bank_reference"), poll_no + 1)
			else:
				_alert_exception(row, "status polling exhausted")
	else:
		if result.outcome in OUTCOME_TO_EVENT:
			_finish(row.name, owner, {"status": "Completed", "completed_at": utc_now(), "disposition": result.outcome, **err})
		elif result.outcome == NOT_SENT and not exhausted:
			_finish(row.name, owner, {"status": "Pending", "next_attempt_at": utc_after(_backoff(attempts)), **err})
		else:
			_finish(row.name, owner, {"status": "Exception", "disposition": result.outcome, **err})


def schedule_status_poll(transfer, partner, payout_attempt, instruction_id, bank_reference, poll_no):
	partner_doc = frappe.get_cached_doc("Remittance Partner", partner)
	if not partner_doc.cap_status_inquiry:
		return
	delay = _backoff(poll_no + 1)
	enqueue("get_payment_status", transfer, partner, {"instruction_id": instruction_id,
		"bank_reference": bank_reference, "poll_no": poll_no}, f"{instruction_id}:{poll_no}",
		payout_attempt=payout_attempt, delay_seconds=delay)


def cancel_polls(instruction_id: str):
	frappe.db.sql("""UPDATE `tabRemittance Outbox` SET status='Cancelled', disposition='RESOLVED'
		WHERE command_type='get_payment_status' AND status='Pending' AND idempotency_key LIKE %s""",
		(instruction_id + ":%",))
	frappe.db.sql("""UPDATE `tabRemittance Outbox` SET status='Completed', completed_at=%s, disposition='RESOLVED_BY_EVIDENCE'
		WHERE command_type='create_payment' AND status='Awaiting Status' AND idempotency_key=%s""",
		(utc_now(), instruction_id))


def recover_expired_leases():
	rows = frappe.db.sql("""SELECT name, command_type, lease_owner, transfer, partner, payout_attempt, payload_enc, command_id
		FROM `tabRemittance Outbox` WHERE status='Leased' AND lease_until<%s LIMIT 100""", utc_now(), as_dict=True)
	from neotec_remittance.adapters.base import AdapterResult
	from neotec_remittance.services import inbox

	for r in rows:
		if r.command_type in SIDE_EFFECTING:
			partner = frappe.get_cached_doc("Remittance Partner", r.partner)
			if r.command_type == "create_payment" and not partner.cap_idempotent_create:
				payload = frappe.parse_json(crypto.decrypt(r.payload_enc))
				n = db.update_row("Remittance Outbox", r.name, {"status": "Awaiting Status", "disposition": "LEASE_EXPIRED_UNCERTAIN"},
					where={"status": "Leased", "lease_owner": r.lease_owner})
				if n == 1:
					inbox.record(r.partner, f"uncertain:{r.command_id}", "submission_uncertain", "Internal",
						payload["instruction_id"], AdapterResult(UNCERTAIN, reason="worker lease expired"))
					schedule_status_poll(r.transfer, r.partner, r.payout_attempt, payload["instruction_id"], None, 0)
			elif r.command_type == "create_payment":
				db.update_row("Remittance Outbox", r.name, {"status": "Pending", "next_attempt_at": utc_now()},
					where={"status": "Leased", "lease_owner": r.lease_owner})
			else:
				db.update_row("Remittance Outbox", r.name, {"status": "Exception", "disposition": "LEASE_EXPIRED_UNCERTAIN"},
					where={"status": "Leased", "lease_owner": r.lease_owner})
		else:
			db.update_row("Remittance Outbox", r.name, {"status": "Pending", "next_attempt_at": utc_now()},
				where={"status": "Leased", "lease_owner": r.lease_owner})
		frappe.db.commit()


def _alert_exception(row, why):
	frappe.log_error(title="Neotec Remittance: outbox exception", message=f"{row.name} {row.command_type}: {why}")


def redrive(name: str):
	"""Operator redrive from the Exception queue. Reuses the original command identity and payload."""
	frappe.only_for(("Remittance Manager", "System Manager"))
	row = db.get_row("Remittance Outbox", name)
	if row.status != "Exception":
		frappe.throw("Only Exception commands can be redriven")
	if row.command_type == "create_payment":
		frappe.throw("Payment submissions cannot be redriven; resolve them with a status inquiry or partner evidence.")
	db.update_row("Remittance Outbox", name, {"status": "Pending", "next_attempt_at": utc_now(),
		"max_attempts": (row.max_attempts or 8) + 3})
	from neotec_remittance.services.audit import note

	note("Remittance Outbox", name, f"Redriven by {frappe.session.user}")
	frappe.db.after_commit.add(kick)
