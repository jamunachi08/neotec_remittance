import frappe

from neotec_remittance.utils.errors import ConfigurationError


def get_settings():
	return frappe.get_cached_doc("Remittance Settings")


def require_enabled():
	s = get_settings()
	if not s.enabled:
		raise ConfigurationError("Remittance platform is disabled", code="PLATFORM_DISABLED")
	return s


def poll_schedule() -> list[int]:
	raw = get_settings().status_poll_schedule or "30,120,300,600"
	out = []
	for part in raw.split(","):
		try:
			out.append(max(5, int(part.strip())))
		except ValueError:
			continue
	return out or [30, 120, 300, 600]


def set_values(values: dict):
	"""Engine-side update of Settings fields (bypasses form validation) with cache invalidation."""
	frappe.db.set_single_value("Remittance Settings", values)
	frappe.clear_document_cache("Remittance Settings", "Remittance Settings")
