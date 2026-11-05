"""Name normalisation for matching (screening, duplicate checks). Original legal spelling is always kept separately."""
import re
import unicodedata

_AR_MAP = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ة": "ه", "ؤ": "و", "ئ": "ي", "ـ": None})
_HARAKAT = re.compile("[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed]")
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")
_HONORIFICS = {"mr", "mrs", "ms", "dr", "al", "el", "bin", "ibn", "bint", "abu"}


def normalize_name(value: str) -> str:
	if not value:
		return ""
	s = _HARAKAT.sub("", value).translate(_AR_MAP)
	s = unicodedata.normalize("NFKD", s)
	s = "".join(ch for ch in s if not unicodedata.combining(ch))
	s = _NON_WORD.sub(" ", s.casefold()).replace("_", " ")
	return _SPACES.sub(" ", s).strip()


def name_tokens(value: str) -> set[str]:
	return {t for t in normalize_name(value).split(" ") if t and t not in _HONORIFICS and len(t) > 1}


def mask_tail(value: str, keep=4) -> str:
	v = re.sub(r"\s", "", value or "")
	if len(v) <= keep:
		return "*" * len(v)
	return "*" * (len(v) - keep) + v[-keep:]
