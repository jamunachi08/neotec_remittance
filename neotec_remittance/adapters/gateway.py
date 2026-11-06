"""Adapter for the separately hosted Neotec Bank Gateway.

Recommended production topology: the gateway runs in KSA on infrastructure that holds bank mTLS keys
(KMS/HSM) and a static egress IP; Frappe never holds bank credentials. Commands are signed with HMAC.
Gateway response body: {"outcome": "ACCEPTED|PAID|REJECTED|PENDING|NOT_FOUND", "bank_reference": ..., ...}"""
import json
import time

import requests

from neotec_remittance.adapters.base import NOT_SENT, UNCERTAIN, AdapterResult, BankAdapter
from neotec_remittance.utils.hashing import hmac_hex

VALID = {"ACCEPTED", "PAID", "REJECTED", "PENDING", "NOT_FOUND"}


class GatewayAdapter(BankAdapter):
	mapping_version = "gateway-1"
	timeout = (5, 25)

	def _post(self, path, body) -> AdapterResult:
		url = (self.partner.gateway_url or "").rstrip("/") + path
		raw = json.dumps(body, sort_keys=True, separators=(",", ":"))
		ts = str(int(time.time()))
		secret = self.partner.get_password("gateway_secret")
		headers = {"Content-Type": "application/json", "X-Neotec-Timestamp": ts,
			"X-Neotec-Signature": hmac_hex(secret, f"{ts}.{raw}"), "Idempotency-Key": body.get("instruction_id", "")}
		try:
			resp = requests.post(url, data=raw, headers=headers, timeout=self.timeout)
		except requests.exceptions.ConnectTimeout:
			return AdapterResult(NOT_SENT, reason="connect timeout")
		except requests.exceptions.ConnectionError as e:
			if "refused" in str(e).lower() or "name or service not known" in str(e).lower():
				return AdapterResult(NOT_SENT, reason="connection refused")
			return AdapterResult(UNCERTAIN, reason="connection error")
		except requests.exceptions.RequestException:
			return AdapterResult(UNCERTAIN, reason="transport error")
		if resp.status_code >= 500 or resp.status_code in (408, 429):
			return AdapterResult(UNCERTAIN, raw_status=str(resp.status_code), reason="gateway unavailable")
		try:
			data = resp.json()
		except ValueError:
			return AdapterResult(UNCERTAIN, raw_status=str(resp.status_code), reason="unparseable response")
		outcome = (data.get("outcome") or "").upper()
		if outcome not in VALID:
			return AdapterResult(UNCERTAIN, raw_status=str(resp.status_code), reason="unknown outcome")
		return AdapterResult(outcome, data.get("bank_reference"), data.get("raw_status"), data.get("reason"),
			data.get("delivered_amount"), data.get("charges_amount"), raw=data)

	def create_payment(self, request):
		return self._post("/v1/payments", request)

	def get_payment_status(self, instruction_id, bank_reference):
		return self._post("/v1/payments/status", {"instruction_id": instruction_id, "bank_reference": bank_reference})

	def recall_payment(self, instruction_id, bank_reference, reason):
		return self._post("/v1/payments/recall", {"instruction_id": instruction_id, "bank_reference": bank_reference,
			"reason": reason})

	def create_refund(self, request):
		return self._post("/v1/refunds", request)
