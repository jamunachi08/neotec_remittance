"""Deterministic sandbox partner for development, UAT and the automated test suite.
Behaviour is selected by the beneficiary bank code:
  SANDBOX-PAID (default)   create -> ACCEPTED, status -> PAID
  SANDBOX-INSTANT          create -> PAID
  SANDBOX-REJECT           create -> REJECTED (certified non-execution)
  SANDBOX-TIMEOUT          create -> UNCERTAIN (but the bank executed), status -> PAID
  SANDBOX-NOEXEC           create -> UNCERTAIN (never executed), status -> NOT_FOUND
  SANDBOX-DOWN             create -> NOT_SENT (connection refused)
  SANDBOX-PENDING          create -> ACCEPTED, status -> PENDING
  SANDBOX-CHARGES          create -> ACCEPTED, status -> PAID with 5.00 partner charges"""
from neotec_remittance.adapters.base import (ACCEPTED, NOT_FOUND, NOT_SENT, PAID, PENDING, REJECTED, UNCERTAIN,
	AdapterResult, BankAdapter)


def _mode(code):
	return (code or "SANDBOX-PAID").upper()


class SandboxAdapter(BankAdapter):
	mapping_version = "sandbox-1"

	def create_payment(self, request):
		mode = _mode(request.get("beneficiary", {}).get("bank_code"))
		ref = "SBX" + request["instruction_id"][:12].upper()
		if mode == "SANDBOX-INSTANT":
			return AdapterResult(PAID, ref, "SETTLED")
		if mode == "SANDBOX-REJECT":
			return AdapterResult(REJECTED, ref, "RJCT", reason="AC01 Incorrect account")
		if mode in ("SANDBOX-TIMEOUT", "SANDBOX-NOEXEC"):
			return AdapterResult(UNCERTAIN, None, None, reason="read timeout")
		if mode == "SANDBOX-DOWN":
			return AdapterResult(NOT_SENT, None, None, reason="connection refused")
		return AdapterResult(ACCEPTED, ref, "ACSP")

	def get_payment_status(self, instruction_id, bank_reference):
		mode = self._mode_for(instruction_id)
		ref = bank_reference or "SBX" + instruction_id[:12].upper()
		if mode == "SANDBOX-NOEXEC":
			return AdapterResult(NOT_FOUND, None, "NOTFOUND")
		if mode == "SANDBOX-PENDING":
			return AdapterResult(PENDING, ref, "ACSP")
		if mode == "SANDBOX-REJECT":
			return AdapterResult(REJECTED, ref, "RJCT")
		if mode == "SANDBOX-CHARGES":
			return AdapterResult(PAID, ref, "ACSC", charges_amount="5.00")
		return AdapterResult(PAID, ref, "ACSC")

	def recall_payment(self, instruction_id, bank_reference, reason):
		return AdapterResult(ACCEPTED, bank_reference, "RECALL_RECEIVED")

	def get_balance(self, account_ref=None):
		return None

	def _mode_for(self, instruction_id):
		import frappe

		bv = frappe.db.sql("""SELECT v.bank_code FROM `tabRemittance Payout Attempt` a
			JOIN `tabRemittance Transfer` t ON t.name=a.transfer
			JOIN `tabRemittance Beneficiary Version` v ON v.name=t.beneficiary_version
			WHERE a.instruction_id=%s""", instruction_id)
		return _mode(bv[0][0] if bv else None)
