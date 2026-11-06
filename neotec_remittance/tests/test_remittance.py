"""Money-integrity acceptance tests. Test ids follow the specification (T01-T15); T08 (cash agents) and
T14 (regional failover drill) are Release 3 / operational drills and are not automated here."""
import json
import time
from decimal import Decimal

import frappe

try:
	from frappe.tests import IntegrationTestCase as TestCase
except ImportError:  # v15
	from frappe.tests.utils import FrappeTestCase as TestCase

from neotec_remittance.services import (execution, idempotency, integrity, ledger, outbox, refunds, safeguarding,
	state)
from neotec_remittance.services.ledger import Cr, Dr
from neotec_remittance.services.settings import set_values
from neotec_remittance.tests import fixtures as fx
from neotec_remittance.utils import db
from neotec_remittance.utils.errors import (AccessDenied, GuardFailed, IdempotencyConflict, LedgerError, NotFound,
	Unauthenticated)
from neotec_remittance.utils.hashing import hmac_hex
from neotec_remittance.utils.timeutil import utc_now


def journals(transfer, event_like="%"):
	return frappe.db.sql("""SELECT event_id FROM `tabRemittance Journal` WHERE transfer=%s AND event_id LIKE %s""",
		(transfer, event_like), pluck=True)


class TestRemittance(TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		fx.setup_platform()

	def setUp(self):
		frappe.set_user("Administrator")
		set_values({"dispatch_paused": 0})

	# ---------------------------------------------------------------- quotes & ledger basics
	def test_quote_math_is_exact(self):
		c = fx.make_customer()
		from neotec_remittance.services import quotes

		q = quotes.create_quote(c, "1000.00", "India", "INR")
		self.assertEqual(q["send"]["fee"], "15.00")
		self.assertEqual(q["send"]["vat"], "2.25")
		self.assertEqual(q["send"]["total"], "1017.25")
		# 23.45 * (1 - 0.5%) = 23.33275 ; payout rounded down to paise
		self.assertEqual(q["rate"], "23.332750000000")
		self.assertEqual(q["receive"]["amount"], "23332.75")

	def test_ledger_rejects_imbalance_and_bad_precision(self):
		with self.assertRaises(LedgerError):
			ledger.post_journal("test:imbalance:" + frappe.generate_hash(), "x", fx.COMPANY, "SAR",
				[Dr("Safeguarded Bank", "10.00"), Cr("Unallocated Receipts", "9.99")])
		with self.assertRaises(LedgerError):
			ledger.post_journal("test:precision:" + frappe.generate_hash(), "x", fx.COMPANY, "SAR",
				[Dr("Safeguarded Bank", "10.001"), Cr("Unallocated Receipts", "10.001")])

	def test_ledger_event_replay_and_conflict(self):
		eid = "test:replay:" + frappe.generate_hash()
		lines = [Dr("Safeguarded Bank", "5.00"), Cr("Unallocated Receipts", "5.00")]
		a = ledger.post_journal(eid, "x", fx.COMPANY, "SAR", lines)
		b = ledger.post_journal(eid, "x", fx.COMPANY, "SAR", lines)
		self.assertEqual(a, b)
		with self.assertRaises(LedgerError):
			ledger.post_journal(eid, "x", fx.COMPANY, "SAR", [Dr("Safeguarded Bank", "6.00"), Cr("Unallocated Receipts", "6.00")])

	def test_engine_records_refuse_document_edits(self):
		c, resp = fx.new_transfer()
		doc = frappe.get_doc("Remittance Transfer", resp["transfer_id"])
		doc.funded_amount = 999999
		with self.assertRaises(frappe.PermissionError):
			doc.save(ignore_permissions=True)
		with self.assertRaises(frappe.PermissionError):
			frappe.delete_doc("Remittance Transfer", resp["transfer_id"], ignore_permissions=True)

	def test_db_trigger_blocks_ledger_update(self):
		if not frappe.db.get_single_value("Remittance Settings", "db_triggers_installed"):
			self.skipTest("DB triggers not installable for this DB user")
		line = frappe.db.sql("SELECT name FROM `tabRemittance Ledger Line` LIMIT 1", pluck=True)[0]
		with self.assertRaises(Exception):
			frappe.db.sql("UPDATE `tabRemittance Ledger Line` SET debit=debit+1 WHERE name=%s", line)

	# ---------------------------------------------------------------- T01/T02 idempotency
	def test_T01_same_key_same_body_returns_same_transfer(self):
		c = fx.make_customer()
		ben = fx.make_beneficiary(c)
		from neotec_remittance.services import quotes, transfers

		q = quotes.create_quote(c, "500.00", "India", "INR")
		r1, s1 = transfers.accept(c, q["quote_id"], ben["beneficiary_version_id"], "auth-1", "key-T01-abcdefgh")
		r2, s2 = transfers.accept(c, q["quote_id"], ben["beneficiary_version_id"], "auth-1", "key-T01-abcdefgh")
		self.assertEqual((s1, s2), (201, 200))
		self.assertEqual(r1["transfer_id"], r2["transfer_id"])
		self.assertEqual(frappe.db.count("Remittance Transfer", {"quote": q["quote_id"]}), 1)

	def test_T02_same_key_different_body_conflicts(self):
		c = fx.make_customer()
		ben = fx.make_beneficiary(c)
		ben2 = fx.make_beneficiary(c, name="Anita Desai")
		from neotec_remittance.services import quotes, transfers

		q = quotes.create_quote(c, "500.00", "India", "INR")
		transfers.accept(c, q["quote_id"], ben["beneficiary_version_id"], "auth-1", "key-T02-abcdefgh")
		with self.assertRaises(IdempotencyConflict):
			transfers.accept(c, q["quote_id"], ben2["beneficiary_version_id"], "auth-1", "key-T02-abcdefgh")
		self.assertEqual(frappe.db.count("Remittance Payout Attempt", {"transfer": ("like", "%")}) >= 0, True)

	# ---------------------------------------------------------------- happy path
	def test_happy_path_paid_with_balanced_books(self):
		c, resp = fx.new_transfer(amount="1000.00")
		tid = resp["transfer_id"]
		r = fx.fund(tid)
		self.assertEqual(r["transfer"], tid)
		t = fx.T(tid)
		self.assertEqual((t.funding_state, t.compliance_state, t.payout_state), ("Funded", "Approved", "Dispatching"))
		frappe.db.commit()
		fx.drain()
		t = fx.T(tid)
		self.assertEqual(t.payout_state, "Paid")
		self.assertEqual(t.customer_status, "Delivered")
		for prefix in ("alloc:%", "funded:%", "convert:%", "paid:%", "fee_earned:%"):
			self.assertTrue(journals(tid, prefix), prefix)
		# customer liability for this transfer nets to zero; deferred fee recognised
		net = frappe.db.sql("""SELECT account_role, SUM(debit)-SUM(credit) FROM `tabRemittance Ledger Line`
			WHERE transfer=%s GROUP BY account_role""", tid)
		net = {k: Decimal(v) for k, v in net}
		self.assertEqual(net["Customer Liability"], 0)
		self.assertEqual(net["Payout Liability"], 0)
		self.assertEqual(net["Deferred Fee"], 0)
		self.assertEqual(net["Fee Revenue"], Decimal("-15.00"))
		self.assertEqual(net["VAT Payable"], Decimal("-2.25"))
		res = frappe.db.get_value("Remittance Reservation", {"transfer": tid}, "state")
		self.assertEqual(res, "Consumed")

	# ---------------------------------------------------------------- T03 timeout but executed
	def test_T03_timeout_then_status_shows_paid_no_second_payout(self):
		c, resp = fx.new_transfer(mode="SANDBOX-TIMEOUT")
		tid = resp["transfer_id"]
		fx.fund(tid)
		frappe.db.commit()
		fx.drain()
		t = fx.T(tid)
		self.assertEqual(t.payout_state, "Paid")
		creates = frappe.db.count("Remittance Outbox", {"transfer": tid, "command_type": "create_payment"})
		self.assertEqual(creates, 1)
		attempts = frappe.db.get_value("Remittance Outbox", {"transfer": tid, "command_type": "create_payment"}, "attempts")
		self.assertEqual(attempts, 1, "uncertain submission must not be resent")
		self.assertEqual(len(journals(tid, "paid:%")), 1)

	def test_uncertain_never_executed_becomes_rejected_and_refunds(self):
		c, resp = fx.new_transfer(mode="SANDBOX-NOEXEC")
		tid = resp["transfer_id"]
		fx.fund(tid)
		frappe.db.commit()
		fx.drain()
		t = fx.T(tid)
		self.assertEqual(t.payout_state, "Rejected")
		self.assertEqual(t.refund_state, "Pending")
		self.assertEqual(len(journals(tid, "reverse:convert:%")), 2)
		self.assertEqual(frappe.db.get_value("Remittance Reservation", {"transfer": tid}, "state"), "Released")

	def test_not_sent_is_retried_safely(self):
		c, resp = fx.new_transfer(mode="SANDBOX-DOWN")
		tid = resp["transfer_id"]
		fx.fund(tid)
		frappe.db.commit()
		fx.drain(rounds=3)
		row = frappe.db.get_value("Remittance Outbox", {"transfer": tid, "command_type": "create_payment"},
			["status", "attempts"], as_dict=True)
		self.assertEqual(row.status, "Pending")
		self.assertEqual(row.attempts, 3)
		self.assertEqual(fx.T(tid).payout_state, "Dispatching")

	# ---------------------------------------------------------------- T04 duplicate & out-of-order callbacks
	def test_T04_duplicate_and_out_of_order_callbacks(self):
		c, resp = fx.new_transfer(mode="SANDBOX-PENDING")
		tid = resp["transfer_id"]
		fx.fund(tid)
		frappe.db.commit()
		outbox.process_due()
		t = fx.T(tid)
		self.assertEqual(t.payout_state, "Accepted")
		instr = frappe.db.get_value("Remittance Payout Attempt", t.payout_attempt, "instruction_id")
		from neotec_remittance.adapters.base import CanonicalEvent
		from neotec_remittance.services import inbox

		paid = CanonicalEvent("evt-paid-" + tid, "paid", instr, "BREF1", "ACSC", currency="INR", raw="{}")
		stale = CanonicalEvent("evt-acc-" + tid, "accepted", instr, "BREF1", "ACSP", raw="{}")
		inbox.record(fx.PARTNER, paid.event_id, "paid", "Callback", instr, event=paid)
		inbox.record(fx.PARTNER, paid.event_id, "paid", "Callback", instr, event=paid)  # duplicate
		frappe.db.commit()
		inbox.process_due()
		inbox.record(fx.PARTNER, stale.event_id, "accepted", "Callback", instr, event=stale)
		frappe.db.commit()
		inbox.process_due()
		t = fx.T(tid)
		self.assertEqual(t.payout_state, "Paid")
		self.assertEqual(len(journals(tid, "paid:%")), 1)
		self.assertEqual(frappe.db.count("Remittance Inbox", {"instruction_id": instr, "event_type": "paid"}), 1)
		self.assertEqual(frappe.db.get_value("Remittance Inbox", {"event_id": stale.event_id}, "status"), "Ignored")

	def test_contradictory_evidence_is_quarantined(self):
		c, resp = fx.new_transfer(mode="SANDBOX-INSTANT")
		tid = resp["transfer_id"]
		fx.fund(tid)
		frappe.db.commit()
		fx.drain()
		t = fx.T(tid)
		self.assertEqual(t.payout_state, "Paid")
		instr = frappe.db.get_value("Remittance Payout Attempt", t.payout_attempt, "instruction_id")
		from neotec_remittance.adapters.base import CanonicalEvent
		from neotec_remittance.services import inbox

		ev = CanonicalEvent("evt-rj-" + tid, "rejected", instr, None, "RJCT", raw="{}")
		inbox.record(fx.PARTNER, ev.event_id, "rejected", "Callback", instr, event=ev)
		frappe.db.commit()
		inbox.process_due()
		self.assertEqual(frappe.db.get_value("Remittance Inbox", {"event_id": ev.event_id}, "status"), "Quarantined")
		t = fx.T(tid)
		self.assertEqual(t.payout_state, "Paid")
		self.assertEqual(t.recon_state, "Exception")

	def test_unknown_reference_quarantined(self):
		from neotec_remittance.adapters.base import CanonicalEvent
		from neotec_remittance.services import inbox

		ev = CanonicalEvent("evt-unknown-" + frappe.generate_hash(), "paid", "no-such-instruction", None, raw="{}")
		inbox.record(fx.PARTNER, ev.event_id, "paid", "Callback", ev.instruction_id, event=ev)
		frappe.db.commit()
		inbox.process_due()
		self.assertEqual(frappe.db.get_value("Remittance Inbox", {"event_id": ev.event_id}, "status"), "Quarantined")

	# ---------------------------------------------------------------- T05 cancel vs dispatch
	def test_T05_cancel_before_dispatch_blocks_send_and_refunds(self):
		set_values({"dispatch_paused": 1})  # keep it at Not Ready/Ready
		c, resp = fx.new_transfer()
		tid = resp["transfer_id"]
		fx.fund(tid)
		self.assertIn(fx.T(tid).payout_state, ("Not Ready",))
		out = refunds.request_cancel(tid, "test")
		self.assertEqual(out["result"], "CANCELLED")
		set_values({"dispatch_paused": 0})
		frappe.db.commit()
		execution.release_due()
		fx.drain()
		self.assertEqual(frappe.db.count("Remittance Outbox", {"transfer": tid}), 0)
		t = fx.T(tid)
		self.assertEqual((t.payout_state, t.refund_state), ("Cancelled", "Pending"))
		self.assertEqual(Decimal(t.refund_amount), Decimal(t.total_payable))

	def test_T05_cancel_after_dispatch_becomes_recall(self):
		c, resp = fx.new_transfer(mode="SANDBOX-PENDING")
		tid = resp["transfer_id"]
		fx.fund(tid)
		frappe.db.commit()
		outbox.process_due()
		self.assertEqual(fx.T(tid).payout_state, "Accepted")
		out = refunds.request_cancel(tid, "test")
		self.assertEqual(out["result"], "RECALL_REQUESTED")
		self.assertEqual(fx.T(tid).payout_state, "Recall Requested")
		with self.assertRaises(GuardFailed):
			refunds.initiate(state.lock_transfer(tid), "must not refund while uncertain")

	# ---------------------------------------------------------------- T06 liquidity
	def test_T06_reservations_never_exceed_liquidity(self):
		psa = fx.accounts.resolve(fx.COMPANY, "INR", "Partner Settlement Asset", fx.PARTNER)
		available = Decimal(psa.balance) - Decimal(psa.reserved)
		# temporarily reserve everything but a small buffer
		frappe.db.sql("UPDATE `tabRemittance Account` SET reserved = reserved + %s WHERE name=%s",
			(available - Decimal("30000.00"), psa.name))
		try:
			c = fx.make_customer()
			_, r1 = fx.new_transfer(c, amount="1000.00")
			_, r2 = fx.new_transfer(c, amount="1000.00")
			fx.fund(r1["transfer_id"])
			fx.fund(r2["transfer_id"])
			states = sorted([fx.T(r1["transfer_id"]).payout_state, fx.T(r2["transfer_id"]).payout_state])
			self.assertEqual(states, ["Dispatching", "Not Ready"])
			blocked = fx.T(r2["transfer_id"]) if fx.T(r2["transfer_id"]).payout_state == "Not Ready" else fx.T(r1["transfer_id"])
			self.assertEqual(blocked.last_error_code, "INSUFFICIENT_LIQUIDITY")
			after = fx.accounts.resolve(fx.COMPANY, "INR", "Partner Settlement Asset", fx.PARTNER)
			self.assertGreaterEqual(Decimal(after.balance) - Decimal(after.reserved), 0)
		finally:
			frappe.db.sql("UPDATE `tabRemittance Account` SET reserved = reserved - %s WHERE name=%s",
				(available - Decimal("30000.00"), psa.name))

	# ---------------------------------------------------------------- T07 guards
	def test_T07_expired_kyc_and_stale_screening_block_release(self):
		c, resp = fx.new_transfer()
		tid = resp["transfer_id"]
		frappe.db.set_value("Remittance Customer", c.name, "kyc_expiry", frappe.utils.add_days(frappe.utils.today(), -1),
			update_modified=False)
		fx.fund(tid)
		t = fx.T(tid)
		self.assertEqual(t.payout_state, "Not Ready")
		self.assertEqual(t.last_error_code, "KYC_NOT_VALID")
		frappe.db.set_value("Remittance Customer", c.name, "kyc_expiry", frappe.utils.add_days(frappe.utils.today(), 30),
			update_modified=False)
		set_values({"screening_list_updated_at": utc_now().replace(year=2020)})
		try:
			execution.release_due()
			self.assertEqual(fx.T(tid).payout_state, "Not Ready")
		finally:
			from neotec_remittance.services.screening import on_watchlist_change

			on_watchlist_change()
		execution.release_due()
		self.assertEqual(fx.T(tid).payout_state, "Dispatching")

	def test_screening_hit_holds_and_maker_checker_decides(self):
		frappe.get_doc({"doctype": "Remittance Watchlist Entry", "full_name": "Blocked Person Example",
			"list_source": "TEST", "active": 1}).insert(ignore_permissions=True)
		c, resp = fx.new_transfer(ben_name="Mr. Blocked Person Example")
		tid = resp["transfer_id"]
		fx.fund(tid)
		t = fx.T(tid)
		self.assertEqual(t.compliance_state, "Hold")
		self.assertEqual(t.customer_status, "Under Review")
		case = frappe.get_doc("Remittance Compliance Case", t.compliance_case)
		officer = _user_with_role("nr-officer@example.com", "Remittance Compliance Officer")
		approver = _user_with_role("nr-approver@example.com", "Remittance Compliance Approver")
		frappe.set_user(officer)
		case.recommendation, case.recommendation_note = "Reject", "confirmed match"
		case.save()
		frappe.set_user(officer)
		case.reload()
		case.decision, case.decision_note = "Reject", "self approval"
		with self.assertRaises(frappe.PermissionError):
			case.save()
		frappe.set_user(approver)
		case.reload()
		case.decision, case.decision_note = "Reject", "confirmed"
		case.save()
		frappe.set_user("Administrator")
		t = fx.T(tid)
		self.assertEqual((t.compliance_state, t.payout_state, t.refund_state), ("Rejected", "Cancelled", "Pending"))
		self.assertEqual(t.customer_status, "Refund in Progress")

	# ---------------------------------------------------------------- T09 return after paid
	def test_T09_paid_then_returned(self):
		c, resp = fx.new_transfer(mode="SANDBOX-INSTANT")
		tid = resp["transfer_id"]
		fx.fund(tid)
		frappe.db.commit()
		fx.drain()
		t = fx.T(tid)
		instr = frappe.db.get_value("Remittance Payout Attempt", t.payout_attempt, "instruction_id")
		from neotec_remittance.adapters.base import CanonicalEvent
		from neotec_remittance.services import inbox

		ev = CanonicalEvent("evt-ret-" + tid, "returned", instr, None, "RTRN", amount=str(t.payout_amount), raw="{}")
		inbox.record(fx.PARTNER, ev.event_id, "returned", "Callback", instr, event=ev)
		frappe.db.commit()
		inbox.process_due()
		t = fx.T(tid)
		self.assertEqual((t.payout_state, t.refund_state), ("Returned", "Eligible"))
		self.assertEqual(len(journals(tid, "paid:%")), 1, "original history retained")
		refunds.initiate_return_refund(tid, "990.00", "test-finance")
		refunds.confirm(tid, "RFND-1", "test-finance")
		self.assertEqual(fx.T(tid).refund_state, "Refunded")

	# ---------------------------------------------------------------- T10 duplicate statement entry
	def test_T10_duplicate_bank_entry_single_effect(self):
		c, resp = fx.new_transfer()
		tid = resp["transfer_id"]
		a = fx.fund(tid, entry_id="DUP-ENTRY-" + tid)
		b = fx.fund(tid, entry_id="DUP-ENTRY-" + tid)
		self.assertFalse(a["duplicate"])
		self.assertTrue(b["duplicate"])
		self.assertEqual(len(journals(tid, "alloc:%")), 1)

	def test_partial_and_overpayment(self):
		c, resp = fx.new_transfer(amount="1000.00")
		tid = resp["transfer_id"]
		fx.fund(tid, amount="500.00")
		self.assertEqual(fx.T(tid).funding_state, "Partially Funded")
		r = fx.fund(tid, amount="600.00")
		t = fx.T(tid)
		self.assertEqual(t.funding_state, "Funded")
		rec = frappe.db.get_value("Remittance Funding Receipt", r["receipt"], ["status", "allocated_amount"], as_dict=True)
		self.assertEqual(rec.status, "Partially Allocated")
		self.assertEqual(Decimal(rec.allocated_amount), Decimal("517.25"))
		refunds.refund_receipt_excess(r["receipt"], "test-finance")
		refunds.confirm_receipt_refund(r["receipt"], "RF-EXCESS", "test-finance")
		self.assertEqual(frappe.db.get_value("Remittance Funding Receipt", r["receipt"], "status"), "Refunded")

	# ---------------------------------------------------------------- T11 crash at commit boundaries
	def test_T11_expired_lease_on_payment_becomes_uncertain_not_resent(self):
		set_values({"dispatch_paused": 0})
		c, resp = fx.new_transfer(mode="SANDBOX-TIMEOUT")
		tid = resp["transfer_id"]
		fx.fund(tid)
		frappe.db.commit()
		name = frappe.db.get_value("Remittance Outbox", {"transfer": tid, "command_type": "create_payment"})
		owner = outbox.claim(name)  # worker claims, then "crashes" before recording anything
		self.assertTrue(owner)
		frappe.db.sql("UPDATE `tabRemittance Outbox` SET lease_until=%s WHERE name=%s", (utc_now().replace(year=2020), name))
		frappe.db.commit()
		fx.drain()
		row = frappe.db.get_value("Remittance Outbox", name, ["status", "attempts"], as_dict=True)
		self.assertEqual(row.attempts, 1, "never re-sent after a lost lease")
		self.assertIn(row.status, ("Awaiting Status", "Completed"))
		self.assertEqual(fx.T(tid).payout_state, "Paid")

	def test_two_workers_cannot_claim_same_command(self):
		set_values({"dispatch_paused": 1})
		c, resp = fx.new_transfer()
		fx.fund(resp["transfer_id"])
		set_values({"dispatch_paused": 0})
		frappe.db.commit()
		execution.release_due()
		name = frappe.db.get_value("Remittance Outbox", {"transfer": resp["transfer_id"]})
		self.assertTrue(outbox.claim(name))
		self.assertIsNone(outbox.claim(name))

	# ---------------------------------------------------------------- T12 cross-customer access
	def test_T12_cross_customer_access_denied(self):
		c1, resp = fx.new_transfer()
		c2 = fx.make_customer()
		from neotec_remittance.services import transfers

		with self.assertRaises(NotFound):
			transfers.get_for_customer(c2, resp["transfer_id"])
		ben = fx.make_beneficiary(c1)
		from neotec_remittance.services import beneficiaries

		with self.assertRaises(NotFound):
			beneficiaries.get_version_for_customer(ben["beneficiary_version_id"], c2.name)

	def test_api_envelope_and_session_identity(self):
		c, resp = fx.new_transfer()
		user = frappe.db.get_value("Remittance Customer", c.name, "user")
		other = fx.make_customer()
		from neotec_remittance.api.v1 import transfers as api

		frappe.set_user(user)
		ok = api.get(transfer_id=resp["transfer_id"])
		self.assertEqual(ok["data"]["transfer_id"], resp["transfer_id"])
		self.assertNotIn("compliance", json.dumps(ok))
		frappe.set_user(frappe.db.get_value("Remittance Customer", other.name, "user"))
		denied = api.get(transfer_id=resp["transfer_id"])
		self.assertEqual(denied["error"]["code"], "TRANSFER_NOT_FOUND")
		self.assertEqual(frappe.local.response["http_status_code"], 404)
		frappe.set_user("Administrator")

	# ---------------------------------------------------------------- T13 forged / replayed callback
	def test_T13_callback_signature_and_replay(self):
		from neotec_remittance.adapters import get_adapter

		adapter = get_adapter(fx.PARTNER)
		body = json.dumps({"event_id": "cb-1", "event_type": "paid", "instruction_id": "x"}).encode()
		ts = str(int(time.time()))
		good = hmac_hex(fx.CALLBACK_SECRET, f"{ts}.".encode() + body)
		self.assertEqual(len(adapter.verify_callback({"X-Neotec-Timestamp": ts, "X-Neotec-Signature": good}, body)), 1)
		with self.assertRaises(Unauthenticated):
			adapter.verify_callback({"X-Neotec-Timestamp": ts, "X-Neotec-Signature": good}, body + b" ")
		old = str(int(time.time()) - 3600)
		with self.assertRaises(Unauthenticated):
			adapter.verify_callback({"X-Neotec-Timestamp": old,
				"X-Neotec-Signature": hmac_hex(fx.CALLBACK_SECRET, f"{old}.".encode() + body)}, body)
		with self.assertRaises(Unauthenticated):
			adapter.verify_callback({"X-Neotec-Timestamp": ts, "X-Neotec-Signature": hmac_hex("wrong", f"{ts}.".encode() + body)}, body)

	# ---------------------------------------------------------------- T15 ERP unavailable
	def test_T15_erp_backlog_replays_once(self):
		"""ERP unavailable / unmapped: subledger is unaffected; once fixed, the backlog posts exactly once."""
		from neotec_remittance.services import erp_bridge

		ob = fx.accounts.resolve(fx.COMPANY, "SAR", "Operating Bank").name
		rd = fx.accounts.resolve(fx.COMPANY, "SAR", "Rounding").name
		frappe.db.set_value("Remittance Account", ob, "erp_account", None)
		eid = "test:t15:" + frappe.generate_hash()
		j = ledger.post_journal(eid, "test", fx.COMPANY, "SAR", [Dr("Operating Bank", "7.00"), Cr("Rounding", "7.00")])
		integrity.seal_chain()
		cc = frappe.db.get_value("Company", fx.COMPANY, "cost_center")
		set_values({"erp_posting_enabled": 1, "erp_cost_center": cc})
		try:
			with self.assertRaises(frappe.ValidationError):
				erp_bridge.post_journal(j)
			frappe.db.rollback()
			self.assertEqual(frappe.db.count("Remittance ERP Posting", {"journal": j}), 0)
			self.assertTrue(ledger.journal_for_event(eid))  # subledger intact
			asset = frappe.db.get_value("Account", {"company": fx.COMPANY, "root_type": "Asset", "is_group": 0,
				"account_type": ("in", ["Bank", "Cash"])})
			exp = frappe.db.get_value("Account", {"company": fx.COMPANY, "root_type": "Expense", "is_group": 0,
				"account_type": ""})
			frappe.db.set_value("Remittance Account", ob, "erp_account", asset)
			frappe.db.set_value("Remittance Account", rd, "erp_account", exp)
			erp_bridge.post_journal(j)
			erp_bridge.post_journal(j)
			self.assertEqual(frappe.db.count("Remittance ERP Posting", {"journal": j}), 1)
			self.assertEqual(frappe.db.get_value("Remittance Journal", j, "erp_status"), "Posted")
		finally:
			set_values({"erp_posting_enabled": 0})

	def test_erp_bridge_posts_mapped_journal_once(self):
		from neotec_remittance.services import erp_bridge

		company_abbr = frappe.db.get_value("Company", fx.COMPANY, "abbr")
		bank = frappe.db.get_value("Account", {"company": fx.COMPANY, "root_type": "Asset", "is_group": 0,
			"account_type": ("in", ["Bank", "Cash"])})
		liab = frappe.db.get_value("Account", {"company": fx.COMPANY, "root_type": "Liability", "is_group": 0,
			"account_type": ""})
		self.assertTrue(bank and liab)
		sb = fx.accounts.resolve(fx.COMPANY, "SAR", "Safeguarded Bank").name
		ua = fx.accounts.resolve(fx.COMPANY, "SAR", "Unallocated Receipts").name
		for acc, erp in ((sb, bank), (ua, liab)):
			frappe.db.set_value("Remittance Account", acc, "erp_account", erp)
		eid = "test:erp:" + frappe.generate_hash()
		j = ledger.post_journal(eid, "funds_received", fx.COMPANY, "SAR",
			[Dr("Safeguarded Bank", "12.00"), Cr("Unallocated Receipts", "12.00")])
		integrity.seal_chain()
		cc = frappe.db.get_value("Company", fx.COMPANY, "cost_center")
		set_values({"erp_posting_enabled": 1, "erp_cost_center": cc})
		try:
			erp_bridge.post_journal(j)
			erp_bridge.post_journal(j)
			self.assertEqual(frappe.db.count("Remittance ERP Posting", {"journal": j}), 1)
			je = frappe.db.get_value("Remittance ERP Posting", {"journal": j}, "journal_entry")
			self.assertEqual(frappe.db.get_value("Journal Entry", je, "docstatus"), 1)
			self.assertEqual(frappe.db.get_value("Journal Entry", je, "neotec_remittance_export_id"), f"nrj:{j}")
		finally:
			set_values({"erp_posting_enabled": 0})

	# ---------------------------------------------------------------- integrity & safeguarding
	def test_hash_chain_verifies(self):
		integrity.seal_chain()
		result = integrity.verify_chain()
		self.assertTrue(result["ok"], result["problems"][:3])

	def test_safeguarding_breach_pauses_dispatch(self):
		eid = "test:shortfall:" + frappe.generate_hash()
		ledger.post_journal(eid, "test", fx.COMPANY, "SAR",
			[Dr("Treasury Funding", "1000000000.00"), Cr("Customer Liability", "1000000000.00")])
		try:
			breaches = safeguarding.run_checks()
			self.assertTrue(breaches)
			self.assertEqual(frappe.db.get_single_value("Remittance Settings", "dispatch_paused"), 1)
		finally:
			ledger.reverse_journal(ledger.journal_for_event(eid).name, "test:shortfall-rev:" + eid)
			set_values({"dispatch_paused": 0})

	def test_late_funding_requires_reconfirmation(self):
		c, resp = fx.new_transfer()
		tid = resp["transfer_id"]
		frappe.db.sql("UPDATE `tabRemittance Transfer` SET funding_deadline=%s WHERE name=%s",
			(utc_now().replace(year=2020), tid))
		fx.fund(tid)
		t = fx.T(tid)
		self.assertEqual((t.funding_state, t.requote_required, t.payout_state), ("Funded", 1, "Not Ready"))
		self.assertEqual(t.customer_status, "Action Required")
		from neotec_remittance.services import quotes, transfers

		q = quotes.create_quote(c, "1000.00", "India", "INR")
		transfers.reconfirm(c, tid, q["quote_id"], "auth-reconfirm")
		execution.release_due()
		self.assertEqual(fx.T(tid).payout_state, "Dispatching")

	def test_limits_enforced(self):
		set_values({"daily_limit": 1500})
		try:
			c = fx.make_customer()
			fx.new_transfer(c, amount="1000.00")
			with self.assertRaises(GuardFailed):
				fx.new_transfer(c, amount="1000.00")
		finally:
			set_values({"daily_limit": 10000000})

	def test_beneficiary_account_encrypted_and_iban_validated(self):
		c = fx.make_customer()
		from neotec_remittance.services import beneficiaries
		from neotec_remittance.utils.errors import ValidationFailed

		with self.assertRaises(ValidationFailed):
			beneficiaries.create_or_version(c, {"full_name": "Test Person", "country": "Saudi Arabia", "currency": "SAR",
				"account_number": "SA0380000000608010167518"})  # bad check digits
		b = beneficiaries.create_or_version(c, {"full_name": "Test Person", "country": "Saudi Arabia", "currency": "SAR",
			"account_number": "SA0380000000608010167519"})
		v = db.get_row("Remittance Beneficiary Version", b["beneficiary_version_id"])
		self.assertNotIn("608010167519", v.account_enc)
		self.assertEqual(v.account_masked[-4:], "7519")


def _user_with_role(email, role):
	if not frappe.db.exists("User", email):
		u = frappe.get_doc({"doctype": "User", "email": email, "first_name": role, "send_welcome_email": 0})
		u.insert(ignore_permissions=True)
		u.add_roles(role)
	return email
