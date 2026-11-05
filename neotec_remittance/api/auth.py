"""Customer identity for the public API.

Gateway Assertion (recommended): the API gateway authenticates the customer (OIDC, device binding, step-up)
and forwards the request over a private link with
  X-Neotec-Subject: <subject id>, X-Neotec-Timestamp: <unix seconds>,
  X-Neotec-Assertion: hex HMAC-SHA256(shared secret, f"{ts}.{subject}.{METHOD}.{path}")
Frappe Session: the logged-in Website User linked to a Remittance Customer.
The customer and legal entity always come from authenticated context, never from request fields."""
import time

import frappe

from neotec_remittance.services.settings import get_settings
from neotec_remittance.utils import db
from neotec_remittance.utils.errors import Unauthenticated
from neotec_remittance.utils.hashing import hmac_hex, safe_equals

CUSTOMER_FIELDS = "name, entity, status, kyc_status, kyc_expiry, customer_name"


def current_customer():
	s = get_settings()
	if s.identity_mode == "Gateway Assertion":
		subject = _verify_assertion(s)
		c = db.find_row("Remittance Customer", {"subject_id": subject}, CUSTOMER_FIELDS)
	else:
		user = frappe.session.user
		if user in (None, "Guest"):
			raise Unauthenticated("Login required")
		c = db.find_row("Remittance Customer", {"user": user}, CUSTOMER_FIELDS)
	if not c:
		raise Unauthenticated("No remittance customer for this identity", code="CUSTOMER_UNKNOWN")
	return c


def _verify_assertion(s) -> str:
	h = frappe.request.headers
	subject, ts, sig = h.get("X-Neotec-Subject"), h.get("X-Neotec-Timestamp"), h.get("X-Neotec-Assertion")
	if not (subject and ts and sig):
		raise Unauthenticated("Gateway assertion missing")
	try:
		if abs(time.time() - int(ts)) > (s.gateway_max_skew_seconds or 120):
			raise Unauthenticated("Gateway assertion expired")
	except ValueError:
		raise Unauthenticated("Gateway assertion invalid")
	secret = s.get_password("gateway_shared_secret", raise_exception=False)
	if not secret:
		raise Unauthenticated("Gateway assertion not configured", code="GATEWAY_NOT_CONFIGURED")
	expected = hmac_hex(secret, f"{ts}.{subject}.{frappe.request.method}.{frappe.request.path}")
	if not safe_equals(expected, sig):
		raise Unauthenticated("Gateway assertion invalid")
	return subject
