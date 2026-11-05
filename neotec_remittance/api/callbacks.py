"""POST /callbacks/{partner}: signature verified over the exact raw bytes, then each event is committed to
the durable inbox before 200 is returned. Business effects are applied asynchronously."""
import frappe

from neotec_remittance.adapters import get_adapter
from neotec_remittance.services import inbox
from neotec_remittance.utils.errors import RemittanceError


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive(partner=None):
	# The partner is taken from the URL only, never from the (untrusted) body.
	partner = frappe.request.args.get("partner")
	try:
		if not partner or not frappe.db.get_value("Remittance Partner", partner, "enabled"):
			raise RemittanceError("Unknown partner", code="UNKNOWN_PARTNER", http_status=404)
		adapter = get_adapter(partner)
		raw = frappe.request.get_data(cache=True) or b""
		events = adapter.verify_callback(dict(frappe.request.headers), raw)
		stored = []
		for ev in events:
			name, created = inbox.record(partner, ev.event_id, ev.event_type, "Callback", ev.instruction_id, event=ev)
			stored.append({"event_id": ev.event_id, "duplicate": not created})
		frappe.db.commit()
		frappe.local.response["http_status_code"] = 200
		return {"accepted": stored}
	except RemittanceError as e:
		frappe.db.rollback()
		frappe.local.response["http_status_code"] = e.http_status
		return {"error": {"code": e.code}}
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title=f"Neotec Remittance: callback rejected ({partner})")
		frappe.local.response["http_status_code"] = 400
		return {"error": {"code": "CALLBACK_REJECTED"}}
