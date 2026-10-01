"""MCP server for Ontario wait times, over stdio.

Run from the project root with ``python -m ctwaits.mcp_server``; the
PyInstaller build turns this file into ``ctwaits-mcp.exe``. Nothing in this
module may print to stdout: stdout is the MCP transport.
"""
from __future__ import annotations

import functools
from typing import Any, Optional

try:  # mcp 2.x renamed FastMCP to MCPServer; the API used here is the same
	from mcp.server.mcpserver import MCPServer as FastMCP
	from mcp.server.mcpserver.exceptions import ToolError
except ModuleNotFoundError:  # mcp 1.x
	from mcp.server.fastmcp import FastMCP  # type: ignore[no-redef]
	from mcp.server.fastmcp.exceptions import ToolError  # type: ignore[no-redef]

try:
	from ctwaits import core
except ImportError:  # run as a bare script from inside the package folder
	import core  # type: ignore


def clear_errors(fn):
	"""Turn validation and API failures into ToolError so the message reaches the
	model. mcp 2.x hides the text of any other exception behind a generic
	"Error executing tool" line."""
	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		try:
			return fn(*args, **kwargs)
		except (ValueError, core.ApiError) as e:
			raise ToolError(str(e)) from e
	return wrapper


INSTRUCTIONS = (
	"Ontario wait times near a postal code, from Ontario Health's public wait-time data "
	"(www.ontariohealth.ca). Covers CT, MRI and breast screening, and every surgical procedure "
	"Ontario Health reports (look procedures up with find_procedures, then pass procedure_id). "
	"CT, MRI and surgery figures are the 90th percentile (P90) wait in days by priority "
	"(P2 urgent, P3 semi-urgent, P4 non-urgent), monthly since January 2022; the Ontario provincial "
	"figure is site id -354. Breast screening has only a current estimated wait, plus phone numbers "
	"and hours. Hospital phone and fax numbers come from a researched table with sources, not from "
	"Ontario Health. Data is cached locally for 24 hours."
)

mcp = FastMCP("ct-waits", instructions=INSTRUCTIONS)


def _service(modality: str, procedure_id: Optional[int], surgery_wait: int) -> core.Service:
	if procedure_id is not None:
		return core.Service(core.SURGERY, core.get_procedure(procedure_id), core.normalize_wait(surgery_wait))
	return core.Service(core.normalize_modality(modality))


@mcp.tool()
@clear_errors
def find_procedures(query: str = "") -> list[dict]:
	"""Search the adult surgical procedures Ontario Health reports wait times for.

	query matches every word anywhere in the name, case-insensitive (for example "knee" or
	"hip replacement"); an empty query lists all procedures. Returns id and name; pass the id as
	procedure_id to the other tools.
	"""
	return [p.to_dict() for p in core.find_procedures(query)]


@mcp.tool()
@clear_errors
def find_sites(postal_code: str, radius_km: float = 40, modality: str = "CT",
		procedure_id: Optional[int] = None, surgery_wait: int = 2) -> list[dict]:
	"""List sites within radius_km of an Ontario postal code that report the chosen service, nearest first.

	modality is CT, MRI or breast screening; pass procedure_id (from find_procedures) to list
	surgical sites instead, with surgery_wait 2 (decision to surgery) or 1 (referral to first
	appointment). Hospital rows carry id, name, address and km; pass the id to site_history.
	Breast screening rows also carry the estimated wait, phone numbers and hours. Adult sites
	only; the postal code may be written with or without the space.
	"""
	svc = _service(modality, procedure_id, surgery_wait)
	lat, lng = core.geocode(postal_code)
	radius = core.normalize_radius(radius_km)
	if svc.is_screening:
		return [s.to_dict() for s in core.screening_sites(lat, lng, radius)]
	return [{"id": s.id, "name": s.name, "address": s.full_address, "km": round(s.distance_km, 1)}
		for s in core.nearby_sites(lat, lng, radius, svc)]


@mcp.tool()
@clear_errors
def rank_sites(postal_code: str, modality: str = "CT", priority: int = 4, radius_km: float = 40,
		procedure_id: Optional[int] = None, surgery_wait: int = 2) -> dict[str, Any]:
	"""Rank nearby sites for one service.

	modality is CT, MRI or breast screening; pass procedure_id (from find_procedures) to rank
	surgical sites, with surgery_wait 2 (decision to surgery) or 1 (referral to first appointment).
	priority is 2, 3 or 4 and is ignored for breast screening.

	CT, MRI and surgery rows are sorted by the 12-month median 90th percentile (P90) wait in days,
	then by the latest P90, and carry latest_p90_days, median_p90_days_12mo, pct_within_target_latest,
	target_days, recording_since and contact (main line, imaging booking line, requisition fax,
	surgical referral fax, source URLs and the date they were checked; null where none was found;
	confirm a fax number before sending patient information). ontario_average holds the same figures
	for the province. Sites with no figures in the last 12 months are listed separately in
	sites_without_recent_data. Breast screening rows are sorted by estimated wait and carry phone
	numbers and hours. The result also carries latest_month, ontario_health_last_updated and, for
	CT, MRI and surgery, the rank-persistence statistics.
	"""
	svc = _service(modality, procedure_id, surgery_wait)
	a = core.analyze(postal_code, radius_km, svc, priority if svc.has_history else 4)
	return a.to_dict()


@mcp.tool()
@clear_errors
def site_history(site_id: int, modality: str = "CT", months: int = 0,
		procedure_id: Optional[int] = None, surgery_wait: int = 2) -> list[dict]:
	"""Monthly P90 wait (days) and % within target for one site, by priority (P2, P3, P4), oldest first.

	months = 0 returns every month Ontario Health publishes (since January 2022); a positive value
	keeps only the newest months. site_id -354 is the Ontario provincial figure. modality is CT or
	MRI; pass procedure_id and surgery_wait for a surgical procedure. Breast screening has no
	history. Values are null where the site reported too few cases.
	"""
	try:
		sid = int(site_id)
		n = int(months)
	except (TypeError, ValueError):
		raise ValueError("site_id and months must be integers.") from None
	if sid == 0 or sid < core.PROVINCIAL_ID:
		raise ValueError(f"site_id must be a positive integer or -354; got {site_id!r}.")
	if n < 0:
		raise ValueError(f"months must be 0 (all) or positive; got {months!r}.")
	svc = _service(modality, procedure_id, surgery_wait)
	if svc.is_screening:
		raise ValueError("Breast screening has no monthly history; use rank_sites or find_sites.")
	h = core.site_history(sid, svc)
	if not h.p90:
		raise ValueError(f"No {svc.label} history found for site {sid}.")
	keys = sorted(h.p90)[-n:] if n else sorted(h.p90)
	return [{"month": core.month_label(k),
		"p90_days": {f"P{p}": h.p90[k][p] for p in core.PRIORITIES},
		"pct_within_target": {f"P{p}": (h.within.get(k) or {}).get(p) for p in core.PRIORITIES}}
		for k in keys]


@mcp.tool()
@clear_errors
def persistence(postal_code: str, modality: str = "CT", priority: int = 4, radius_km: float = 40,
		procedure_id: Optional[int] = None, surgery_wait: int = 2) -> list[dict]:
	"""How stable the ranking of nearby sites is over time, for CT, MRI or a surgical procedure.

	For lags of 1, 3, 6 and 12 months: mean Spearman rho between site P90s at month m and
	m plus lag, the share of month pairs where the month-m leader is still in the top 3,
	the chance baseline (3 divided by the number of sites), and the number of month pairs.
	Month pairs with fewer than 6 reporting sites are skipped. Not available for breast screening.
	"""
	svc = _service(modality, procedure_id, surgery_wait)
	if svc.is_screening:
		raise ValueError("Breast screening has no monthly history, so persistence cannot be computed.")
	a = core.analyze(postal_code, radius_km, svc, priority)
	return [s.to_dict() for s in a.lag_stats]


def main() -> None:
	mcp.run(transport="stdio")


if __name__ == "__main__":
	main()
