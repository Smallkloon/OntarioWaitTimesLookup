"""Data and analysis layer for the Ontario wait-time tools. No UI code lives here.

Data sources (plain GET, JSON, no auth), the same APIs behind
https://www.ontariohealth.ca/system/reporting/wait-times:

- ``https://or.hqontario.ca`` for diagnostic imaging (CT, MRI) and surgery.
  - ``/city/postalcode/{POSTAL}``: postal code to coordinates.
  - ``/DiagnosticImaging/wtdata/EN/adult/{lat}/{lng}/{page}/{CT|MRI}/1``: the
    nearby site list for one modality.
  - ``/DiagnosticImaging/wtchartdata/EN/adult/{CT|MRI}/1/{site_id}``: one site's
    monthly history since January 2022.
  - ``/Surgical/wtsurgicaldata/EN/adult/{lat}/{lng}/{page}/{type}/{modality}/{wait}``
    and ``/Surgical/wtsurgicalchartdata/EN/adult/{type}/{modality}/{wait}/{site_id}``:
    the same pair for one surgical procedure; wait is 1 (referral to first
    clinician appointment) or 2 (decision to surgery).
  - ``/surgery/getsurgeriesbyname/surgical/EN/adult/``: every reported procedure.
  - Site id -354 is the Ontario provincial figure in every history endpoint.
- ``https://obspwaittimeapi.ontariohealth.ca/api/obsp`` for Ontario Breast
  Screening Program sites: current estimated wait, phone numbers and hours,
  no monthly history.

Wait numbers for CT, MRI and surgery always come from the per-site history
endpoints. The list endpoints are used only to find sites.
"""
from __future__ import annotations

import csv
import functools
import hashlib
import json
import math
import os
import re
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional, Union

BASE = "https://or.hqontario.ca"
OBSP_BASE = "https://obspwaittimeapi.ontariohealth.ca"
SOURCE_URL = "https://www.ontariohealth.ca"
SOURCE_LABEL = "www.ontariohealth.ca"
LANG = "EN"
AGE_GROUP = "adult"
PROVINCIAL_ID = -354
PROVINCIAL_NAME = "Ontario provincial average"
SICKKIDS_ID = 4824
PRIORITIES = (2, 3, 4)
IMAGING = ("CT", "MRI", "BREAST")
SURGERY = "SURGERY"
MODALITY_LABELS = {"CT": "CT", "MRI": "MRI", "BREAST": "Breast screening", SURGERY: "Surgery"}
MODALITY_ALIASES = {
	"CT": "CT", "MRI": "MRI",
	"BREAST": "BREAST", "BREAST SCREENING": "BREAST", "BREAST_SCREENING": "BREAST",
	"BREAST-SCREENING": "BREAST", "BREASTSCREENING": "BREAST", "OBSP": "BREAST",
	"MAMMOGRAM": "BREAST", "MAMMOGRAPHY": "BREAST",
}
SURGERY_WAITS = {
	2: "Decision to surgery (Wait 2)",
	1: "Referral to first appointment (Wait 1)",
}
DEFAULT_RADIUS_KM = 40.0
DEFAULT_POSTAL = ""  # no built-in location; the app remembers the last one locally
MAX_WORKERS = 8
TIMEOUT_S = 30
RETRIES = 1
CACHE_TTL_S = 24 * 60 * 60
MAX_PAGES = 100
OBSP_PAGE_SIZE = 50  # the breast screening API returns the 50 nearest sites to any origin, no paging
SCREENING_MAX_QUERIES = 80
SCREENING_MIN_SPLIT_KM = 5.0
SCREENING_WORKERS = 4
SCREENING_CAP_NOTE = "Some outlying breast screening sites may be missing from this search."
CONTACTS_FILE = Path(__file__).with_name("contacts.json")
EARTH_KM = 6371.0
LAGS = (1, 3, 6, 12)
MIN_SITES_FOR_RHO = 6
WINDOW_MONTHS = 12
USER_AGENT = "CTWaits/2.0"
MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July",
	"August", "September", "October", "November", "December")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

POSTAL_RE = re.compile(r"^[A-Z]\d[A-Z]\d[A-Z]\d$")

# month key "YYYYMM" -> {priority: value or None}
History = dict[str, dict[int, Optional[float]]]
ProgressFn = Callable[[str], None]


class ApiError(Exception):
	"""The API could not be reached or returned something unusable."""


# ---------------------------------------------------------------- formatting helpers

def parse_number(v) -> Optional[float]:
	"""Wait figures arrive as strings. "RI" (reported insufficient), "LV" (too
	few cases to report), "NV" (no cases), "NS" (not performed here), blanks
	and missing values all mean None."""
	try:
		return float(v)
	except (TypeError, ValueError):
		return None


def month_label(key: Optional[str]) -> str:
	"""'202607' -> '2026-07'."""
	key = str(key or "")
	return f"{key[:4]}-{key[4:]}" if len(key) == 6 else key


def month_long(key: Optional[str]) -> str:
	"""'202607' -> 'July 2026'."""
	key = str(key or "")
	if len(key) != 6 or not key.isdigit() or not 1 <= int(key[4:]) <= 12:
		return key
	return f"{MONTH_NAMES[int(key[4:]) - 1]} {key[:4]}"


def month_short(key: Optional[str]) -> str:
	"""'202607' -> 'Jul 2026'."""
	long = month_long(key)
	return long[:3] + long[long.find(" "):] if " " in long else long


def format_date(d: Optional[date]) -> str:
	"""date(2026, 9, 25) -> 'September 25, 2026'."""
	return f"{MONTH_NAMES[d.month - 1]} {d.day}, {d.year}" if d else ""


def format_phone(raw) -> str:
	digits = re.sub(r"\D", "", str(raw or ""))
	if len(digits) == 10:
		return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
	if len(digits) == 11 and digits[0] == "1":
		return f"1-{digits[1:4]}-{digits[4:7]}-{digits[7:]}"
	return str(raw or "").strip()


def format_hour(hhmm) -> str:
	"""'0800' -> '8:00 am', '1630' -> '4:30 pm'."""
	s = str(hhmm or "").strip()
	if len(s) != 4 or not s.isdigit():
		return s
	h = int(s[:2])
	return f"{h % 12 or 12}:{s[2:]} {'pm' if h >= 12 else 'am'}"


# ---------------------------------------------------------------- validation

def normalize_postal(postal) -> str:
	"""Upper-case, strip whitespace, and validate a Canadian postal code."""
	code = re.sub(r"\s+", "", str(postal or "")).upper()
	if not code:
		raise ValueError("Enter a postal code, for example A1A 1A1.")
	if not POSTAL_RE.match(code):
		raise ValueError(f"Invalid postal code {postal!r}; expected a form like A1A 1A1.")
	return code


def normalize_modality(modality) -> str:
	"""CT, MRI or BREAST (breast screening)."""
	m = re.sub(r"\s+", " ", str(modality or "").strip().upper())
	if m not in MODALITY_ALIASES:
		raise ValueError(f"Imaging type must be CT, MRI or breast screening; got {modality!r}.")
	return MODALITY_ALIASES[m]


def normalize_priority(priority) -> int:
	try:
		p = int(priority)
	except (TypeError, ValueError):
		raise ValueError(f"Priority must be 2, 3, or 4; got {priority!r}.") from None
	if p not in PRIORITIES:
		raise ValueError(f"Priority must be 2, 3, or 4; got {priority!r}.")
	return p


def normalize_wait(wait) -> int:
	try:
		w = int(wait)
	except (TypeError, ValueError):
		raise ValueError(f"Surgery wait must be 1 or 2; got {wait!r}.") from None
	if w not in SURGERY_WAITS:
		raise ValueError(f"Surgery wait must be 1 or 2; got {wait!r}.")
	return w


def normalize_radius(radius_km) -> float:
	try:
		r = float(radius_km)
	except (TypeError, ValueError):
		raise ValueError(f"Radius must be a number of kilometres; got {radius_km!r}.") from None
	if not 0 < r <= 500:
		raise ValueError("Radius must be between 0 and 500 km.")
	return r


# ---------------------------------------------------------------- HTTP + cache

def default_cache_dir() -> Path:
	base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
	return Path(base) / "CTWaits" / "cache"


CACHE_DIR: Optional[Path] = default_cache_dir()  # set to None to disable caching


def _cache_path(url: str) -> Optional[Path]:
	if CACHE_DIR is None:
		return None
	return Path(CACHE_DIR) / (hashlib.sha1(url.encode("utf-8")).hexdigest() + ".json")


def _cache_read(path: Path):
	try:
		if time.time() - path.stat().st_mtime > CACHE_TTL_S:
			return None
		return json.loads(path.read_bytes())
	except (OSError, ValueError):
		return None


def _cache_write(path: Path, raw: bytes) -> None:
	try:
		path.parent.mkdir(parents=True, exist_ok=True)
		tmp = path.with_suffix(".tmp")
		tmp.write_bytes(raw)
		os.replace(tmp, path)
	except OSError:
		pass


def clear_cache() -> int:
	"""Expire every cached response. Returns the number of files touched."""
	if CACHE_DIR is None or not Path(CACHE_DIR).exists():
		return 0
	n = 0
	for p in Path(CACHE_DIR).glob("*.json"):
		try:
			os.utime(p, (0, 0))
			n += 1
		except OSError:
			pass
	return n


def fetch_json(url: str):
	"""GET ``url`` and parse JSON. 30 s timeout, one retry, 24 h disk cache."""
	path = _cache_path(url)
	if path is not None:
		cached = _cache_read(path)
		if cached is not None:
			return cached
	last: Optional[BaseException] = None
	for _attempt in range(RETRIES + 1):
		try:
			req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
			with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
				raw = r.read()
			data = json.loads(raw)
			if path is not None:
				_cache_write(path, raw)
			return data
		except (urllib.error.URLError, OSError, ValueError) as e:
			last = e
	raise ApiError(f"Could not fetch {url}: {last}") from last


# ---------------------------------------------------------------- dataclasses

@dataclass(frozen=True)
class Procedure:
	"""A surgical procedure Ontario Health reports wait times for."""
	id: int
	name: str
	modality_type_id: int
	modality_id: int

	@classmethod
	def from_api(cls, r: dict) -> "Procedure":
		return cls(int(r["Id"]), re.sub(r"\s+", " ", str(r.get("NameEn") or "")).strip(),
			int(r["ModalityTypeID"]), int(r["ModalityID"]))

	def to_dict(self) -> dict:
		return {"id": self.id, "name": self.name}


@dataclass(frozen=True)
class Service:
	"""What is being searched: CT, MRI, breast screening, or one surgical
	procedure with a wait type (1 or 2)."""
	modality: str = "CT"
	procedure: Optional[Procedure] = None
	wait: int = 2

	def __post_init__(self):
		if self.modality not in IMAGING + (SURGERY,):
			raise ValueError(f"Unknown service {self.modality!r}.")
		if self.modality == SURGERY and self.procedure is None:
			raise ValueError("Choose a surgical procedure.")
		if self.wait not in SURGERY_WAITS:
			raise ValueError(f"Surgery wait must be 1 or 2; got {self.wait!r}.")

	@property
	def is_surgery(self) -> bool:
		return self.modality == SURGERY

	@property
	def is_screening(self) -> bool:
		return self.modality == "BREAST"

	@property
	def has_history(self) -> bool:
		return not self.is_screening

	@property
	def label(self) -> str:
		if self.is_surgery:
			return f"{self.procedure.name}, {SURGERY_WAITS[self.wait]}"
		return MODALITY_LABELS[self.modality]

	@property
	def within_verb(self) -> str:
		"""How the % within target figure is phrased."""
		if self.is_surgery:
			return "treated" if self.wait == 2 else "seen"
		return "scanned"

	@property
	def department(self) -> str:
		if self.is_surgery:
			return f"{self.procedure.name} surgery"
		return "breast screening" if self.is_screening else "diagnostic imaging"

	def list_url(self, lat: float, lng: float, page: int) -> str:
		if self.is_surgery:
			p = self.procedure
			return (f"{BASE}/Surgical/wtsurgicaldata/{LANG}/{AGE_GROUP}/{lat}/{lng}/{page}/"
				f"{p.modality_type_id}/{p.modality_id}/{self.wait}")
		if self.is_screening:
			raise ValueError("Breast screening sites come from screening_sites().")
		return f"{BASE}/DiagnosticImaging/wtdata/{LANG}/{AGE_GROUP}/{lat}/{lng}/{page}/{self.modality}/1"

	def chart_url(self, site_id: int) -> str:
		if self.is_surgery:
			p = self.procedure
			return (f"{BASE}/Surgical/wtsurgicalchartdata/{LANG}/{AGE_GROUP}/"
				f"{p.modality_type_id}/{p.modality_id}/{self.wait}/{int(site_id)}")
		if self.is_screening:
			raise ValueError("Breast screening has no monthly history.")
		return f"{BASE}/DiagnosticImaging/wtchartdata/{LANG}/{AGE_GROUP}/{self.modality}/1/{int(site_id)}"

	def to_dict(self) -> dict:
		return {"label": self.label, "modality": self.modality,
			"procedure": self.procedure.to_dict() if self.procedure else None,
			"surgery_wait": self.wait if self.is_surgery else None}


def as_service(service: Union[Service, str, None]) -> Service:
	if isinstance(service, Service):
		return service
	return Service(normalize_modality(service or "CT"))


@dataclass(frozen=True)
class Site:
	"""A hospital site from a list endpoint. Deliberately carries no wait
	figures: wait numbers come only from the per-site history."""
	id: int
	name: str
	address: str
	city: str
	postal_code: str
	distance_km: float
	latitude: float
	longitude: float

	@classmethod
	def from_api(cls, r: dict) -> "Site":
		return cls(
			id=int(r["Id"]),
			name=str(r.get("Name") or "").strip(),
			address=", ".join(x.strip() for x in (r.get("Address1"), r.get("Address2")) if x and str(x).strip()),
			city=str(r.get("City") or "").strip(),
			postal_code=str(r.get("PostalCode") or "").strip(),
			distance_km=float(r.get("Distance") or 0.0),
			latitude=float(r.get("Latitude") or 0.0),
			longitude=float(r.get("Longitude") or 0.0),
		)

	@property
	def full_address(self) -> str:
		pc = self.postal_code
		if len(pc) == 6:
			pc = f"{pc[:3]} {pc[3:]}"
		return ", ".join(x for x in (self.address, " ".join(y for y in (self.city, pc) if y)) if x)

	def to_dict(self) -> dict:
		return asdict(self)


PROVINCE_SITE = Site(PROVINCIAL_ID, PROVINCIAL_NAME, "", "", "", 0.0, 0.0, 0.0)


@dataclass
class SiteHistory:
	"""One site's monthly history: P90 wait days, % within target, and the
	latest target in days, each by priority (2, 3, 4)."""
	p90: History = field(default_factory=dict)
	within: History = field(default_factory=dict)
	targets: dict[int, Optional[float]] = field(default_factory=dict)
	cases: History = field(default_factory=dict)

	def months(self) -> list[str]:
		return sorted(self.p90)

	def recording_since(self, priority) -> tuple[Optional[str], int, int]:
		"""(first month with a reported P90, months reported, months since then)."""
		p = normalize_priority(priority)
		months = self.months()
		reported = [m for m in months if self.p90[m].get(p) is not None]
		if not reported:
			return None, 0, 0
		return reported[0], len(reported), sum(1 for m in months if m >= reported[0])

	def within_latest(self, priority, month: Optional[str] = None) -> tuple[Optional[str], Optional[float]]:
		"""(month, % within target) for ``month``, or the newest month that has a figure."""
		p = normalize_priority(priority)
		if month is not None:
			return month, (self.within.get(month) or {}).get(p)
		for m in sorted(self.within, reverse=True):
			v = self.within[m].get(p)
			if v is not None:
				return m, v
		return None, None

	def within_mean(self, priority, months: list[str]) -> Optional[float]:
		p = normalize_priority(priority)
		vals = [self.within[m][p] for m in months if m in self.within and self.within[m].get(p) is not None]
		return statistics.mean(vals) if vals else None

	def within_period(self, priority, months: list[str]) -> Optional[float]:
		"""Share of patients within target across ``months``, weighting each month
		by its case count; the plain monthly mean when counts are missing."""
		p = normalize_priority(priority)
		num = den = 0.0
		for m in months:
			w = (self.within.get(m) or {}).get(p)
			n = (self.cases.get(m) or {}).get(p)
			if w is not None and n:
				num += w * n
				den += n
		return num / den if den > 0 else self.within_mean(p, months)


def parse_chart(rows) -> SiteHistory:
	"""Turn a wtchartdata or wtsurgicalchartdata response into a SiteHistory."""
	out = SiteHistory()
	if not isinstance(rows, list):
		raise ApiError("History response was not a list.")
	newest = None
	for m in rows:
		if not isinstance(m, dict) or "Key" not in m:
			continue
		key = str(m["Key"]).strip()
		waits: dict[int, dict] = {}
		for x in m.get("WaitTimes") or []:
			try:
				waits[int(x.get("PriorityId"))] = x
			except (TypeError, ValueError):
				continue
		out.p90[key] = {p: parse_number((waits.get(p) or {}).get("WaitTime90percentile")) for p in PRIORITIES}
		out.within[key] = {p: parse_number((waits.get(p) or {}).get("WaitTimePercentWithinTarget")) for p in PRIORITIES}
		out.cases[key] = {p: parse_number((waits.get(p) or {}).get("NumberOfCases")) for p in PRIORITIES}
		if newest is None or key > newest:
			newest = key
			out.targets = {p: parse_number((waits.get(p) or {}).get("Target")) for p in PRIORITIES}
	return out


@dataclass
class ScreeningSite:
	"""An Ontario Breast Screening Program site: current estimate and contact details."""
	id: str
	name: str
	address: str
	city: str
	postal_code: str
	distance_km: float
	wait_days: Optional[float]
	wait_text: str
	phone: str
	ext: str
	toll_free: str
	hours: dict[str, str]
	accessible: bool
	languages: str
	last_updated: Optional[date]
	latitude: float = 0.0
	longitude: float = 0.0

	@classmethod
	def from_api(cls, r: dict) -> "ScreeningSite":
		addr = r.get("address") or {}
		excluded = str(r.get("wtExcluded") or "").upper() == "Y"
		days = None if excluded else parse_number(r.get("waitTimeDays"))
		hours = {}
		for d in WEEKDAYS:
			h = (r.get("hours") or {}).get(d)
			if h and h.get("startTime") and h.get("endTime"):
				hours[d] = f"{format_hour(h['startTime'])} to {format_hour(h['endTime'])}"
			else:
				hours[d] = "Closed"
		langs = r.get("languages") or {}
		spoken = [name for key, name in (("english", "English"), ("french", "French")) if langs.get(key)]
		updated = None
		try:
			updated = datetime.fromisoformat(str(r.get("lastUpdated"))[:19]).date()
		except (TypeError, ValueError):
			pass
		toll = format_phone(r.get("tollFree"))
		if toll and not toll.startswith("1-"):
			toll = "1-" + toll
		return cls(
			id=str(r.get("id") or ""),
			name=str(r.get("name") or "").strip(),
			address=", ".join(x.strip() for x in (addr.get("line_1"), addr.get("line_2")) if x and str(x).strip()),
			city=str(addr.get("city") or "").strip(),
			postal_code=str(addr.get("postalCode") or "").strip(),
			distance_km=float(r.get("distance") or 0.0),
			wait_days=days,
			wait_text=(str(r.get("waitTime") or "").strip() or f"{days:g} days") if days is not None else "Not available",
			phone=format_phone(r.get("phone1")),
			ext=str(r.get("ext1") or "").strip(),
			toll_free=toll,
			hours=hours,
			accessible=str(r.get("accessibility") or "").upper() == "Y",
			languages=" and ".join(spoken),
			last_updated=updated,
			latitude=float(r.get("latitude") or 0.0),
			longitude=float(r.get("longitude") or 0.0),
		)

	@property
	def full_address(self) -> str:
		return ", ".join(x for x in (self.address, " ".join(y for y in (self.city, self.postal_code) if y)) if x)

	@property
	def sort_key(self) -> tuple:
		"""Wait band ("0 to 2 weeks" sorts before "2 to 4 weeks"), then distance;
		sites without an estimate last."""
		m = re.match(r"\s*(\d+)", self.wait_text)
		band = int(m.group(1)) if m and self.wait_days is not None else (self.wait_days or 0.0) / 7
		return (self.wait_days is None, band, self.distance_km)

	def to_dict(self) -> dict:
		d = asdict(self)
		d["distance_km"] = round(self.distance_km, 1)
		d["last_updated"] = self.last_updated.isoformat() if self.last_updated else None
		return d


@dataclass
class Row:
	"""One ranked site (or the provincial line)."""
	site: Site
	latest_p90: Optional[float]
	median_p90: Optional[float]
	months_available: int

	@property
	def is_province(self) -> bool:
		return self.site.id == PROVINCIAL_ID

	def to_dict(self, hist: Optional[SiteHistory] = None, priority: Optional[int] = None,
			latest: Optional[str] = None) -> dict:
		d = {
			"site_id": self.site.id,
			"name": self.site.name,
			"address": self.site.full_address,
			"distance_km": None if self.is_province else round(self.site.distance_km, 2),
			"latest_p90_days": self.latest_p90,
			"median_p90_days_12mo": self.median_p90,
		}
		if hist is not None and priority is not None:
			first, reported, total = hist.recording_since(priority)
			_m, pct = hist.within_latest(priority, latest)
			d.update({
				"pct_within_target_latest": pct,
				"target_days": hist.targets.get(priority),
				"recording_since": month_label(first) if first else None,
				"months_reported": reported,
			})
		if not self.is_province:
			c = contact_for(self.site.id)
			d["contact"] = c.to_dict() if c else None
		return d


@dataclass
class LagStat:
	"""Rank persistence at one lag."""
	lag_months: int
	mean_rho: float
	leader_top3_share: float
	chance_share: float
	pairs: int

	def to_dict(self) -> dict:
		return asdict(self)


@dataclass
class Analysis:
	"""Everything one search produces, for either front end."""
	postal_code: str
	latitude: float
	longitude: float
	service: Service
	priority: Optional[int]
	radius_km: float
	today: date
	latest_month: Optional[str]
	last_updated: str
	sites: list[Site] = field(default_factory=list)
	histories: dict[int, SiteHistory] = field(default_factory=dict)
	province: Optional[SiteHistory] = None
	province_row: Optional[Row] = None
	rows: list[Row] = field(default_factory=list)
	lag_stats: list[LagStat] = field(default_factory=list)
	screening: list[ScreeningSite] = field(default_factory=list)
	no_data: list[Row] = field(default_factory=list)
	screening_complete: bool = True

	@property
	def screening_capped(self) -> bool:
		return self.service.is_screening and not self.screening_complete

	@property
	def window(self) -> list[str]:
		"""The last 12 months of the series, oldest first."""
		return month_keys({k: h.p90 for k, h in self.histories.items()})[-WINDOW_MONTHS:]

	def to_dict(self) -> dict:
		out = {
			"postal_code": self.postal_code,
			"service": self.service.to_dict(),
			"priority": self.priority,
			"radius_km": self.radius_km,
			"latest_month": month_label(self.latest_month) if self.latest_month else None,
			"ontario_health_last_updated": self.last_updated,
			"source": SOURCE_URL,
		}
		if self.service.is_screening:
			out["sites_found"] = len(self.screening)
			if self.screening_capped:
				out["note"] = SCREENING_CAP_NOTE
			out["rows"] = [s.to_dict() for s in self.screening]
			return out
		out["sites_found"] = len(self.sites)
		out["ontario_average"] = (self.province_row.to_dict(self.province, self.priority, self.latest_month)
			if self.province_row else None)
		out["rows"] = [r.to_dict(self.histories.get(r.site.id), self.priority, self.latest_month) for r in self.rows]
		out["sites_without_recent_data"] = [r.to_dict(self.histories.get(r.site.id), self.priority, self.latest_month)
			for r in self.no_data]
		out["persistence"] = [s.to_dict() for s in self.lag_stats]
		return out


# ---------------------------------------------------------------- API wrappers

def geocode(postal) -> tuple[float, float]:
	"""Postal code -> (lat, lng). Rejects codes outside Ontario."""
	code = normalize_postal(postal)
	data = fetch_json(f"{BASE}/city/postalcode/{code}")
	if (not isinstance(data, dict) or data.get("Latitude") in (None, "")
			or data.get("Longitude") in (None, "")):
		raise ApiError(f"Postal code {code} was not recognised by the Ontario Health lookup.")
	if not data.get("InOntario"):
		raise ValueError(f"Postal code {code} is not in Ontario.")
	return float(data["Latitude"]), float(data["Longitude"])


def _is_sickkids(r: dict) -> bool:
	name = str(r.get("Name") or "").lower()
	return int(r["Id"]) == SICKKIDS_ID or "sick children" in name or "sickkids" in name


def nearby_sites(lat: float, lng: float, max_km: float, service: Union[Service, str] = "CT") -> list[Site]:
	"""Sites within ``max_km`` of (lat, lng) that report ``service``, nearest first.

	Drops the provincial total row (Id -354) and, in this adult tool, the
	Hospital for Sick Children. Paginates until the last distance on a page
	exceeds the radius or a page returns no new Ids. Wait figures on these
	rows are ignored.
	"""
	svc = as_service(service)
	sites: list[Site] = []
	seen: set[int] = set()
	page = 1
	while page <= MAX_PAGES:
		rows = fetch_json(svc.list_url(lat, lng, page))
		if not isinstance(rows, list):
			raise ApiError("Site list response was not a list.")
		rows = [r for r in rows if int(r["Id"]) != PROVINCIAL_ID and int(r["Id"]) not in seen]
		if not rows:
			break
		for r in rows:
			seen.add(int(r["Id"]))
			if float(r["Distance"]) <= max_km and not _is_sickkids(r):
				sites.append(Site.from_api(r))
		if float(rows[-1]["Distance"]) > max_km:
			break
		page += 1
	return sites


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
	p1, p2 = math.radians(lat1), math.radians(lat2)
	dp, dl = p2 - p1, math.radians(lng2 - lng1)
	a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
	return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(a)))


def offset_point(lat: float, lng: float, dist_km: float, bearing_deg: float) -> tuple[float, float]:
	"""The point ``dist_km`` from (lat, lng) along ``bearing_deg``."""
	d, b = dist_km / EARTH_KM, math.radians(bearing_deg)
	p1, l1 = math.radians(lat), math.radians(lng)
	p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
	l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
	return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


def _obsp_nearest(lat: float, lng: float) -> list[dict]:
	q = urllib.parse.urlencode({"originLatitude": lat, "originLongitude": lng, "start": 0,
		"itemsCount": OBSP_PAGE_SIZE, "sortBy": "distance", "sortOrder": "asc", "LocationType": "PostalCode"})
	rows = fetch_json(f"{OBSP_BASE}/api/obsp?{q}")
	if not isinstance(rows, list):
		raise ApiError("Breast screening response was not a list.")
	return rows


def _obsp_covered_km(lat: float, lng: float, rows: list[dict]) -> float:
	"""Straight-line radius around (lat, lng) inside which one response is
	complete. The API returns the 50 nearest sites by driving distance (or by
	a straight-line proxy, about 1.41 times the great-circle distance, when no
	route exists), so a missing site is at least as far by road as the
	farthest one returned. Driving routes are taken to be at most twice the
	straight-line distance."""
	if len(rows) < OBSP_PAGE_SIZE:
		return float("inf")
	far = max(float(r.get("distance") or 0.0) for r in rows)
	routed = sum(1 for r in rows if r.get("condition") == "ROUTE_EXISTS") * 2 >= len(rows)
	straight = max(haversine_km(lat, lng, float(r.get("latitude") or 0), float(r.get("longitude") or 0)) for r in rows)
	return min(straight, far / (2.0 if routed else 1.42))


def screening_search(lat: float, lng: float, max_km: float) -> tuple[list[ScreeningSite], bool]:
	"""Breast screening sites within ``max_km`` straight-line, sorted by
	estimated wait band (sites without an estimate last), then distance.

	The API answers each query with only the 50 sites nearest its origin and
	ignores paging, so the search area is covered with extra origins: when one
	query does not reach the edge of its disk, the disk is split into seven
	half-radius disks (the centre plus six around it) and those are queried
	too. The API errors for some origins outside its service area (open
	water, the United States); such a disk is split once more without its
	centre, and dropped once it is small. Returns (sites, complete); complete
	is False only if the query budget ran out first."""
	found: dict[str, dict] = {}
	covered: dict[tuple[float, float], float] = {}
	complete = True
	origin = (round(lat, 4), round(lng, 4))
	pending = [(origin, float(max_km))]

	def query(pt):
		# The API also answers HTTP 500 to bursts of parallel requests, so back off and retry.
		for attempt in range(3):
			try:
				return _obsp_nearest(*pt)
			except ApiError:
				if attempt == 2:
					if pt == origin:
						raise
					return None
				time.sleep(0.4 * (attempt + 1))
		return None

	with ThreadPoolExecutor(max_workers=SCREENING_WORKERS) as ex:
		while pending:
			todo = list(dict.fromkeys(pt for pt, _need in pending if pt not in covered))
			room = SCREENING_MAX_QUERIES - len(covered)
			if len(todo) > room:
				todo, complete = todo[:max(0, room)], False
			for pt, rows in zip(todo, ex.map(query, todo)):
				if rows is None:
					covered[pt] = -1.0
					continue
				for r in rows:
					found.setdefault(str(r.get("id")), r)
				covered[pt] = _obsp_covered_km(pt[0], pt[1], rows)
			nxt = []
			for pt, need in pending:
				c = covered.get(pt)
				if c is None:
					complete = False
					continue
				if c < 0:
					if need > SCREENING_MIN_SPLIT_KM:
						for k in range(6):
							q = offset_point(pt[0], pt[1], need * math.sqrt(3) / 2, 60 * k)
							nxt.append(((round(q[0], 4), round(q[1], 4)), need / 2))
					continue
				if c >= need:
					continue
				half = need / 2
				if c < half:
					nxt.append((pt, half))
				for k in range(6):
					q = offset_point(pt[0], pt[1], need * math.sqrt(3) / 2, 60 * k)
					nxt.append(((round(q[0], 4), round(q[1], 4)), half))
			pending = nxt
	out: list[ScreeningSite] = []
	for r in found.values():
		s = ScreeningSite.from_api(r)
		s.distance_km = haversine_km(lat, lng, s.latitude, s.longitude)
		if s.distance_km <= max_km:
			out.append(s)
	out.sort(key=lambda s: s.sort_key)
	return out, complete


def screening_sites(lat: float, lng: float, max_km: float) -> list[ScreeningSite]:
	"""Breast screening sites within ``max_km``; see screening_search."""
	return screening_search(lat, lng, max_km)[0]


# ---------------------------------------------------------------- researched contacts

@dataclass(frozen=True)
class Intake:
	"""A shared intake fax: a regional CT/MRI intake, or a hip and knee intake."""
	fax: str
	label: str
	modalities: tuple[str, ...]
	note: str
	source: str


@dataclass(frozen=True)
class Contact:
	"""Phone and fax numbers for one hospital site, researched from published
	sources (Ontario Health's APIs do not carry them)."""
	main_phone: Optional[str]
	di_phone: Optional[str]
	di_fax: Optional[str]
	mri_fax: Optional[str]
	surgery_fax: Optional[str]
	source_urls: tuple[str, ...]
	confidence: str
	notes: str
	checked: str
	regional_intake: tuple[Intake, ...] = ()
	msk_intake: tuple[Intake, ...] = ()
	verified: bool = True

	def imaging_fax(self, modality: str) -> Optional[str]:
		return (self.mri_fax or self.di_fax) if modality == "MRI" else self.di_fax

	def intake_for(self, modality: str) -> list[Intake]:
		"""Regional intake faxes that take requisitions for ``modality``."""
		return [i for i in self.regional_intake if modality in i.modalities]

	def intake_named(self, fax: Optional[str]) -> Optional[Intake]:
		"""The regional intake whose fax is ``fax``, if any."""
		return next((i for i in self.regional_intake if fax and i.fax == fax), None)

	def to_dict(self) -> dict:
		d = asdict(self)
		d["source_urls"] = list(self.source_urls)
		d["regional_intake"] = [asdict(i) | {"modalities": list(i.modalities)} for i in self.regional_intake]
		d["msk_intake"] = [{"fax": i.fax, "label": i.label, "source": i.source} for i in self.msk_intake]
		return d


@functools.lru_cache(maxsize=1)
def load_contacts() -> dict[int, Contact]:
	try:
		raw = json.loads(CONTACTS_FILE.read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return {}
	checked = str(raw.get("checked") or "")
	out: dict[int, Contact] = {}
	for r in raw.get("sites") or []:
		try:
			sid = int(r["id"])
		except (KeyError, TypeError, ValueError):
			continue

		def clean(k, r=r):
			v = r.get(k)
			return str(v).strip() if v not in (None, "") else None

		def intakes(key, r=r):
			out_i = []
			for x in r.get(key) or []:
				if isinstance(x, dict) and x.get("fax"):
					out_i.append(Intake(str(x["fax"]), str(x.get("label") or "Central intake"),
						tuple(x.get("modalities") or ()), str(x.get("note") or ""), str(x.get("source") or "")))
			return tuple(out_i)

		out[sid] = Contact(clean("main_phone"), clean("di_phone"), clean("di_fax"), clean("mri_fax"),
			clean("surgery_fax"), tuple(u for u in (r.get("source_urls") or []) if u),
			str(r.get("confidence") or ""), str(r.get("notes") or ""), str(r.get("checked") or checked),
			intakes("regional_intake"), intakes("msk_intake"), bool(r.get("verified", True)))
	return out


def contact_for(site_id) -> Optional[Contact]:
	try:
		return load_contacts().get(int(site_id))
	except (TypeError, ValueError):
		return None


def procedures() -> list[Procedure]:
	"""Every adult surgical procedure Ontario Health reports, sorted by name."""
	rows = fetch_json(f"{BASE}/surgery/getsurgeriesbyname/surgical/{LANG}/{AGE_GROUP}/")
	if not isinstance(rows, list):
		raise ApiError("Procedure list response was not a list.")
	seen: dict[int, Procedure] = {}
	for r in rows:
		try:
			p = Procedure.from_api(r)
		except (KeyError, TypeError, ValueError):
			continue
		if p.name and p.id not in seen:
			seen[p.id] = p
	return sorted(seen.values(), key=lambda p: p.name.casefold())


def find_procedures(query: str, items: Optional[list[Procedure]] = None) -> list[Procedure]:
	"""Procedures whose name contains every word of ``query`` (case-insensitive);
	names starting with the query come first. An empty query returns all."""
	items = procedures() if items is None else items
	words = str(query or "").casefold().split()
	if not words:
		return list(items)
	q = " ".join(words)
	hits = [p for p in items if all(w in p.name.casefold() for w in words)]
	return sorted(hits, key=lambda p: (not p.name.casefold().startswith(q), p.name.casefold()))


def get_procedure(procedure_id, items: Optional[list[Procedure]] = None) -> Procedure:
	try:
		pid = int(procedure_id)
	except (TypeError, ValueError):
		raise ValueError(f"procedure_id must be an integer; got {procedure_id!r}.") from None
	for p in (procedures() if items is None else items):
		if p.id == pid:
			return p
	raise ValueError(f"No surgical procedure with id {pid}; use find_procedures to look one up.")


def site_history(site_id: int, service: Union[Service, str] = "CT") -> SiteHistory:
	"""One site's full monthly history (Id -354 is the province)."""
	return parse_chart(fetch_json(as_service(service).chart_url(int(site_id))))


def history(site_id: int, modality: Union[Service, str] = "CT") -> History:
	"""P90 wait by month and priority: {'YYYYMM': {2: d, 3: d, 4: d}}."""
	return site_history(site_id, modality).p90


def fetch_histories(sites, service: Union[Service, str] = "CT",
		progress: Optional[ProgressFn] = None) -> dict[int, SiteHistory]:
	"""Histories for every site (Site objects or ids), fetched concurrently
	(max 8 workers). The returned dict preserves input order so tie-breaking
	in the ranking matches the reference script."""
	svc = as_service(service)
	ids = [s.id if isinstance(s, Site) else int(s) for s in sites]
	results: dict[int, SiteHistory] = {}
	if not ids:
		return results
	with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
		futures = {ex.submit(site_history, sid, svc): sid for sid in ids}
		done = 0
		for fut in as_completed(futures):
			results[futures[fut]] = fut.result()
			done += 1
			if progress:
				progress(f"Fetching history {done}/{len(ids)}")
	return {sid: results[sid] for sid in ids}


# ---------------------------------------------------------------- analysis

def month_keys(histories: dict[int, History]) -> list[str]:
	return sorted({k for h in histories.values() for k in h})


def latest_month(histories: dict[int, History]) -> Optional[str]:
	months = month_keys(histories)
	return months[-1] if months else None


def _p90s(histories: dict) -> dict[int, History]:
	return {k: (h.p90 if isinstance(h, SiteHistory) else h) for k, h in histories.items()}


def summarize(site: Site, h: History, priority: int, months: list[str]) -> Optional[Row]:
	"""Latest and 12-month median P90 for one site; None when the window is empty."""
	if not months:
		return None
	latest, window = months[-1], months[-WINDOW_MONTHS:]
	vals = [h[m][priority] for m in window if m in h and h[m].get(priority) is not None]
	if not vals:
		return None
	return Row(site, (h.get(latest) or {}).get(priority), statistics.median(vals), len(vals))


def rank(sites: list[Site], histories: dict, priority) -> list[Row]:
	"""Rank sites by 12-month median P90, then by latest P90. Sites with no
	usable value in the last 12 months are omitted."""
	p = normalize_priority(priority)
	hist = _p90s(histories)
	months = month_keys(hist)
	rows = [r for s in sites if (r := summarize(s, hist.get(s.id) or {}, p, months)) is not None]
	rows.sort(key=lambda r: (r.median_p90, r.latest_p90 if r.latest_p90 is not None else 9e9))
	return rows


def spearman(a: list[float], b: list[float]) -> float:
	"""Spearman rho using ordinal ranks (ties broken by position), as in the
	reference script."""
	n = len(a)
	if n != len(b):
		raise ValueError("spearman: sequences differ in length")
	if n < 2:
		raise ValueError("spearman: need at least two observations")

	def ranks(x):
		order = sorted(range(len(x)), key=lambda i: x[i])
		r = [0] * len(x)
		for pos, i in enumerate(order):
			r[i] = pos
		return r

	ra, rb = ranks(a), ranks(b)
	return 1 - 6 * sum((p - q) ** 2 for p, q in zip(ra, rb)) / (n * (n * n - 1))


def persistence(histories: dict, priority, lags=LAGS, min_sites: int = MIN_SITES_FOR_RHO) -> list[LagStat]:
	"""How stable is the ranking over time?

	For each lag (in steps of the observed month series), the mean Spearman
	rho between site P90s at month m and month m+lag, the share of pairs
	where the month-m leader is still top 3 at m+lag, and the chance
	baseline (3/n). Month pairs with fewer than ``min_sites`` sites are
	skipped.
	"""
	p = normalize_priority(priority)
	hist = _p90s(histories)
	months = month_keys(hist)
	out: list[LagStat] = []
	for lag in lags:
		rhos: list[float] = []
		kept: list[bool] = []
		chance: list[float] = []
		for i in range(len(months) - lag):
			m0, m1 = months[i], months[i + lag]
			ids = [k for k, h in hist.items()
				if h.get(m0, {}).get(p) is not None and h.get(m1, {}).get(p) is not None]
			if len(ids) < min_sites:
				continue
			x = [hist[k][m0][p] for k in ids]
			y = [hist[k][m1][p] for k in ids]
			rhos.append(spearman(x, y))
			leader = ids[x.index(min(x))]
			top3 = sorted(ids, key=lambda k: hist[k][m1][p])[:3]
			kept.append(leader in top3)
			chance.append(3 / len(ids))
		if rhos:
			out.append(LagStat(lag, statistics.mean(rhos), statistics.mean(kept),
				statistics.mean(chance), len(rhos)))
	return out


def site_series(h: History, months: int = 0) -> list[tuple[str, dict[int, Optional[float]]]]:
	"""One site's history oldest first; ``months`` > 0 keeps only the newest ``months``."""
	keys = sorted(h)
	if months and months > 0:
		keys = keys[-months:]
	return [(k, h[k]) for k in keys]


# ---------------------------------------------------------------- one-shot

def analyze(postal_code: str = DEFAULT_POSTAL, radius_km: float = DEFAULT_RADIUS_KM,
		service: Union[Service, str] = "CT", priority=4, today: Optional[date] = None,
		progress: Optional[ProgressFn] = None) -> Analysis:
	"""Run the whole pipeline for one search."""
	code = normalize_postal(postal_code)
	svc = as_service(service)
	radius = normalize_radius(radius_km)
	prio = normalize_priority(priority) if svc.has_history else None
	today = today or date.today()

	def say(msg: str) -> None:
		if progress:
			progress(msg)

	say(f"Looking up {code}")
	lat, lng = geocode(code)
	if svc.is_screening:
		say(f"Finding breast screening sites within {radius:g} km")
		scr, complete = screening_search(lat, lng, radius)
		if not scr:
			raise ApiError(f"No breast screening sites found within {radius:g} km of {code}.")
		updated = max((s.last_updated for s in scr if s.last_updated), default=None)
		return Analysis(code, lat, lng, svc, None, radius, today, None, format_date(updated), screening=scr,
			screening_complete=complete)

	say(f"Finding {svc.label} sites within {radius:g} km")
	sites = nearby_sites(lat, lng, radius, svc)
	if not sites:
		raise ApiError(f"No sites report {svc.label} within {radius:g} km of {code}.")
	hist = fetch_histories(sites + [PROVINCE_SITE], svc, progress)
	province = hist.pop(PROVINCIAL_ID, None)
	say("Ranking")
	p90 = _p90s(hist)
	months = month_keys(p90)
	rows = rank(sites, p90, prio)
	province_row = summarize(PROVINCE_SITE, province.p90, prio, months) if province else None
	stats = persistence(p90, prio)
	latest = months[-1] if months else None
	ranked = {r.site.id for r in rows}
	no_data = [Row(s, None, None, 0) for s in sites if s.id not in ranked]
	return Analysis(code, lat, lng, svc, prio, radius, today, latest, month_long(latest),
		sites, hist, province, province_row, rows, stats, no_data=no_data)


def write_csv(path, a: Analysis) -> None:
	"""Export the results (and persistence stats, when available) to one CSV file."""
	with open(path, "w", newline="", encoding="utf-8-sig") as f:
		w = csv.writer(f)
		w.writerow(["source", SOURCE_URL, "ontario_health_last_updated", a.last_updated])
		w.writerow(["postal_code", a.postal_code, "service", a.service.label, "priority",
			a.priority if a.priority is not None else "", "radius_km", a.radius_km])
		w.writerow([])
		if a.service.is_screening:
			w.writerow(["rank", "site", "address", "km", "estimated_wait", "wait_days", "phone", "ext",
				"toll_free", "wheelchair_accessible", "languages", "last_updated"])
			for i, s in enumerate(a.screening, 1):
				w.writerow([i, s.name, s.full_address, f"{s.distance_km:.1f}", s.wait_text,
					"" if s.wait_days is None else f"{s.wait_days:g}", s.phone, s.ext, s.toll_free,
					"yes" if s.accessible else "no", s.languages,
					s.last_updated.isoformat() if s.last_updated else ""])
			return
		w.writerow(["rank", "site_id", "site", "address", "km", "latest_p90_wait_days",
			"median_12mo_p90_wait_days", "pct_within_target_latest", "pct_within_target_12mo", "recording_since",
			"main_phone", "booking_phone", "fax"])

		def line(rank_no, r: Row, h: Optional[SiteHistory]):
			first, pct, pct12 = None, None, None
			if h is not None:
				first = h.recording_since(a.priority)[0]
				pct = h.within_latest(a.priority, a.latest_month)[1]
				pct12 = h.within_period(a.priority, a.window)
			c = None if r.is_province else contact_for(r.site.id)
			fax = None
			if c is not None:
				fax = c.surgery_fax if a.service.is_surgery else c.imaging_fax(a.service.modality)
			w.writerow([rank_no, r.site.id, r.site.name, r.site.full_address,
				"" if r.is_province else f"{r.site.distance_km:.1f}",
				"" if r.latest_p90 is None else f"{r.latest_p90:g}",
				"" if r.median_p90 is None else f"{r.median_p90:g}",
				"" if pct is None else f"{pct:g}", "" if pct12 is None else f"{pct12:.0f}",
				month_label(first) if first else "",
				(c.main_phone or "") if c else "", (c.di_phone or "") if c and not a.service.is_surgery else "",
				fax or ""])

		if a.province_row:
			line("", a.province_row, a.province)
		for i, r in enumerate(a.rows, 1):
			line(i, r, a.histories.get(r.site.id))
		for r in a.no_data:
			line("", r, a.histories.get(r.site.id))
		w.writerow([])
		w.writerow(["lag_months", "mean_rho", "leader_top3_share", "chance_share", "pairs"])
		for s in a.lag_stats:
			w.writerow([s.lag_months, f"{s.mean_rho:.3f}", f"{s.leader_top3_share:.3f}",
				f"{s.chance_share:.3f}", s.pairs])
