"""HTTP smoke-test preparation (not a unit test). Usage:
bench --site <site> execute neotec_remittance.tests.http_smoke.prepare
Returns the subject id and secrets for an external HTTP client to exercise the public API."""
import frappe

from neotec_remittance.services.settings import set_values
from neotec_remittance.tests import fixtures as fx


def prepare():
	frappe.flags.in_test = True  # bypass interactive user-creation throttling for the fixture user
	fx.setup_platform()
	c = fx.make_customer()
	subject = "sub-" + frappe.generate_hash(length=10)
	frappe.db.set_value("Remittance Customer", c.name, "subject_id", subject, update_modified=False)
	settings = frappe.get_doc("Remittance Settings")
	settings.identity_mode = "Gateway Assertion"
	settings.gateway_shared_secret = "smoke-gateway-secret-123456"
	settings.save(ignore_permissions=True)
	frappe.db.commit()
	return {"subject": subject, "gateway_secret": "smoke-gateway-secret-123456", "partner": fx.PARTNER,
		"callback_secret": fx.CALLBACK_SECRET}


def restore():
	set_values({"identity_mode": "Frappe Session"})
	frappe.db.commit()
