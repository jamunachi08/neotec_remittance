"""Field protection: envelope encryption with a pluggable key provider.

Each value gets its own random data key (Fernet). The data key is wrapped by a master key provider.
The default provider wraps with the site's encryption key; a KMS/HSM provider can be plugged in via the
hook `neotec_remittance_key_provider` (dotted path to a class with key_id, wrap(bytes), unwrap(bytes)).
Blind indexes (keyed HMAC) allow exact matching without decrypting."""
import base64

import frappe
from cryptography.fernet import Fernet

from neotec_remittance.utils.hashing import hmac_hex

PREFIX = "nre1"


class SiteKeyProvider:
	key_id = "site"

	def _fernet(self):
		from frappe.utils.password import get_encryption_key

		return Fernet(get_encryption_key().encode())

	def wrap(self, data_key: bytes) -> bytes:
		return self._fernet().encrypt(data_key)

	def unwrap(self, wrapped: bytes) -> bytes:
		return self._fernet().decrypt(wrapped)


def _provider():
	paths = frappe.get_hooks("neotec_remittance_key_provider") or []
	if paths:
		return frappe.get_attr(paths[-1])()
	return SiteKeyProvider()


def _b64(b: bytes) -> str:
	return base64.urlsafe_b64encode(b).decode()


def _unb64(s: str) -> bytes:
	return base64.urlsafe_b64decode(s.encode())


def encrypt(plaintext: str | None) -> str | None:
	if plaintext in (None, ""):
		return None
	provider = _provider()
	data_key = Fernet.generate_key()
	token = Fernet(data_key).encrypt(plaintext.encode())
	return ".".join([PREFIX, provider.key_id, _b64(provider.wrap(data_key)), _b64(token)])


def decrypt(blob: str | None) -> str | None:
	if not blob:
		return None
	prefix, key_id, wrapped, token = blob.split(".", 3)
	if prefix != PREFIX:
		raise ValueError("Unknown ciphertext format")
	provider = _provider()
	if provider.key_id != key_id and key_id != "site":
		raise ValueError(f"Ciphertext was wrapped by key provider '{key_id}'")
	if key_id == "site" and provider.key_id != "site":
		provider = SiteKeyProvider()
	data_key = provider.unwrap(_unb64(wrapped))
	return Fernet(data_key).decrypt(_unb64(token)).decode()


def _index_key() -> str:
	key = frappe.conf.get("neotec_remittance_index_key")
	if key:
		return key
	from frappe.utils.password import get_encryption_key

	return hmac_hex(get_encryption_key(), "neotec-remittance-blind-index-v1")


def blind_index(value: str | None, purpose: str) -> str | None:
	if not value:
		return None
	norm = "".join(value.split()).upper()
	return hmac_hex(_index_key(), f"{purpose}:{norm}")
