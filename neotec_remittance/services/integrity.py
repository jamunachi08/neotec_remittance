"""Tamper evidence for the subledger.

1. Database triggers block UPDATE/DELETE on ledger lines and on the immutable columns of journals.
   (Requires TRIGGER privilege; on managed hosting where it is unavailable the status is recorded and
   the hash chain below remains the detective control.)
2. A single sealer links journals into a SHA-256 chain in creation order, outside the posting transaction,
   so posting never serialises on a global chain head.
3. verify_chain() recomputes every content hash from ledger lines and every chain link."""
import frappe

from neotec_remittance.services.ledger import journal_content
from neotec_remittance.utils.hashing import digest, sha256_hex
from neotec_remittance.utils.money import D
from neotec_remittance.utils.timeutil import utc_now

GENESIS = "0" * 64
LOCK_NAME = "neotec_remittance_chain_sealer"

TRIGGERS = {
	"nr_ledger_line_no_update": ("BEFORE UPDATE", "tabRemittance Ledger Line",
		"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Neotec Remittance ledger lines are append-only'"),
	"nr_ledger_line_no_delete": ("BEFORE DELETE", "tabRemittance Ledger Line",
		"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Neotec Remittance ledger lines cannot be deleted'"),
	"nr_journal_no_delete": ("BEFORE DELETE", "tabRemittance Journal",
		"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Neotec Remittance journals cannot be deleted'"),
	"nr_journal_guard_update": ("BEFORE UPDATE", "tabRemittance Journal", """BEGIN
		IF NOT (OLD.event_id <=> NEW.event_id AND OLD.event_type <=> NEW.event_type AND OLD.entity <=> NEW.entity
			AND OLD.currency <=> NEW.currency AND OLD.total <=> NEW.total AND OLD.transfer <=> NEW.transfer
			AND OLD.content_hash <=> NEW.content_hash AND OLD.posted_at <=> NEW.posted_at
			AND OLD.accounting_date <=> NEW.accounting_date AND OLD.reverses <=> NEW.reverses) THEN
			SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Neotec Remittance journal content is immutable';
		END IF;
		IF OLD.sealed = 1 AND NOT (OLD.chain_seq <=> NEW.chain_seq AND OLD.prev_hash <=> NEW.prev_hash
			AND OLD.chain_hash <=> NEW.chain_hash AND NEW.sealed = 1) THEN
			SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Neotec Remittance journal seal is immutable';
		END IF;
	END"""),
}


def install_triggers() -> bool:
	ok = True
	for name, (timing, table, body) in TRIGGERS.items():
		try:
			frappe.db.commit()  # DDL commits implicitly; start from a clean transaction
			frappe.db.sql(f"DROP TRIGGER IF EXISTS `{name}`")
			frappe.db.sql(f"CREATE TRIGGER `{name}` {timing} ON `{table}` FOR EACH ROW {body}")
			frappe.db.commit()
		except Exception as e:
			ok = False
			frappe.log_error(title="Neotec Remittance: trigger install failed", message=f"{name}: {e}")
	from neotec_remittance.services.settings import set_values

	set_values({"db_triggers_installed": 1 if ok else 0})
	return ok


def chain_link(prev_hash: str, content_hash: str, seq: int) -> str:
	return sha256_hex(f"{seq}:{prev_hash}:{content_hash}")


def seal_chain(batch: int = 1000) -> int:
	got = frappe.db.sql("SELECT GET_LOCK(%s, 0)", LOCK_NAME)[0][0]
	if not got:
		return 0
	try:
		head = frappe.db.sql("""SELECT chain_seq, chain_hash FROM `tabRemittance Journal`
			WHERE sealed=1 ORDER BY chain_seq DESC LIMIT 1""", as_dict=True)
		seq, prev = (head[0].chain_seq, head[0].chain_hash) if head else (0, GENESIS)
		rows = frappe.db.sql("""SELECT name, content_hash FROM `tabRemittance Journal` WHERE sealed=0
			ORDER BY creation, name LIMIT %s""", batch, as_dict=True)
		for r in rows:
			seq += 1
			link = chain_link(prev, r.content_hash, seq)
			frappe.db.sql("""UPDATE `tabRemittance Journal` SET chain_seq=%s, prev_hash=%s, chain_hash=%s, sealed=1
				WHERE name=%s AND sealed=0""", (seq, prev, link, r.name))
			prev = link
		frappe.db.commit()
		return len(rows)
	finally:
		frappe.db.sql("SELECT RELEASE_LOCK(%s)", LOCK_NAME)


def recompute_content_hash(journal) -> str:
	lines = frappe.db.sql("""SELECT account, debit, credit FROM `tabRemittance Ledger Line`
		WHERE journal=%s ORDER BY line_no""", journal.name, as_dict=True)
	content = journal_content(journal.event_id, journal.event_type, journal.entity, journal.currency,
		journal.transfer, journal.accounting_date,
		[{"account": l.account, "debit": D(l.debit), "credit": D(l.credit)} for l in lines])
	return digest(content)


def verify_chain(limit: int | None = None) -> dict:
	problems = []
	prev, expected_seq = GENESIS, 1
	sql = """SELECT name, event_id, event_type, entity, currency, transfer, accounting_date, content_hash,
		chain_seq, prev_hash, chain_hash FROM `tabRemittance Journal` WHERE sealed=1 ORDER BY chain_seq"""
	checked = 0
	for j in frappe.db.sql(sql + (f" LIMIT {int(limit)}" if limit else ""), as_dict=True):
		checked += 1
		if j.chain_seq != expected_seq:
			problems.append(f"sequence gap before {j.name}: expected {expected_seq}, found {j.chain_seq}")
		if j.prev_hash != prev:
			problems.append(f"broken link at {j.name}")
		if chain_link(j.prev_hash, j.content_hash, j.chain_seq) != j.chain_hash:
			problems.append(f"chain hash mismatch at {j.name}")
		if recompute_content_hash(j) != j.content_hash:
			problems.append(f"content altered at {j.name} ({j.event_id})")
		prev, expected_seq = j.chain_hash, j.chain_seq + 1
		if len(problems) > 50:
			break
	status = "OK" if not problems else "FAILED: " + "; ".join(problems[:10])
	from neotec_remittance.services.settings import set_values

	set_values({"last_chain_verification": utc_now(), "chain_verification_status": f"{status} ({checked} journals)"[:1000]})
	if problems:
		frappe.log_error(title="Neotec Remittance: ledger integrity verification FAILED", message="\n".join(problems))
	frappe.db.commit()
	return {"ok": not problems, "checked": checked, "problems": problems}
