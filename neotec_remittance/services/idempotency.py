"""Durable API idempotency. Records are permanent for the transaction lifecycle (no cache TTL):
an expired cache can never permit a second monetary action.

Concurrency: the unique scope_key insert blocks a concurrent duplicate until the first transaction
commits; the loser then reads the stored response."""
import json

import frappe

from neotec_remittance.utils import db
from neotec_remittance.utils.errors import IdempotencyConflict, ValidationFailed
from neotec_remittance.utils.hashing import digest, sha256_hex


class Idem:
	def __init__(self, scope_key, request_hash, name=None, replay=None):
		self.scope_key, self.request_hash, self.name, self.replay = scope_key, request_hash, name, replay


def begin(entity: str, principal: str, operation: str, key: str | None, body: dict) -> Idem:
	if not key or len(key) < 8 or len(key) > 128:
		raise ValidationFailed("Idempotency-Key header (8-128 chars) is required", code="IDEMPOTENCY_KEY_REQUIRED")
	scope_key = sha256_hex(f"{entity}|{principal}|{operation}|{key}")
	request_hash = digest(body)
	existing = db.find_row("Remittance Idempotency Record", {"scope_key": scope_key}, "name, request_hash, response_json, http_status")
	if existing:
		return _replay_or_conflict(existing, scope_key, request_hash)
	db.savepoint("nr_idem")
	try:
		name = db.insert_row("Remittance Idempotency Record", {"scope_key": scope_key, "operation": operation,
			"principal": principal, "entity": entity, "request_hash": request_hash})
	except Exception as e:
		if not db.is_duplicate(e):
			raise
		db.rollback_to("nr_idem")
		existing = db.find_row("Remittance Idempotency Record", {"scope_key": scope_key},
			"name, request_hash, response_json, http_status")
		return _replay_or_conflict(existing, scope_key, request_hash)
	return Idem(scope_key, request_hash, name=name)


def _replay_or_conflict(existing, scope_key, request_hash):
	if existing.request_hash != request_hash:
		raise IdempotencyConflict("Idempotency-Key was already used with a different request")
	if not existing.response_json:
		raise IdempotencyConflict("Original request with this Idempotency-Key is still in progress",
			code="IDEMPOTENCY_IN_PROGRESS")
	return Idem(scope_key, request_hash, name=existing.name, replay=json.loads(existing.response_json))


def complete(idem: Idem, response: dict, resource: str | None = None, http_status: int = 201):
	db.update_row("Remittance Idempotency Record", idem.name, {"response_json": frappe.as_json(response),
		"resource": resource, "http_status": http_status})
