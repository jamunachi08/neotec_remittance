"""Uniform API envelope: {"data": ...} on success, {"error": {"code", "message"}} on failure, with HTTP status.
Unexpected errors are logged and returned without internal detail."""
import functools
import inspect

import frappe

from neotec_remittance.utils.errors import RemittanceError


def endpoint(fn):
	accepted = set(inspect.signature(fn).parameters)

	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		kwargs = {k: v for k, v in kwargs.items() if k in accepted}
		try:
			result = fn(*args, **kwargs)
			status = 200
			if isinstance(result, tuple):
				result, status = result
			frappe.local.response["http_status_code"] = status
			return {"data": result}
		except RemittanceError as e:
			frappe.db.rollback()
			frappe.local.response["http_status_code"] = e.http_status
			frappe.clear_messages()
			return {"error": {"code": e.code, "message": str(e), **({"details": e.details} if e.details else {})}}
		except (frappe.PermissionError, frappe.AuthenticationError):
			frappe.db.rollback()
			frappe.local.response["http_status_code"] = 403
			frappe.clear_messages()
			return {"error": {"code": "ACCESS_DENIED", "message": "Access denied"}}
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Neotec Remittance API error: {fn.__name__}")
			frappe.local.response["http_status_code"] = 500
			frappe.clear_messages()
			return {"error": {"code": "INTERNAL_ERROR", "message": "Unexpected error",
				"correlation_id": frappe.request.headers.get("X-Correlation-ID") if frappe.request else None}}
	return wrapper


def header(name):
	return frappe.request.headers.get(name) if getattr(frappe, "request", None) else None
