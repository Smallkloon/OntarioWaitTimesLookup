"""Shared test plumbing: fixture loader and a fake API that replaces core.fetch_json."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
	sys.path.insert(0, str(ROOT))

from ctwaits import core  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
	return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeApi:
	"""Serve canned payloads by URL and record every URL requested.

	``add(match, payload)``: ``match`` is a substring or a predicate(url);
	``payload`` is data or a callable(url) returning data.
	"""

	def __init__(self) -> None:
		self.routes: list[tuple[object, object]] = []
		self.calls: list[str] = []

	def add(self, match, payload) -> None:
		self.routes.append((match, payload))

	def __call__(self, url: str):
		self.calls.append(url)
		for match, payload in self.routes:
			hit = match(url) if callable(match) else (match in url)
			if hit:
				return payload(url) if callable(payload) else payload
		raise AssertionError(f"Unexpected request: {url}")

	def urls(self, part: str) -> list[str]:
		return [u for u in self.calls if part in u]


@pytest.fixture
def api(monkeypatch) -> FakeApi:
	fake = FakeApi()
	monkeypatch.setattr(core, "fetch_json", fake)
	monkeypatch.setattr(core, "CACHE_DIR", None)
	monkeypatch.setattr(core.time, "sleep", lambda _s: None)  # retries back off; tests need not wait
	return fake


@pytest.fixture
def fixtures():
	return load_fixture
