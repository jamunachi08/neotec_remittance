import frappe

from neotec_remittance.utils.errors import ConfigurationError


def get_adapter(partner_name: str):
	partner = frappe.get_cached_doc("Remittance Partner", partner_name)
	if partner.adapter == "Sandbox":
		if partner.environment == "Production":
			raise ConfigurationError("Sandbox adapter cannot be used in Production", code="ADAPTER_MISCONFIGURED")
		from neotec_remittance.adapters.sandbox import SandboxAdapter

		return SandboxAdapter(partner)
	if partner.adapter == "Gateway":
		from neotec_remittance.adapters.gateway import GatewayAdapter

		return GatewayAdapter(partner)
	if partner.adapter == "Custom" and partner.adapter_path:
		return frappe.get_attr(partner.adapter_path)(partner)
	raise ConfigurationError(f"No adapter configured for {partner_name}", code="ADAPTER_MISCONFIGURED")
