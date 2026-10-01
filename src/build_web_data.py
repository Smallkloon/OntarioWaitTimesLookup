"""Build the data files the static web version (docs/) loads.

- docs/contacts.js: the researched hospital phone and fax table (ctwaits/contacts.json).
- docs/screening.js: a province-wide snapshot of Ontario Breast Screening Program
  sites. The breast screening API only answers pages on www.ontariohealth.ca, so a
  browser on another site cannot call it; this snapshot stands in for it.

The breast screening API returns only the 50 sites nearest the point it is
given. The snapshot starts from a spread of Ontario towns and then queries from
every site it finds, until no query turns up a new site.

Run from the project root: python src/build_web_data.py
A scheduled GitHub Action (.github/workflows/refresh-web-data.yml) runs it daily.
"""
from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ctwaits import core  # noqa: E402

DOCS = ROOT / "docs"
MAX_QUERIES = 2000
SEEDS = [  # (lat, lng) of towns spread across Ontario
	(43.6532, -79.3832), (45.4215, -75.6972), (43.2557, -79.8711), (42.9849, -81.2453), (42.3149, -83.0364),
	(43.4516, -80.4925), (46.4917, -80.9930), (48.3809, -89.2477), (44.2312, -76.4860), (44.3894, -79.6903),
	(44.3091, -78.3197), (46.5136, -84.3358), (46.3091, -79.4608), (48.4758, -81.3305), (49.7670, -94.4894),
	(42.9745, -82.4066), (44.5690, -80.9406), (43.0896, -79.0849), (43.8971, -78.8658), (44.1628, -77.3832),
	(45.0213, -74.7303), (45.8263, -77.1167), (49.7833, -92.8333), (50.0975, -91.9213), (51.2734, -80.6438),
	(49.4155, -82.4334), (45.3269, -79.2167), (45.3435, -80.0352), (44.6082, -79.4197), (48.7639, -86.5937),
	(49.6833, -83.6667), (47.5081, -79.6828), (50.4167, -86.9333), (51.7333, -91.8167), (52.9333, -82.4167),
]
KEEP = ("id", "name", "waitTimeDays", "waitTime", "wtExcluded", "latitude", "longitude", "address", "phone1",
	"ext1", "tollFree", "hours", "accessibility", "languages", "lastUpdated")


def write_js(path: Path, var: str, data) -> None:
	text = f"window.{var} = " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + ";\n"
	path.write_text(text, encoding="utf-8")
	print(f"wrote {path.relative_to(ROOT)} ({path.stat().st_size:,} bytes)")


def build_contacts() -> None:
	data = json.loads((ROOT / "ctwaits" / "contacts.json").read_text(encoding="utf-8"))
	write_js(DOCS / "contacts.js", "CT_CONTACTS", data)


def build_screening() -> None:
	core.CACHE_DIR = None  # always fresh
	found: dict[str, dict] = {}
	asked: set[tuple[float, float]] = set()
	todo = list(dict.fromkeys((round(a, 4), round(b, 4)) for a, b in SEEDS))
	failures = 0
	t0 = time.time()

	def query(pt):
		# The API returns HTTP 500 under bursts of parallel requests; back off and retry.
		for attempt in range(4):
			try:
				return core._obsp_nearest(*pt)
			except core.ApiError:
				time.sleep(0.5 * (attempt + 1))
		return None

	with ThreadPoolExecutor(max_workers=3) as ex:
		while todo and len(asked) < MAX_QUERIES:
			todo = todo[: MAX_QUERIES - len(asked)]
			asked.update(todo)
			nxt = []
			for rows in ex.map(query, todo):
				if rows is None:
					failures += 1
					continue
				for r in rows:
					sid = str(r.get("id"))
					if sid not in found:
						found[sid] = r
						pt = (round(float(r.get("latitude") or 0), 4), round(float(r.get("longitude") or 0), 4))
						if pt not in asked and pt != (0.0, 0.0):
							nxt.append(pt)
			todo = list(dict.fromkeys(nxt))
	if not found:
		raise SystemExit("No breast screening sites came back; leaving the old snapshot in place.")
	complete = not todo
	sites = [{k: r.get(k) for k in KEEP} for r in sorted(found.values(), key=lambda r: str(r.get("name")))]
	print(f"breast screening: {len(sites)} sites from {len(asked)} queries ({failures} failed) "
		f"in {time.time() - t0:.0f}s, complete={complete}")
	write_js(DOCS / "screening.js", "CT_SCREENING", {
		"generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
		"complete": complete,
		"sites": sites,
	})


if __name__ == "__main__":
	DOCS.mkdir(exist_ok=True)
	build_contacts()
	build_screening()
