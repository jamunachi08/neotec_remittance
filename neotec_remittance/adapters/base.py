"""Bank adapter contract. Every adapter translates between the bank-independent canonical model and one
partner's protocol, and declares its capabilities on the Remittance Partner record.

Outcome semantics matter more than transport:
- NOT_SENT: the adapter can prove the instruction never left (e.g. TCP connect refused). Safe to retry.
- UNCERTAIN: anything else that failed (timeouts, 5xx, broken connections, unexpected errors). Never a
  basis for resubmission unless the bank guarantees duplicate prevention on the same key."""
import json
import time
from dataclasses import asdict, dataclass, field

import frappe

from neotec_remittance.utils.errors import Unauthenticated
from neotec_remittance.utils.hashing import hmac_hex, safe_equals, sha256_hex

ACCEPTED, PAID, REJECTED, PENDING, NOT_SENT, UNCERTAIN, NOT_FOUND = (
	"ACCEPTED", "PAID", "REJECTED", "PENDING", "NOT_SENT", "UNCERTAIN", "NOT_FOUND")
EVENT_TYPES = {"accepted", "paid", "rejected", "returned", "recalled", "recall_refused", "refund_completed",
	"submission_uncertain", "not_found_final"}


@dataclass
class AdapterResult:
	outcome: str
	bank_reference: str | None = None
	raw_status: str | None = None
	reason: str | None = None
	delivered_amount: str | None = None
	charges_amount: str | None = None
	raw: dict = field(default_factory=dict)

	def as_dict(self):
		return asdict(self)


@dataclass
class CanonicalEvent:
	event_id: str
	event_type: str
	instruction_id: str | None = None
	bank_reference: str | None = None
	raw_status: str | None = None
	occurred_at: str | None = None
	currency: str | None = None
	amount: str | None = None
	charges_amount: str | None = None
	source_sequence: str | None = None
	raw: str = ""

	@property
	def evidence_hash(self):
		return sha256_hex(self.raw or "")


OUTCOME_TO_EVENT = {ACCEPTED: "accepted", PENDING: "accepted", PAID: "paid", REJECTED: "rejected"}


class BankAdapter:
	mapping_version = "base-1"

	def __init__(self, partner):
		self.partner = partner

	# ---- outbound ------------------------------------------------------------------------------
	def create_payment(self, request: dict) -> AdapterResult:
		raise NotImplementedError

	def get_payment_status(self, instruction_id: str, bank_reference: str | None) -> AdapterResult:
		raise NotImplementedError

	def recall_payment(self, instruction_id: str, bank_reference: str | None, reason: str) -> AdapterResult:
		raise NotImplementedError

	def create_refund(self, request: dict) -> AdapterResult:
		raise NotImplementedError

	def get_balance(self, account_ref: str | None = None) -> dict | None:
		return None

	# ---- inbound -------------------------------------------------------------------------------
	def verify_callback(self, headers: dict, raw_body: bytes) -> list[CanonicalEvent]:
		"""Default scheme: X-Neotec-Timestamp + X-Neotec-Signature = HMAC-SHA256(secret, f"{ts}.{raw}")."""
		secret = self.partner.get_password("callback_secret", raise_exception=False)
		if not secret:
			raise Unauthenticated("Callback secret not configured", code="CALLBACK_NOT_CONFIGURED")
		ts = headers.get("X-Neotec-Timestamp") or headers.get("x-neotec-timestamp")
		sig = headers.get("X-Neotec-Signature") or headers.get("x-neotec-signature")
		if not ts or not sig:
			raise Unauthenticated("Missing signature headers", code="CALLBACK_SIGNATURE_MISSING")
		try:
			ts_i = int(ts)
		except ValueError:
			raise Unauthenticated("Bad timestamp", code="CALLBACK_TIMESTAMP_INVALID")
		window = self.partner.callback_replay_window_seconds or 300
		if abs(time.time() - ts_i) > window:
			raise Unauthenticated("Callback outside replay window", code="CALLBACK_REPLAYED_OR_STALE")
		expected = hmac_hex(secret, f"{ts}.".encode() + raw_body)
		if not safe_equals(expected, sig):
			raise Unauthenticated("Signature mismatch", code="CALLBACK_SIGNATURE_INVALID")
		return self.parse_events(raw_body)

	def parse_events(self, raw_body: bytes) -> list[CanonicalEvent]:
		data = json.loads(raw_body or b"{}")
		items = data.get("events") if isinstance(data, dict) and "events" in data else [data]
		events = []
		for item in items:
			et = (item.get("event_type") or "").lower()
			if et not in EVENT_TYPES or not item.get("event_id"):
				frappe.throw(f"Unsupported or incomplete event: {item.get('event_type')!r}")
			events.append(CanonicalEvent(event_id=str(item["event_id"]), event_type=et,
				instruction_id=item.get("instruction_id"), bank_reference=item.get("bank_reference"),
				raw_status=item.get("raw_status"), occurred_at=item.get("occurred_at"), currency=item.get("currency"),
				amount=item.get("amount"), charges_amount=item.get("charges_amount"),
				source_sequence=str(item.get("source_sequence") or "") or None,
				raw=json.dumps(item, sort_keys=True)))
		return events
