# Copyright (c) 2026, Neotec Integrated Solutions
# License: see license.txt

from frappe.model.document import Document

from neotec_remittance.utils.names import normalize_name


class RemittanceWatchlistEntry(Document):
	def validate(self):
		self.name_normalized = normalize_name(self.full_name)

	def on_update(self):
		from neotec_remittance.services.screening import on_watchlist_change

		on_watchlist_change()

	def on_trash(self):
		from neotec_remittance.services.screening import on_watchlist_change

		on_watchlist_change()
