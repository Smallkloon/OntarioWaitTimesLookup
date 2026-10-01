"""Tests for ctwaits.core against recorded fixtures and small synthetic cases."""
from __future__ import annotations

import json
from datetime import date

import pytest

from ctwaits import core

GEO = "/city/postalcode/"
LAT, LNG = 43.651801, -79.38256  # Toronto City Hall, M5H 2N2
CT = core.Service("CT")
KNEE = core.Procedure(49, "Knee Replacement", 3, 2)
KNEE_W2 = core.Service(core.SURGERY, KNEE, 2)


def site_row(sid: int, dist: float, name: str | None = None) -> dict:
	"""A list-endpoint row. Carries (bogus) wait figures on purpose."""
	return {"Id": sid, "Name": name or f"Site {sid}", "Address1": "1 Main St", "City": "Toronto",
		"PostalCode": "M5H2N2", "Distance": dist, "Latitude": 43.0, "Longitude": -79.0,
		"WaitTimes": [{"PriorityId": 4, "WaitTime90percentile": "999.00"}]}


def pages_by_number(api, pages: dict, kind: str = "/wtdata/") -> None:
	"""Route list-endpoint pages by the page segment that follows lat/lng."""
	def match(u, n):
		if kind not in u:
			return False
		parts = u.split("/")
		i = next(i for i, x in enumerate(parts) if x in (str(LAT), "0", "0.0") and i + 2 < len(parts))
		return parts[i + 2] == str(n)
	for n, rows in pages.items():
		api.add(lambda u, n=n: match(u, n), rows)


def H(values, start_year=2025, start_month=1, priority=4) -> core.History:
	"""Synthetic history: consecutive months with ``values`` for one priority."""
	out: core.History = {}
	y, m = start_year, start_month
	for v in values:
		out[f"{y}{m:02d}"] = {p: (v if p == priority else None) for p in core.PRIORITIES}
		m += 1
		if m > 12:
			y, m = y + 1, 1
	return out


def S(sid: int, dist: float = 1.0) -> core.Site:
	return core.Site(sid, f"Site {sid}", "1 Main St", "Toronto", "M5H2N2", dist, 43.0, -79.0)


# ---------------------------------------------------------------- RI / LV / NV / null parsing

@pytest.mark.parametrize("raw,expected", [
	("84.00", 84.0), ("1.00", 1.0), ("92.0000000000000000", 92.0),
	("RI", None), ("LV", None), ("NV", None), ("NS", None), ("", None), (None, None),
])
def test_parse_number(raw, expected):
	assert core.parse_number(raw) == expected


def test_history_maps_ri_and_missing_to_none(api, fixtures):
	raw = fixtures("wtchartdata-ct-1358-ri.json")
	api.add("/wtchartdata/EN/adult/CT/1/1358", raw)
	h = core.history(1358, "CT")
	assert len(h) == len(raw)
	nones = floats = 0
	for m in raw:
		by_p = {x["PriorityId"]: x.get("WaitTime90percentile") for x in m["WaitTimes"]}
		for p in core.PRIORITIES:
			got = h[m["Key"]][p]
			if core.parse_number(by_p.get(p)) is None:
				assert got is None
				nones += 1
			else:
				assert got == float(by_p[p])
				floats += 1
	assert nones > 0 and floats > 0
	assert set(next(iter(h.values()))) == set(core.PRIORITIES)  # combined 234 is dropped


def test_history_handles_missing_priority_and_short_series(api):
	api.add("/wtchartdata/", [
		{"Key": "202606", "WaitTimes": [{"PriorityId": 4, "WaitTime90percentile": "30.00",
			"WaitTimePercentWithinTarget": "55.00", "Target": "28"}]},
		{"Key": "202605", "WaitTimes": []},
		{"Key": "202604", "WaitTimes": [{"PriorityId": "3", "WaitTime90percentile": "LV"}, {"PriorityId": None}]},
		{"Key": "202603"},
	])
	h = core.site_history(1, "ct")
	assert h.p90 == {
		"202606": {2: None, 3: None, 4: 30.0},
		"202605": {2: None, 3: None, 4: None},
		"202604": {2: None, 3: None, 4: None},
		"202603": {2: None, 3: None, 4: None},
	}
	assert h.within["202606"][4] == 55.0
	assert h.targets == {2: None, 3: None, 4: 28.0}
	assert h.recording_since(4) == ("202606", 1, 1)
	assert h.recording_since(2) == (None, 0, 0)


def test_queensway_fixture_full_history_and_within_target(api, fixtures):
	raw = fixtures("wtchartdata-ct-4985.json")
	api.add("/wtchartdata/EN/adult/CT/1/4985", raw)
	h = core.site_history(4985, CT)
	assert len(h.months()) == 55 and h.months()[0] == "202201" and h.months()[-1] == "202607"
	assert h.p90["202607"] == {2: 1.0, 3: 43.0, 4: 84.0}
	assert h.within_latest(4) == ("202607", 27.0)
	assert h.targets == {2: 2.0, 3: 10.0, 4: 28.0}
	first, reported, total = h.recording_since(4)
	assert first == "202201" and total == 55 and reported <= 55
	assert [k for k, _ in core.site_series(h.p90)] == sorted(h.p90)
	assert [k for k, _ in core.site_series(h.p90, 12)] == sorted(h.p90)[-12:]


def test_within_mean_ignores_missing_months():
	h = core.SiteHistory(within={"202601": {4: 40.0}, "202602": {4: None}, "202603": {4: 60.0}})
	assert h.within_mean(4, ["202601", "202602", "202603"]) == 50.0
	assert h.within_mean(4, ["202602"]) is None
	assert h.within_latest(4) == ("202603", 60.0)
	assert h.within_latest(4, "202602") == ("202602", None)


# ---------------------------------------------------------------- geocode

def test_geocode_from_fixture(api, fixtures):
	api.add(GEO, fixtures("postalcode-m5h2n2.json"))
	assert core.geocode("m5h 2n2") == (LAT, LNG)
	assert api.calls == [f"{core.BASE}/city/postalcode/M5H2N2"]


@pytest.mark.parametrize("bad", ["", "M5H", "12345", "M5H 2N", "ABCDEF", None, "M5H2N2X"])
def test_geocode_rejects_malformed_codes_without_a_request(api, bad):
	with pytest.raises(ValueError):
		core.geocode(bad)
	assert api.calls == []


def test_geocode_rejects_codes_outside_ontario(api):
	api.add(GEO, {"PostalCode": "H2X 1Y4", "Province": "QC", "Latitude": 45.5, "Longitude": -73.6, "InOntario": False})
	with pytest.raises(ValueError, match="not in Ontario"):
		core.geocode("H2X1Y4")


# ---------------------------------------------------------------- URLs per service

def test_service_urls():
	assert CT.list_url(LAT, LNG, 2) == f"{core.BASE}/DiagnosticImaging/wtdata/EN/adult/{LAT}/{LNG}/2/CT/1"
	assert core.Service("MRI").chart_url(7) == f"{core.BASE}/DiagnosticImaging/wtchartdata/EN/adult/MRI/1/7"
	assert KNEE_W2.list_url(LAT, LNG, 1) == f"{core.BASE}/Surgical/wtsurgicaldata/EN/adult/{LAT}/{LNG}/1/3/2/2"
	assert core.Service(core.SURGERY, KNEE, 1).chart_url(-354) == \
		f"{core.BASE}/Surgical/wtsurgicalchartdata/EN/adult/3/2/1/-354"
	with pytest.raises(ValueError):
		core.Service("BREAST").chart_url(1)
	with pytest.raises(ValueError):
		core.Service(core.SURGERY)
	with pytest.raises(ValueError):
		core.Service(core.SURGERY, KNEE, 3)
	assert KNEE_W2.label == "Knee Replacement, Decision to surgery (Wait 2)"
	assert (CT.within_verb, KNEE_W2.within_verb, core.Service(core.SURGERY, KNEE, 1).within_verb) == \
		("scanned", "treated", "seen")


# ---------------------------------------------------------------- provincial row, SickKids

def test_nearby_drops_provincial_row_and_sickkids(api, fixtures):
	p1, p2 = fixtures("wtdata-page1.json"), fixtures("wtdata-page2.json")
	assert p1[0]["Id"] == core.PROVINCIAL_ID  # the recorded response really starts with it
	assert any(r["Id"] == core.SICKKIDS_ID for r in p1)
	pages_by_number(api, {1: p1, 2: p2, 3: []})
	sites = core.nearby_sites(LAT, LNG, 40, "CT")
	ids = [s.id for s in sites]
	assert core.PROVINCIAL_ID not in ids
	assert core.SICKKIDS_ID not in ids
	assert ids[0] == 4831 and sites[0].name.startswith("Unity Health Toronto-St. Michael")
	assert all(s.distance_km <= 40 for s in sites)
	assert [s.distance_km for s in sites] == sorted(s.distance_km for s in sites)
	assert len(ids) == len(set(ids))
	assert all("/wtdata/EN/adult/" in u and u.endswith("/CT/1") for u in api.calls)


def test_ct_site_list_fixture_uses_the_modality_segment(api, fixtures):
	p1 = fixtures("wtdata-ct-page1.json")
	pages_by_number(api, {1: p1, 2: []})
	sites = core.nearby_sites(LAT, LNG, 15, "CT")
	assert [s.id for s in sites][:2] == [4831, 1359]
	assert sites[0].full_address == "30 Bond Street, Toronto M5B 1W8"


def test_surgical_site_list_from_fixture(api, fixtures):
	p1 = fixtures("surgical-list-knee-wait2-page1.json")
	pages_by_number(api, {1: p1, 2: []}, kind="/wtsurgicaldata/")
	sites = core.nearby_sites(LAT, LNG, 40, KNEE_W2)
	assert core.PROVINCIAL_ID not in [s.id for s in sites]
	assert sites[0].id == 4831
	assert all("/Surgical/wtsurgicaldata/EN/adult/" in u and u.endswith("/3/2/2") for u in api.calls)


# ---------------------------------------------------------------- pagination stop

def test_pagination_stops_when_last_distance_exceeds_radius(api):
	pages_by_number(api, {
		1: [site_row(core.PROVINCIAL_ID, 0, "Ontario"), site_row(1, 1.0), site_row(2, 2.0)],
		2: [site_row(3, 10.0), site_row(4, 50.0)],
		3: [site_row(5, 60.0)],
	})
	sites = core.nearby_sites(LAT, LNG, 40)
	assert [s.id for s in sites] == [1, 2, 3]
	assert len(api.calls) == 2  # page 3 is never requested


def test_pagination_stops_when_page_has_no_new_ids(api):
	pages_by_number(api, {
		1: [site_row(1, 1.0), site_row(2, 2.0)],
		2: [site_row(2, 2.0), site_row(1, 1.0)],  # the API repeats itself past the end
		3: [site_row(9, 3.0)],
	})
	assert [s.id for s in core.nearby_sites(LAT, LNG, 40)] == [1, 2]
	assert len(api.calls) == 2


def test_pagination_stops_on_empty_page(api):
	pages_by_number(api, {1: [site_row(1, 1.0)], 2: []})
	assert [s.id for s in core.nearby_sites(LAT, LNG, 40)] == [1]
	assert len(api.calls) == 2


def test_pagination_keeps_only_sites_inside_radius_on_last_page(api):
	pages_by_number(api, {1: [site_row(1, 39.9), site_row(2, 40.0), site_row(3, 40.1)]})
	assert [s.id for s in core.nearby_sites(LAT, LNG, 40)] == [1, 2]
	assert len(api.calls) == 1


# ---------------------------------------------------------------- breast screening

def test_screening_sites_from_fixture(api, fixtures):
	raw = fixtures("obsp-m5h2n2.json")
	api.add(core.OBSP_BASE, raw)
	sites = core.screening_sites(LAT, LNG, 5)
	assert sites and all(s.distance_km <= 5 for s in sites)
	assert len(api.calls) == 1 and "LocationType=PostalCode" in api.calls[0]
	dragon = next(s for s in sites if s.name.startswith("Dragon City"))
	assert dragon.phone == "416-603-1197"
	assert dragon.wait_text == "0 to 2 weeks" and dragon.wait_days == 6.0
	assert dragon.hours["monday"] == "8:00 am to 4:00 pm" and dragon.hours["sunday"] == "Closed"
	assert dragon.accessible and dragon.languages == "English"
	assert dragon.last_updated == date(2026, 9, 25)
	keys = [s.sort_key for s in sites]
	assert keys == sorted(keys)
	first_band = [s for s in sites if s.wait_text == sites[0].wait_text]
	assert [s.distance_km for s in first_band] == sorted(s.distance_km for s in first_band)


def _obsp_row(i, lat, lng, dist, days=10.0, excluded="N"):
	return {"id": f"s{i}", "name": f"Clinic {i}", "waitTimeDays": days, "waitTime": "2 to 4 weeks",
		"wtExcluded": excluded, "distance": dist, "condition": "ROUTE_EXISTS", "latitude": lat, "longitude": lng,
		"address": {"line_1": "1 Main", "city": "Toronto", "postalCode": "M5H 2N2"}, "phone1": "4165550100",
		"tollFree": "8005550199", "hours": {}, "accessibility": "N",
		"languages": {"english": True, "french": True}, "lastUpdated": "2026-09-01T10:00:00"}


def _fake_obsp(sites):
	"""Mimic the real API: the 50 sites nearest the query origin, by a 'driving'
	distance 1.3 times the straight line, whatever paging is asked for."""
	import urllib.parse

	def answer(url):
		q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
		la, ln = float(q["originLatitude"][0]), float(q["originLongitude"][0])
		ranked = sorted(sites, key=lambda r: core.haversine_km(la, ln, r["latitude"], r["longitude"]))[:50]
		return [dict(r, distance=round(1.3 * core.haversine_km(la, ln, r["latitude"], r["longitude"]), 2))
			for r in ranked]
	return answer


def test_screening_search_covers_the_radius_past_the_50_site_cap(api):
	sites = [_obsp_row(f"{i}-{j}", LAT + i * 0.02, LNG + j * 0.03, 0) for i in range(-15, 16) for j in range(-15, 16)]
	api.add(core.OBSP_BASE, _fake_obsp(sites))
	got, complete = core.screening_search(LAT, LNG, 15)
	want = {r["id"] for r in sites if core.haversine_km(LAT, LNG, r["latitude"], r["longitude"]) <= 15}
	assert complete and len(want) > 50
	assert {s.id for s in got} == want
	assert len(api.calls) > 1
	assert all(s.distance_km <= 15 for s in got)


def test_screening_search_routes_around_failing_origins(api):
	"""Origins over open water make the real API answer 500; the search must
	carry on with the neighbouring origins instead of failing."""
	sites = [_obsp_row(f"{i}-{j}", LAT + i * 0.02, LNG + j * 0.03, 0) for i in range(0, 16) for j in range(-15, 16)]
	good = _fake_obsp(sites)

	def answer(url):
		import urllib.parse
		q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
		if float(q["originLatitude"][0]) < LAT - 0.01:  # "lake" south of the origin
			raise core.ApiError("HTTP Error 500")
		return good(url)

	api.add(core.OBSP_BASE, answer)
	got, complete = core.screening_search(LAT, LNG, 15)
	want = {r["id"] for r in sites if core.haversine_km(LAT, LNG, r["latitude"], r["longitude"]) <= 15}
	assert complete and {s.id for s in got} == want


def test_screening_search_still_fails_when_the_origin_fails(api):
	def boom(url):
		raise core.ApiError("HTTP Error 500")
	api.add(core.OBSP_BASE, boom)
	with pytest.raises(core.ApiError):
		core.screening_search(LAT, LNG, 15)


def test_screening_search_reports_an_exhausted_budget(api, monkeypatch):
	sites = [_obsp_row(f"{i}-{j}", LAT + i * 0.01, LNG + j * 0.015, 0) for i in range(-30, 31) for j in range(-30, 31)]
	api.add(core.OBSP_BASE, _fake_obsp(sites))
	monkeypatch.setattr(core, "SCREENING_MAX_QUERIES", 3)
	got, complete = core.screening_search(LAT, LNG, 30)
	assert not complete and len(api.calls) == 3 and got


def test_screening_without_estimate_sorts_last(api):
	rows = [_obsp_row(i, LAT + i * 0.001, LNG, 1 + i / 100) for i in range(5)]
	rows[1] = _obsp_row(1, LAT + 0.001, LNG, 1.01, excluded="Y")
	rows[3] = _obsp_row(3, LAT + 0.003, LNG, 1.03, days=None)
	api.add(core.OBSP_BASE, rows)
	sites = core.screening_sites(LAT, LNG, 40)
	assert len(api.calls) == 1  # fewer than 50 back means the whole province was returned
	assert [s.id for s in sites][-2:] == ["s1", "s3"] and all(s.wait_days is None for s in sites[-2:])
	assert sites[-1].wait_text == "Not available"
	assert sites[0].toll_free == "1-800-555-0199" and sites[0].languages == "English and French"


# ---------------------------------------------------------------- surgery procedures

def test_procedures_from_fixture(api, fixtures):
	api.add("/surgery/getsurgeriesbyname/surgical/EN/adult/", fixtures("procedures-adult.json"))
	procs = core.procedures()
	assert len(procs) >= 150
	assert [p.name.casefold() for p in procs] == sorted(p.name.casefold() for p in procs)
	knee = core.get_procedure(49, procs)
	assert (knee.name, knee.modality_type_id, knee.modality_id) == ("Knee Replacement", 3, 2)
	hits = core.find_procedures("knee", procs)
	assert hits[0].name.startswith("Knee") and all("knee" in p.name.lower() for p in hits)
	assert core.find_procedures("hip replace", procs)
	assert core.find_procedures("", procs) == procs
	with pytest.raises(ValueError):
		core.get_procedure(999999, procs)


# ---------------------------------------------------------------- Spearman

def test_spearman_hand_computed():
	# ranks of a: 0 1 2 3 4; ranks of b: 0 2 1 4 3; d^2 = 0 1 1 1 1 = 4
	# rho = 1 - 6*4 / (5 * (25 - 1)) = 1 - 24/120 = 0.8
	assert core.spearman([1, 2, 3, 4, 5], [1, 3, 2, 5, 4]) == pytest.approx(0.8)
	# ranks of a: 2 0 1 3; ranks of b: 0 1 2 3; d^2 = 4 1 1 0 = 6
	# rho = 1 - 36 / (4 * 15) = 0.4
	assert core.spearman([3, 1, 2, 4], [1, 2, 3, 4]) == pytest.approx(0.4)
	assert core.spearman([10, 20, 30], [1, 2, 3]) == pytest.approx(1.0)
	assert core.spearman([10, 20, 30], [3, 2, 1]) == pytest.approx(-1.0)
	assert core.spearman([84, 90, 120], [1.5, 2.5, 99.0]) == pytest.approx(1.0)


def test_spearman_rejects_bad_input():
	with pytest.raises(ValueError):
		core.spearman([1], [1])
	with pytest.raises(ValueError):
		core.spearman([1, 2], [1])


# ---------------------------------------------------------------- ranking

def test_rank_uses_the_last_12_months():
	h = H([1] + [10 * i for i in range(1, 13)], start_year=2024, start_month=12)
	(r,) = core.rank([S(1)], {1: h}, 4)
	assert r.months_available == 12
	assert r.latest_p90 == 120
	assert r.median_p90 == 65


def test_rank_handles_ri_months_and_short_series():
	h = H([None] * 8 + [40, 60, 50, None])  # UHN-style short series
	(r,) = core.rank([S(1)], {1: core.SiteHistory(p90=h)}, 4)
	assert r.months_available == 3 and r.latest_p90 is None and r.median_p90 == 50


def test_rank_sorts_by_median_then_latest_and_omits_empty_sites():
	hist = {
		1: H([50] * 12),
		2: H([None] * 9 + [40, 60, None]),
		3: H([30] * 12),
		4: H([None] * 12),
		5: H([30] * 11 + [10]),
	}
	sites = [S(i, dist=i) for i in (1, 2, 3, 4, 5)]
	assert [r.site.id for r in core.rank(sites, hist, 4)] == [5, 3, 1, 2]
	assert core.rank(sites, {}, 4) == []


# ---------------------------------------------------------------- persistence

def test_persistence_perfect_and_reversed():
	hist = {i: H([i, i, i]) for i in range(1, 7)}
	stats = core.persistence(hist, 4, lags=(1, 2))
	assert [s.lag_months for s in stats] == [1, 2]
	assert [s.pairs for s in stats] == [2, 1]
	for s in stats:
		assert s.mean_rho == pytest.approx(1.0)
		assert s.leader_top3_share == 1.0
		assert s.chance_share == pytest.approx(0.5)
	hist = {i: H([i, 7 - i]) for i in range(1, 7)}
	(s,) = core.persistence(hist, 4, lags=(1,))
	assert s.mean_rho == pytest.approx(-1.0)
	assert s.leader_top3_share == 0.0


def test_persistence_skips_months_with_fewer_than_six_sites():
	assert core.persistence({i: H([i, i]) for i in range(1, 6)}, 4) == []
	assert [s.lag_months for s in core.persistence({i: H([i, i]) for i in range(1, 7)}, 4)] == [1]
	assert core.persistence({}, 4) == []


# ---------------------------------------------------------------- end to end, and the wtdata guard

def _chart_for(url: str):
	sid = int(url.rstrip("/").rsplit("/", 1)[1])
	v = f"{abs(sid) % 97 + 1}.00"
	return [{"Key": k, "WaitTimes": [{"PriorityId": p, "WaitTime90percentile": v, "WaitTimePercentWithinTarget": "50",
		"Target": "28"} for p in (2, 3, 4, 234)]} for k in ("202607", "202606")]


def test_wait_numbers_never_come_from_list_endpoints(api, fixtures):
	"""List rows carry wait figures (for wtdata, MRI ones when the modality
	segment is wrong). Every number the ranking reports must come from the
	per-site history endpoint."""
	api.add(GEO, fixtures("postalcode-m5h2n2.json"))
	p1 = fixtures("wtdata-ct-page1.json")
	assert any(r.get("WaitTimes") for r in p1)  # the trap is really in the recorded response
	pages_by_number(api, {1: p1, 2: []})
	api.add("/wtchartdata/EN/adult/CT/1/", _chart_for)

	a = core.analyze("M5H2N2", 15, "CT", 4, today=date(2026, 10, 1))
	assert a.rows and a.latest_month == "202607" and a.last_updated == "July 2026"
	for r in a.rows:
		assert r.latest_p90 == r.site.id % 97 + 1
		assert r.median_p90 == r.site.id % 97 + 1
	assert a.province_row is not None and a.province_row.latest_p90 == 354 % 97 + 1
	assert a.province_row.site.name == core.PROVINCIAL_NAME
	assert len(api.urls("/wtchartdata/")) == len(a.sites) + 1  # one per site plus the province
	assert api.urls("/wtchartdata/EN/adult/CT/1/-354")
	assert not any("wait" in f.lower() for f in core.Site.__dataclass_fields__)


def test_analyze_surgery_with_provincial_line(api, fixtures):
	api.add(GEO, fixtures("postalcode-m5h2n2.json"))
	pages_by_number(api, {1: fixtures("surgical-list-knee-wait2-page1.json"), 2: []}, kind="/wtsurgicaldata/")
	api.add("/wtsurgicalchartdata/EN/adult/3/2/2/-354", fixtures("surgical-chart-knee-wait2-ontario.json"))
	api.add("/wtsurgicalchartdata/EN/adult/3/2/2/3742", fixtures("surgical-chart-knee-wait2-3742.json"))
	api.add("/wtsurgicalchartdata/", _chart_for)
	a = core.analyze("M5H2N2", 15, KNEE_W2, 4, today=date(2026, 10, 1))
	assert a.province is not None and a.province.p90["202607"][4] == 236.0
	assert a.province_row.latest_p90 == 236.0
	sinai = next(r for r in a.rows if r.site.id == 3742)
	assert sinai.latest_p90 == 176.0
	d = a.to_dict()
	assert d["service"]["procedure"] == {"id": 49, "name": "Knee Replacement"}
	assert d["ontario_average"]["latest_p90_days"] == 236.0
	assert d["rows"][0]["recording_since"] is not None


def test_analyze_breast_screening(api, fixtures):
	api.add(GEO, fixtures("postalcode-m5h2n2.json"))
	api.add(core.OBSP_BASE, fixtures("obsp-m5h2n2.json"))
	a = core.analyze("M5H2N2", 5, "breast screening", 4)
	assert a.priority is None and a.screening and not a.rows and a.province_row is None
	assert a.last_updated  # e.g. "September 25, 2026"
	assert a.to_dict()["rows"][0]["phone"]
	assert a.screening_capped == (len(a.screening) >= 50)
	assert not api.urls("/wtchartdata/")


def test_fetch_histories_preserves_site_order(api):
	api.add("/wtchartdata/", lambda u: [{"Key": "202607", "WaitTimes": []}])
	out = core.fetch_histories([S(i, dist=i) for i in (5, 3, 9, 1)], "CT")
	assert list(out) == [5, 3, 9, 1]
	assert len(api.urls("/wtchartdata/")) == 4
	assert core.fetch_histories([], "CT") == {}


# ---------------------------------------------------------------- misc

def test_validation_and_formatting_helpers():
	with pytest.raises(ValueError):
		core.normalize_priority(5)
	with pytest.raises(ValueError):
		core.normalize_priority("x")
	assert core.normalize_priority("4") == 4
	with pytest.raises(ValueError):
		core.normalize_modality("xray")
	assert core.normalize_modality(" mri ") == "MRI"
	assert core.normalize_modality("Breast screening") == "BREAST"
	assert core.normalize_modality("obsp") == "BREAST"
	with pytest.raises(ValueError):
		core.normalize_wait(3)
	with pytest.raises(ValueError):
		core.normalize_radius(0)
	assert core.month_label("202607") == "2026-07"
	assert core.month_long("202607") == "July 2026"
	assert core.month_short("202601") == "Jan 2026"
	assert core.month_label(None) == ""
	assert core.format_date(date(2026, 9, 25)) == "September 25, 2026"
	assert core.format_phone("4166031197") == "416-603-1197"
	assert core.format_hour("1630") == "4:30 pm" and core.format_hour("0000") == "12:00 am"


def test_write_csv(tmp_path):
	s = core.Site(1, "A", "1 Main", "Toronto", "M5H2N2", 1.0, 0, 0)
	h = core.SiteHistory(p90={"202607": {2: None, 3: None, 4: 84.0}}, within={"202607": {2: None, 3: None, 4: 27.0}})
	a = core.Analysis("M5H2N2", 0, 0, CT, 4, 40.0, date(2026, 10, 1), "202607", "July 2026", [s], {1: h},
		core.SiteHistory(p90={"202607": {2: 1.0, 3: 67.0, 4: 184.0}}),
		core.Row(core.PROVINCE_SITE, 184.0, 180.0, 12), [core.Row(s, 84.0, 90.0, 12)],
		[core.LagStat(1, 0.88, 0.7, 0.12, 54)])
	p = tmp_path / "out.csv"
	core.write_csv(p, a)
	text = p.read_text(encoding="utf-8-sig")
	assert "ontario_health_last_updated,July 2026" in text
	assert ",-354,Ontario provincial average,,,184,180,,,2026-07,,," in text
	assert "1,1,A,\"1 Main, Toronto M5H 2N2\",1.0,84,90,27,27,2026-07,,," in text
	assert "1,0.880,0.700,0.120,54" in text
	assert "deadline" not in text


def test_within_period_weights_by_cases():
	h = core.SiteHistory(within={"202601": {4: 20.0}, "202602": {4: 80.0}, "202603": {4: None}},
		cases={"202601": {4: 300.0}, "202602": {4: 100.0}, "202603": {4: 50.0}})
	assert h.within_period(4, ["202601", "202602", "202603"]) == pytest.approx(35.0)  # (20*300 + 80*100) / 400
	h.cases = {}
	assert h.within_period(4, ["202601", "202602"]) == pytest.approx(50.0)  # plain mean without counts
	assert h.within_period(4, ["202603"]) is None


def test_sites_without_recent_data_are_kept(api, fixtures):
	api.add(GEO, fixtures("postalcode-m5h2n2.json"))
	pages_by_number(api, {1: [site_row(1, 1.0), site_row(2, 2.0)], 2: []})

	def chart(url):
		sid = int(url.rsplit("/", 1)[1])
		v = "LV" if sid == 2 else "40.00"
		return [{"Key": "202607", "WaitTimes": [{"PriorityId": 4, "WaitTime90percentile": v}]}]

	api.add("/wtchartdata/", chart)
	a = core.analyze("M5H2N2", 40, "CT", 4)
	assert [r.site.id for r in a.rows] == [1]
	assert [r.site.id for r in a.no_data] == [2] and a.no_data[0].median_p90 is None
	assert a.to_dict()["sites_without_recent_data"][0]["site_id"] == 2


def test_contacts_loader(tmp_path, monkeypatch):
	f = tmp_path / "contacts.json"
	f.write_text(json.dumps({"checked": "2026-10-01", "sites": [
		{"id": 4985, "main_phone": "905-555-0100", "di_phone": "905-555-0101", "di_fax": "905-555-0102",
			"mri_fax": None, "surgery_fax": "", "source_urls": ["https://example.org/di"], "confidence": "high",
			"notes": "Central booking"},
		{"id": "bad"},
	]}), encoding="utf-8")
	monkeypatch.setattr(core, "CONTACTS_FILE", f)
	core.load_contacts.cache_clear()
	try:
		c = core.contact_for(4985)
		assert c.main_phone == "905-555-0100" and c.surgery_fax is None and c.checked == "2026-10-01"
		assert c.imaging_fax("CT") == "905-555-0102" and c.imaging_fax("MRI") == "905-555-0102"
		assert core.contact_for(1) is None and len(core.load_contacts()) == 1
		assert c.regional_intake == () and c.msk_intake == () and c.verified
	finally:
		core.load_contacts.cache_clear()


def test_contacts_intakes(tmp_path, monkeypatch):
	f = tmp_path / "contacts.json"
	f.write_text(json.dumps({"checked": "2026-10-01", "sites": [{
		"id": 4834, "main_phone": "519-555-0100", "di_fax": "519-555-0102", "mri_fax": "519-555-0103",
		"regional_intake": [{"fax": "365-317-9260", "label": "Ontario Health West central intake",
			"modalities": ["CT", "MRI"], "note": "Outpatient only.", "source": "https://example.org/hub"}],
		"msk_intake": [{"fax": "855-346-9138", "label": "Hip and knee central intake", "source": "https://example.org/msk"}],
		"verified": False, "source_urls": ["https://example.org/di"], "confidence": "high"}]}), encoding="utf-8")
	monkeypatch.setattr(core, "CONTACTS_FILE", f)
	core.load_contacts.cache_clear()
	try:
		c = core.contact_for(4834)
		assert [i.fax for i in c.intake_for("MRI")] == ["365-317-9260"] and c.intake_for("BREAST") == []
		assert c.intake_named("365-317-9260").label.startswith("Ontario Health West") and c.intake_named("519-555-0102") is None
		assert c.imaging_fax("MRI") == "519-555-0103" and not c.verified
		d = c.to_dict()
		assert d["regional_intake"][0]["modalities"] == ["CT", "MRI"] and d["msk_intake"][0]["fax"] == "855-346-9138"
		assert json.dumps(d)
	finally:
		core.load_contacts.cache_clear()
