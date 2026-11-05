import hashlib
import hmac
import json
from decimal import Decimal


def _default(o):
	if isinstance(o, Decimal):
		return format(o, "f")
	return str(o)


def canonical_json(obj) -> str:
	return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default)


def sha256_hex(data) -> str:
	if isinstance(data, str):
		data = data.encode()
	return hashlib.sha256(data).hexdigest()


def digest(obj) -> str:
	return sha256_hex(canonical_json(obj))


def hmac_hex(key, message) -> str:
	if isinstance(key, str):
		key = key.encode()
	if isinstance(message, str):
		message = message.encode()
	return hmac.new(key, message, hashlib.sha256).hexdigest()


def safe_equals(a: str, b: str) -> bool:
	return hmac.compare_digest((a or "").encode(), (b or "").encode())
