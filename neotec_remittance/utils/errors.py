"""Typed domain errors. Each carries a stable machine code and an HTTP status for the API layer."""
import frappe


class RemittanceError(frappe.ValidationError):
	code = "REMITTANCE_ERROR"
	http_status = 422

	def __init__(self, message=None, code=None, http_status=None, details=None):
		super().__init__(message or self.code)
		if code:
			self.code = code
		if http_status:
			self.http_status = http_status
		self.details = details or {}


class ValidationFailed(RemittanceError):
	code = "VALIDATION_FAILED"
	http_status = 422


class NotFound(RemittanceError):
	code = "NOT_FOUND"
	http_status = 404


class AccessDenied(RemittanceError):
	code = "ACCESS_DENIED"
	http_status = 403


class Unauthenticated(RemittanceError):
	code = "UNAUTHENTICATED"
	http_status = 401


class IdempotencyConflict(RemittanceError):
	code = "IDEMPOTENCY_CONFLICT"
	http_status = 409


class ConcurrencyConflict(RemittanceError):
	code = "CONCURRENCY_CONFLICT"
	http_status = 409


class InvalidTransition(RemittanceError):
	code = "INVALID_TRANSITION"
	http_status = 409


class GuardFailed(RemittanceError):
	code = "GUARD_FAILED"
	http_status = 409


class LedgerError(RemittanceError):
	code = "LEDGER_ERROR"
	http_status = 500


class ConfigurationError(RemittanceError):
	code = "CONFIGURATION_ERROR"
	http_status = 503
