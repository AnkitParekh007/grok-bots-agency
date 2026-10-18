"""Bounded source collections: incomplete retrieval must not look like completion."""
import math
import time
from urllib.parse import urlsplit, parse_qs

from .http import IntegrationError, IntegrationNotAuthorized


class IncompleteSource(IntegrationError):
    pass


class CollectionBudget:
    def __init__(self, http, cfg):
        self.http = http
        self.max_pages = int(cfg.get("max_pages", 10))
        self.max_items = int(cfg.get("max_collection_items", 10000))
        seconds = float(cfg.get("collection_timeout_s", 60))
        if not 1 <= self.max_pages <= 100 or not 1 <= self.max_items <= 10000 or not math.isfinite(seconds) or not 1 <= seconds <= 300:
            raise ValueError("invalid source collection page/item/deadline bounds")
        self.deadline, self.count = time.monotonic() + seconds, 0

    def get(self, path, params=None):
        if time.monotonic() >= self.deadline:
            raise IncompleteSource("source collection deadline exceeded; retrieval is incomplete")
        return self.http.get_json(path, params, deadline=self.deadline)

    def rows(self, data, key):
        if not isinstance(data, dict) or not isinstance(data.get(key), list) or any(not isinstance(row, dict) for row in data[key]):
            raise IncompleteSource("source collection has an unsupported page schema")
        self.count += len(data[key])
        if self.count > self.max_items:
            raise IncompleteSource("source collection exceeds item budget; retrieval is incomplete")
        return data[key]


def offset_page(data, rows, start, *, start_key="startAt", total_key="total"):
    total = data.get(total_key)
    if type(total) is not int or total < start + len(rows) or data.get(start_key, start) != start or (not rows and start < total):
        raise IncompleteSource("source collection pagination counters are inconsistent")
    return start + len(rows), start + len(rows) >= total


def next_offset(data, start):
    last = data.get("isLastPage")
    if type(last) is not bool:
        raise IncompleteSource("source collection omits its completion marker")
    if last:
        return None
    value = data.get("nextPageStart")
    if type(value) is not int or not start < value <= 1_000_000_000:
        raise IncompleteSource("source collection continuation does not advance")
    return value


def cloud_next(http, endpoint, value, original_params):
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise IncompleteSource("invalid source collection continuation")
    try:
        url = http.url(value)
    except (ValueError, TypeError):
        raise IncompleteSource("invalid source collection continuation URL") from None
    http._check_url(url)
    if urlsplit(url).path.rstrip("/") != urlsplit(http.url(endpoint)).path.rstrip("/"):
        raise IntegrationNotAuthorized("source pagination cannot change the approved resource path")
    params = parse_qs(urlsplit(url).query, keep_blank_values=True)
    if any(len(values) != 1 for values in params.values()) or set(params) - (set(original_params) | {"page", "pagelen"}):
        raise IntegrationNotAuthorized("source pagination has unreviewed query parameters")
    if "pagelen" in params:
        try:
            size = int(params["pagelen"][0])
        except ValueError:
            raise IncompleteSource("invalid source continuation page size") from None
        if not 1 <= size <= int(original_params["pagelen"]):
            raise IncompleteSource("source continuation exceeds approved page size")
    for key, expected in original_params.items():
        if key != "pagelen" and params.get(key) != [str(expected)]:
            raise IntegrationNotAuthorized("source pagination changed the approved query scope")
    return url
