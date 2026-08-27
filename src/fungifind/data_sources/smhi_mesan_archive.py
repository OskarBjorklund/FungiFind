"""Point-wise reader for SMHI's historical MESAN (GRIDPP) GRIB2 archive.

The archive files contain the complete 2.5 km domain and are roughly 60--70 MB
per hour.  This reader deliberately uses HTTP byte ranges: it caches the GRIB2
metadata and the small packed-data blocks that contain the requested grid cell,
instead of downloading every complete Scandinavian field.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import Lock
from types import MappingProxyType
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from fungifind.data_sources.smhi_mesan import (
    MesanGridPoint,
    parse_utc_datetime,
)

SMHI_GRID_ARCHIVE_FEED_9 = "https://opendata-download-grid-archive.smhi.se/feed/9"
HISTORICAL_SOURCE_API = "SMHI_GRID_ARCHIVE"
HISTORICAL_SOURCE_PRODUCT = "SMHI_MESAN_GRIDPP"
HISTORICAL_SOURCE_VERSION = "feed-9/grib2"
USER_AGENT = "FungiFind-MESAN-history/0.1"
ATOM_NAMESPACE = "http://www.w3.org/2005/Atom"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MESAN_ARCHIVE_CACHE = (
    PROJECT_ROOT / "src" / "data" / "weather" / "mesan_archive_cache"
)

_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")
_TIMESTAMP_IN_TITLE = re.compile(r"(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}) UTC")


class HistoricalMesanError(RuntimeError):
    """Raised when the official archive or a GRIB2 file is invalid."""


class HistoricalMesanUnavailable(HistoricalMesanError):
    """Raised when SMHI has no archive feed for a requested date."""


@dataclass(frozen=True, slots=True)
class HistoricalMesanAsset:
    timestamp: datetime
    href: str
    filename: str
    updated_time: datetime


@dataclass(frozen=True, slots=True)
class GribMessageMetadata:
    offset: int
    total_length: int
    discipline: int
    parameter_category: int
    parameter_number: int
    product_definition_template: int
    surface_type: int
    surface_value: int | None
    reference_time: datetime
    valid_time: datetime
    forecast_seconds: int
    grid_section: bytes
    grid_identity: str
    width: int
    height: int
    scanning_mode: int
    data_offset: int
    packed_value_count: int
    data_representation_template: int
    reference_value: float
    binary_scale_factor: int
    decimal_scale_factor: int
    bits_per_value: int
    bitmap_indicator: int

    @property
    def parameter_key(self) -> tuple[int, int, int, int | None, int]:
        return (
            self.parameter_category,
            self.parameter_number,
            self.surface_type,
            self.surface_value,
            self.forecast_seconds,
        )


@dataclass(frozen=True, slots=True)
class HistoricalMesanLayout:
    grid_identity: str
    file_size: int
    required_messages: Mapping[str, GribMessageMetadata]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "required_messages",
            MappingProxyType(dict(self.required_messages)),
        )


@dataclass(frozen=True, slots=True)
class HistoricalGridSelection:
    requested_point: MesanGridPoint
    grid_point: MesanGridPoint
    row: int
    column: int
    packed_value_index: int
    grid_identity: str
    crs_wkt: str
    grid_resolution_m: tuple[float, float]
    sampling_method: str = "nearest_grid_cell_center:no_interpolation"


@dataclass(frozen=True, slots=True)
class HistoricalMesanHour:
    requested_point: MesanGridPoint
    grid_point: MesanGridPoint
    time: datetime
    air_temperature_c: float
    precipitation_1h_mm: float
    relative_humidity_percent: float
    wind_speed_m_s: float
    wind_u_m_s: float
    wind_v_m_s: float
    source_api: str
    source_product: str
    source_version: str
    source_url: str
    source_file: str
    grid_identity: str
    sampling_method: str
    source_units: Mapping[str, str]
    reference_time: datetime
    created_time: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "time", self.time.astimezone(UTC))
        object.__setattr__(self, "reference_time", self.reference_time.astimezone(UTC))
        object.__setattr__(self, "created_time", self.created_time.astimezone(UTC))
        object.__setattr__(self, "source_units", MappingProxyType(dict(self.source_units)))


JsonOpener = Callable[..., Any]


def _open_with_retry(opener: JsonOpener, request: Request, timeout: float) -> Any:
    for attempt in range(3):
        try:
            return opener(request, timeout=timeout)
        except HTTPError as exc:
            if attempt == 2 or not (exc.code in (406, 408, 429) or exc.code >= 500):
                raise
        except (URLError, TimeoutError):
            if attempt == 2:
                raise
        # The grid archive occasionally returns a short-lived cached 406 while
        # materialising an older Atom day feed. A multi-second retry is needed;
        # sub-second retries merely hit the same Varnish response again.
        time.sleep((2.0, 5.0)[attempt])
    raise AssertionError("unreachable retry state")


def _parse_atom_time(value: str, context: str) -> datetime:
    normalized = value[:-5] + value[-5:-2] + ":" + value[-2:] if re.search(
        r"[+-]\d{4}$", value
    ) else value
    return parse_utc_datetime(normalized, context)


class MesanArchiveFeedClient:
    """Discover hourly GRIB2 assets through SMHI's official Atom feed."""

    def __init__(
        self,
        feed_url: str = SMHI_GRID_ARCHIVE_FEED_9,
        *,
        timeout: float = 60.0,
        opener: JsonOpener = urlopen,
    ) -> None:
        self.feed_url = feed_url.rstrip("/")
        self.timeout = timeout
        self.opener = opener

    def _get_xml(self, url: str) -> bytes:
        request = Request(
            url,
            headers={"Accept": "application/atom+xml", "User-Agent": USER_AGENT},
        )
        try:
            with _open_with_retry(self.opener, request, self.timeout) as response:
                status = int(getattr(response, "status", 200))
                if not 200 <= status < 300:
                    raise HistoricalMesanError(f"HTTP {status} for {url}")
                return response.read()
        except HTTPError as exc:
            if exc.code == 404:
                raise HistoricalMesanUnavailable(f"No MESAN archive feed at {url}") from exc
            raise HistoricalMesanError(f"HTTP {exc.code} for {url}: {exc.reason}") from exc
        except (URLError, TimeoutError) as exc:
            raise HistoricalMesanError(f"Could not read {url}: {exc}") from exc

    def list_day(self, requested_date: date) -> tuple[HistoricalMesanAsset, ...]:
        url = (
            f"{self.feed_url}/{requested_date.year:04d}/{requested_date.month:02d}"
            f"/{requested_date.day:02d}"
        )
        try:
            raw = self._get_xml(url)
        except HistoricalMesanError as exc:
            if "HTTP 406" not in str(exc):
                raise
            # Some older day feeds intermittently/permanently return 406 even
            # though their month entry and hourly archive files exist. Fall back
            # to the same official feed's documented data-path convention and
            # validate every candidate with HEAD; no file URL is accepted blindly.
            return self._list_day_from_validated_assets(requested_date)
        try:
            root = ElementTree.fromstring(raw)
        except ElementTree.ParseError as exc:
            raise HistoricalMesanError(f"Invalid Atom XML returned by {url}") from exc
        assets: list[HistoricalMesanAsset] = []
        namespace = {"atom": ATOM_NAMESPACE}
        for index, entry in enumerate(root.findall("atom:entry", namespace)):
            title = (entry.findtext("atom:title", default="", namespaces=namespace)).strip()
            match = _TIMESTAMP_IN_TITLE.fullmatch(title)
            if match is None:
                raise HistoricalMesanError(
                    f"Archive entry {index} has an unexpected title: {title!r}"
                )
            timestamp = datetime.fromisoformat(
                f"{match.group(1)}T{match.group(2)}:{match.group(3)}:00+00:00"
            )
            links = [
                link
                for link in entry.findall("atom:link", namespace)
                if link.attrib.get("rel") == "enclosure"
            ]
            if len(links) != 1 or not links[0].attrib.get("href"):
                raise HistoricalMesanError(
                    f"Archive entry {title!r} has no unique enclosure link"
                )
            href = str(links[0].attrib["href"])
            filename = Path(urlparse(href).path).name
            updated = entry.findtext("atom:updated", default="", namespaces=namespace)
            if not filename or not updated:
                raise HistoricalMesanError(f"Archive entry {title!r} lacks metadata")
            assets.append(
                HistoricalMesanAsset(
                    timestamp=timestamp,
                    href=href,
                    filename=filename,
                    updated_time=_parse_atom_time(updated, f"updated time for {title}"),
                )
            )
        assets.sort(key=lambda item: item.timestamp)
        if len({asset.timestamp for asset in assets}) != len(assets):
            raise HistoricalMesanError(f"Duplicate timestamps in archive feed {url}")
        return tuple(assets)

    def _list_day_from_validated_assets(
        self, requested_date: date
    ) -> tuple[HistoricalMesanAsset, ...]:
        parsed_feed = urlparse(self.feed_url)
        collection = Path(parsed_feed.path).name
        if not collection.isdigit():
            raise HistoricalMesanError(
                f"Cannot derive archive collection from feed URL {self.feed_url}"
            )
        root = f"{parsed_feed.scheme}://{parsed_feed.netloc}"
        assets: list[HistoricalMesanAsset] = []
        for hour in range(24):
            timestamp = datetime(
                requested_date.year,
                requested_date.month,
                requested_date.day,
                hour,
                tzinfo=UTC,
            )
            filename = f"MESAN_{timestamp:%Y%m%d%H%M}+000H00M"
            href = (
                f"{root}/data/{collection}/{timestamp:%Y%m}/{filename}"
            )
            request = Request(href, method="HEAD", headers={"User-Agent": USER_AGENT})
            try:
                with _open_with_retry(self.opener, request, self.timeout) as response:
                    status = int(getattr(response, "status", 200))
                    last_modified = response.headers.get("Last-Modified")
            except HTTPError as exc:
                if exc.code == 404:
                    continue
                raise HistoricalMesanError(
                    f"HTTP {exc.code} while validating fallback asset {href}"
                ) from exc
            except (URLError, TimeoutError) as exc:
                raise HistoricalMesanError(
                    f"Could not validate fallback asset {href}: {exc}"
                ) from exc
            if not 200 <= status < 300 or not last_modified:
                raise HistoricalMesanError(
                    f"Fallback asset lacks validated Last-Modified metadata: {href}"
                )
            updated = parsedate_to_datetime(last_modified)
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=UTC)
            assets.append(
                HistoricalMesanAsset(
                    timestamp=timestamp,
                    href=href,
                    filename=filename,
                    updated_time=updated.astimezone(UTC),
                )
            )
        return tuple(assets)

    def list_period(
        self, start_time: datetime, end_time: datetime
    ) -> tuple[tuple[HistoricalMesanAsset, ...], int]:
        start = _exact_utc_hour(start_time)
        end = _exact_utc_hour(end_time)
        if end < start:
            raise ValueError("MESAN archive period ends before it starts")
        parsed_feed = urlparse(self.feed_url)
        collection = Path(parsed_feed.path).name
        if not collection.isdigit():
            raise HistoricalMesanError(
                f"Cannot derive archive collection from feed URL {self.feed_url}"
            )
        root_url = f"{parsed_feed.scheme}://{parsed_feed.netloc}"
        available_dates: dict[date, datetime] = {}
        month_cursor = date(start.year, start.month, 1)
        final_month = date(end.year, end.month, 1)
        while month_cursor <= final_month:
            month_url = (
                f"{self.feed_url}/{month_cursor.year:04d}/{month_cursor.month:02d}"
            )
            try:
                raw = self._get_xml(month_url)
            except HistoricalMesanUnavailable:
                pass
            else:
                try:
                    month_root = ElementTree.fromstring(raw)
                except ElementTree.ParseError as exc:
                    raise HistoricalMesanError(
                        f"Invalid Atom XML returned by {month_url}"
                    ) from exc
                namespace = {"atom": ATOM_NAMESPACE}
                for entry in month_root.findall("atom:entry", namespace):
                    title = entry.findtext(
                        "atom:title", default="", namespaces=namespace
                    ).strip()
                    try:
                        entry_date = date.fromisoformat(title)
                    except ValueError as exc:
                        raise HistoricalMesanError(
                            f"Unexpected month-feed entry title: {title!r}"
                        ) from exc
                    updated = entry.findtext(
                        "atom:updated", default="", namespaces=namespace
                    )
                    if not updated:
                        raise HistoricalMesanError(
                            f"Month-feed entry {title!r} has no updated time"
                        )
                    available_dates[entry_date] = _parse_atom_time(
                        updated, f"updated time for {title}"
                    )
            month_cursor = (
                date(month_cursor.year + 1, 1, 1)
                if month_cursor.month == 12
                else date(month_cursor.year, month_cursor.month + 1, 1)
            )

        assets: list[HistoricalMesanAsset] = []
        unavailable_days = 0
        current = start.date()
        while current <= end.date():
            day_updated = available_dates.get(current)
            if day_updated is None:
                unavailable_days += 1
                current += timedelta(days=1)
                continue
            for hour in range(24):
                timestamp = datetime(
                    current.year, current.month, current.day, hour, tzinfo=UTC
                )
                if not start <= timestamp <= end:
                    continue
                filename = f"MESAN_{timestamp:%Y%m%d%H%M}+000H00M"
                assets.append(
                    HistoricalMesanAsset(
                        timestamp=timestamp,
                        href=(
                            f"{root_url}/data/{collection}/{timestamp:%Y%m}/{filename}"
                        ),
                        filename=filename,
                        # The precise per-file Last-Modified header supersedes
                        # this day-feed timestamp during GRIB sampling.
                        updated_time=day_updated,
                    )
                )
            current += timedelta(days=1)
        assets.sort(key=lambda item: item.timestamp)
        return tuple(assets), unavailable_days


class ByteRangeReader(Protocol):
    def get_size(self, url: str) -> int: ...

    def get_last_modified(self, url: str) -> datetime | None: ...

    def read(self, url: str, start: int, length: int) -> bytes: ...

    def download(self, url: str, start: int, length: int, target: Path) -> None: ...


class CachedHttpRangeReader:
    """Validated HTTP byte-range reader with a Git-ignored on-disk cache."""

    def __init__(
        self,
        cache_directory: str | Path = DEFAULT_MESAN_ARCHIVE_CACHE,
        *,
        timeout: float = 60.0,
        block_size: int = 4096,
        opener: JsonOpener = urlopen,
    ) -> None:
        if block_size < 512:
            raise ValueError("HTTP range-cache blocks must be at least 512 bytes")
        self.cache_directory = Path(cache_directory).resolve()
        self.timeout = timeout
        self.block_size = block_size
        self.opener = opener
        self._sizes: dict[str, int] = {}
        self._last_modified: dict[str, datetime] = {}
        self._lock_guard = Lock()
        self._path_locks: dict[Path, Lock] = {}

    @staticmethod
    def _url_key(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def _cache_path(self, url: str, start: int, length: int) -> Path:
        return (
            self.cache_directory
            / "ranges"
            / self._url_key(url)[:20]
            / f"{start:012d}-{length:06d}.bin"
        )

    def _path_lock(self, path: Path) -> Lock:
        with self._lock_guard:
            return self._path_locks.setdefault(path, Lock())

    def get_size(self, url: str) -> int:
        if url in self._sizes:
            return self._sizes[url]
        request = Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        try:
            with _open_with_retry(self.opener, request, self.timeout) as response:
                status = int(getattr(response, "status", 200))
                if not 200 <= status < 300:
                    raise HistoricalMesanError(f"HTTP {status} for {url}")
                raw_size = response.headers.get("Content-Length")
                last_modified = response.headers.get("Last-Modified")
        except HTTPError as exc:
            if exc.code == 404:
                raise HistoricalMesanUnavailable(f"Historical GRIB is unavailable: {url}") from exc
            raise HistoricalMesanError(f"HTTP {exc.code} for {url}: {exc.reason}") from exc
        except (URLError, TimeoutError) as exc:
            raise HistoricalMesanError(f"Could not inspect {url}: {exc}") from exc
        try:
            size = int(raw_size)
        except (TypeError, ValueError) as exc:
            raise HistoricalMesanError(f"No valid Content-Length for {url}") from exc
        if size <= 0:
            raise HistoricalMesanError(f"Invalid Content-Length {size} for {url}")
        if last_modified:
            parsed_modified = parsedate_to_datetime(last_modified)
            if parsed_modified.tzinfo is None:
                parsed_modified = parsed_modified.replace(tzinfo=UTC)
            self._last_modified[url] = parsed_modified.astimezone(UTC)
        self._sizes[url] = size
        return size

    def get_last_modified(self, url: str) -> datetime | None:
        self.get_size(url)
        return self._last_modified.get(url)

    def _fetch_range(self, url: str, start: int, length: int) -> bytes:
        end = start + length - 1
        request = Request(
            url,
            headers={
                "Accept": "application/octet-stream",
                "Range": f"bytes={start}-{end}",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with _open_with_retry(self.opener, request, self.timeout) as response:
                status = int(getattr(response, "status", 200))
                content_range = response.headers.get("Content-Range", "")
                raw = response.read()
        except HTTPError as exc:
            if exc.code == 404:
                raise HistoricalMesanUnavailable(f"Historical GRIB is unavailable: {url}") from exc
            raise HistoricalMesanError(f"HTTP {exc.code} for {url}: {exc.reason}") from exc
        except (URLError, TimeoutError) as exc:
            raise HistoricalMesanError(f"Could not read {url}: {exc}") from exc
        match = _CONTENT_RANGE.fullmatch(content_range)
        if status != 206 or match is None:
            raise HistoricalMesanError(
                f"Server ignored or malformed byte range {start}-{end} for {url}"
            )
        returned_start, returned_end, total_size = (int(value) for value in match.groups())
        if returned_start != start or returned_end != end or len(raw) != length:
            raise HistoricalMesanError(
                f"Incomplete byte range {start}-{end} for {url}: received {len(raw)} bytes"
            )
        self._sizes[url] = total_size
        return raw

    def read(self, url: str, start: int, length: int) -> bytes:
        if start < 0 or length <= 0:
            raise ValueError("Byte ranges require a non-negative start and positive length")
        file_size = self.get_size(url)
        if start + length > file_size:
            raise HistoricalMesanError(
                f"Byte range {start}+{length} exceeds {file_size} bytes for {url}"
            )
        result = bytearray()
        position = start
        end = start + length
        while position < end:
            block_start = position - (position % self.block_size)
            block_length = min(self.block_size, file_size - block_start)
            cache_path = self._cache_path(url, block_start, block_length)
            with self._path_lock(cache_path):
                if cache_path.is_file() and cache_path.stat().st_size != block_length:
                    cache_path.unlink()
                if not cache_path.is_file():
                    raw = self._fetch_range(url, block_start, block_length)
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = cache_path.with_suffix(cache_path.suffix + ".part")
                    try:
                        with temporary.open("wb") as output:
                            output.write(raw)
                        if temporary.stat().st_size != block_length:
                            raise HistoricalMesanError(f"Incomplete cache block for {url}")
                        temporary.replace(cache_path)
                    finally:
                        if temporary.exists():
                            temporary.unlink()
                offset = position - block_start
                take = min(end - position, block_length - offset)
                with cache_path.open("rb") as cached:
                    cached.seek(offset)
                    result.extend(cached.read(take))
            position += take
        if len(result) != length:
            raise HistoricalMesanError(f"Corrupt cached byte range for {url}")
        return bytes(result)

    def download(self, url: str, start: int, length: int, target: Path) -> None:
        with self._path_lock(target):
            if target.is_file() and target.stat().st_size == length:
                return
            end = start + length - 1
            request = Request(
                url,
                headers={
                    "Accept": "application/octet-stream",
                    "Range": f"bytes={start}-{end}",
                    "User-Agent": USER_AGENT,
                },
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(target.suffix + ".part")
            try:
                with _open_with_retry(self.opener, request, self.timeout) as response:
                    status = int(getattr(response, "status", 200))
                    content_range = response.headers.get("Content-Range", "")
                    match = _CONTENT_RANGE.fullmatch(content_range)
                    if status != 206 or match is None:
                        raise HistoricalMesanError(f"Server ignored byte range for {url}")
                    with temporary.open("wb") as output:
                        while chunk := response.read(1024 * 1024):
                            output.write(chunk)
                if temporary.stat().st_size != length:
                    raise HistoricalMesanError(
                        f"Incomplete GRIB template for {url}: "
                        f"{temporary.stat().st_size}/{length}"
                    )
                temporary.replace(target)
            except (HTTPError, URLError, TimeoutError) as exc:
                raise HistoricalMesanError(
                    f"Could not download GRIB template {url}: {exc}"
                ) from exc
            finally:
                if temporary.exists():
                    temporary.unlink()


def _exact_utc_hour(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Historical MESAN timestamps require a timezone")
    result = value.astimezone(UTC)
    if result.minute or result.second or result.microsecond:
        raise ValueError("Historical MESAN timestamps must align to an exact hour")
    return result


def _signed_magnitude(raw: int, bit_count: int) -> int:
    sign_bit = 1 << (bit_count - 1)
    return -(raw & (sign_bit - 1)) if raw & sign_bit else raw


def _time_unit_seconds(code: int) -> int:
    units = {0: 60, 1: 3600, 2: 86400}
    try:
        return units[code]
    except KeyError as exc:
        raise HistoricalMesanError(f"Unsupported GRIB time-range unit: {code}") from exc


def parse_grib_message_header(prefix: bytes, *, offset: int = 0) -> GribMessageMetadata:
    """Parse sections 0--6 of one GRIB2 message; no field array is loaded."""

    if len(prefix) < 16 or prefix[:4] != b"GRIB" or prefix[7] != 2:
        raise HistoricalMesanError(f"No GRIB2 message at byte offset {offset}")
    discipline = prefix[6]
    total_length = int.from_bytes(prefix[8:16], "big")
    if total_length < 32:
        raise HistoricalMesanError(f"Invalid GRIB2 message length at byte offset {offset}")
    sections: dict[int, bytes] = {}
    position = 16
    data_offset: int | None = None
    while position + 5 <= len(prefix):
        section_length = int.from_bytes(prefix[position : position + 4], "big")
        section_number = prefix[position + 4]
        if section_length < 5:
            raise HistoricalMesanError(f"Invalid GRIB section at byte offset {offset}")
        if section_number == 7:
            data_offset = offset + position + 5
            break
        end = position + section_length
        if end > len(prefix):
            raise HistoricalMesanError(
                f"GRIB metadata prefix is truncated at byte offset {offset}"
            )
        sections[section_number] = prefix[position:end]
        position = end
    if data_offset is None or not all(number in sections for number in (1, 3, 4, 5, 6)):
        raise HistoricalMesanError(f"Incomplete GRIB metadata at byte offset {offset}")

    section1 = sections[1]
    try:
        reference_time = datetime(
            int.from_bytes(section1[12:14], "big"),
            section1[14],
            section1[15],
            section1[16],
            section1[17],
            section1[18],
            tzinfo=UTC,
        )
    except (IndexError, ValueError) as exc:
        raise HistoricalMesanError("Invalid GRIB reference time") from exc

    section3 = sections[3]
    if len(section3) < 65:
        raise HistoricalMesanError("Unsupported truncated GRIB grid definition")
    grid_template = int.from_bytes(section3[12:14], "big")
    if grid_template != 30:
        raise HistoricalMesanError(
            f"Unsupported GRIB grid template {grid_template}; MESAN GRIDPP expects 30"
        )
    packed_value_count = int.from_bytes(section3[6:10], "big")
    width = int.from_bytes(section3[30:34], "big")
    height = int.from_bytes(section3[34:38], "big")
    scanning_mode = section3[64]
    if width <= 0 or height <= 0 or packed_value_count != width * height:
        raise HistoricalMesanError("GRIB grid dimensions do not match packed point count")

    section4 = sections[4]
    if len(section4) < 28:
        raise HistoricalMesanError("Truncated GRIB product definition")
    product_template = int.from_bytes(section4[7:9], "big")
    if product_template not in (0, 8):
        raise HistoricalMesanError(
            f"Unsupported GRIB product-definition template {product_template}"
        )
    forecast_seconds = (
        int.from_bytes(section4[18:22], "big") * _time_unit_seconds(section4[17])
    )
    surface_scale = section4[23]
    surface_raw = int.from_bytes(section4[24:28], "big")
    surface_value = (
        None
        if surface_scale == 255 or surface_raw == 0xFFFFFFFF
        else round(surface_raw * (10 ** -_signed_magnitude(surface_scale, 8)))
    )

    section5 = sections[5]
    if len(section5) < 21:
        raise HistoricalMesanError("Truncated GRIB data representation")
    representation_template = int.from_bytes(section5[9:11], "big")
    if representation_template != 0:
        raise HistoricalMesanError(
            f"Unsupported GRIB data-representation template {representation_template}"
        )
    reference_value = struct.unpack(">f", section5[11:15])[0]
    binary_scale = _signed_magnitude(int.from_bytes(section5[15:17], "big"), 16)
    decimal_scale = _signed_magnitude(int.from_bytes(section5[17:19], "big"), 16)
    bits_per_value = section5[19]
    if not math.isfinite(reference_value) or not 0 <= bits_per_value <= 32:
        raise HistoricalMesanError("Invalid GRIB simple-packing metadata")
    bitmap_indicator = sections[6][5] if len(sections[6]) >= 6 else -1
    if bitmap_indicator != 255:
        raise HistoricalMesanError("GRIB bitmap fields are not supported for MESAN point reads")

    return GribMessageMetadata(
        offset=offset,
        total_length=total_length,
        discipline=discipline,
        parameter_category=section4[9],
        parameter_number=section4[10],
        product_definition_template=product_template,
        surface_type=section4[22],
        surface_value=surface_value,
        reference_time=reference_time,
        valid_time=reference_time + timedelta(seconds=forecast_seconds),
        forecast_seconds=forecast_seconds,
        grid_section=section3,
        grid_identity="grib2-section3-sha256:" + hashlib.sha256(section3).hexdigest(),
        width=width,
        height=height,
        scanning_mode=scanning_mode,
        data_offset=data_offset,
        packed_value_count=int.from_bytes(section5[5:9], "big"),
        data_representation_template=representation_template,
        reference_value=reference_value,
        binary_scale_factor=binary_scale,
        decimal_scale_factor=decimal_scale,
        bits_per_value=bits_per_value,
        bitmap_indicator=bitmap_indicator,
    )


def _required_name(message: GribMessageMetadata) -> str | None:
    keys = {
        (0, 0, 103, 2, 0): "temperature",
        (2, 2, 103, 10, 0): "wind_u",
        (2, 3, 103, 10, 0): "wind_v",
        (1, 1, 103, 2, 0): "relative_humidity",
        (1, 52, 103, 0, 3600): "precipitation_1h",
    }
    return keys.get(message.parameter_key)


def discover_grib_layout(
    asset: HistoricalMesanAsset,
    range_reader: ByteRangeReader,
) -> HistoricalMesanLayout:
    file_size = range_reader.get_size(asset.href)
    required: dict[str, GribMessageMetadata] = {}
    offset = 0
    first_grid_identity: str | None = None
    message_count = 0
    while offset < file_size:
        prefix = range_reader.read(asset.href, offset, min(512, file_size - offset))
        message = parse_grib_message_header(prefix, offset=offset)
        if offset + message.total_length > file_size:
            raise HistoricalMesanError(f"GRIB message exceeds source file {asset.filename}")
        if first_grid_identity is None:
            first_grid_identity = message.grid_identity
        elif message.grid_identity != first_grid_identity:
            raise HistoricalMesanError(f"Multiple grids occur in {asset.filename}")
        name = _required_name(message)
        if name is not None:
            if name in required:
                raise HistoricalMesanError(f"Duplicate required GRIB field {name}")
            required[name] = message
        offset += message.total_length
        message_count += 1
        if message_count > 256:
            raise HistoricalMesanError(f"Unreasonable GRIB message count in {asset.filename}")
    if offset != file_size:
        raise HistoricalMesanError(f"GRIB messages do not fill {asset.filename}")
    missing = sorted(
        {"temperature", "wind_u", "wind_v", "relative_humidity", "precipitation_1h"}
        - set(required)
    )
    if missing:
        raise HistoricalMesanError(f"Required GRIB fields missing from {asset.filename}: {missing}")
    return HistoricalMesanLayout(
        grid_identity=str(first_grid_identity),
        file_size=file_size,
        required_messages=required,
    )


def _packed_index(row: int, column: int, message: GribMessageMetadata) -> int:
    if message.scanning_mode != 0x40:
        raise HistoricalMesanError(
            f"Unsupported MESAN GRIB scanning mode 0x{message.scanning_mode:02x}"
        )
    return (message.height - 1 - row) * message.width + column


def _decode_simple_packed_value(
    raw: bytes,
    *,
    bit_offset: int,
    metadata: GribMessageMetadata,
) -> float:
    byte_value = int.from_bytes(raw, "big")
    total_bits = len(raw) * 8
    trailing = total_bits - bit_offset - metadata.bits_per_value
    if trailing < 0:
        raise HistoricalMesanError("Truncated packed GRIB value")
    packed = (byte_value >> trailing) & ((1 << metadata.bits_per_value) - 1)
    return (
        metadata.reference_value
        + packed * (2**metadata.binary_scale_factor)
    ) * (10 ** -metadata.decimal_scale_factor)


class MesanHistoricalGribPointReader:
    """Read the five compatible meteorological fields for one WGS84 point."""

    def __init__(
        self,
        range_reader: ByteRangeReader | None = None,
        *,
        cache_directory: str | Path = DEFAULT_MESAN_ARCHIVE_CACHE,
    ) -> None:
        self.cache_directory = Path(cache_directory).resolve()
        self.range_reader = (
            CachedHttpRangeReader(self.cache_directory)
            if range_reader is None
            else range_reader
        )

    def select_grid_point(
        self,
        asset: HistoricalMesanAsset,
        layout: HistoricalMesanLayout,
        latitude: float,
        longitude: float,
    ) -> HistoricalGridSelection:
        temperature = layout.required_messages["temperature"]
        template_path = (
            self.cache_directory
            / "grid_templates"
            / f"{layout.grid_identity.split(':')[-1]}.grib2"
        )
        self.range_reader.download(
            asset.href,
            temperature.offset,
            temperature.total_length,
            template_path,
        )
        try:
            import rasterio
            from pyproj import Transformer
        except ImportError as exc:
            raise HistoricalMesanError(
                "Historical MESAN grid mapping requires rasterio and pyproj"
            ) from exc
        try:
            with rasterio.open(template_path) as dataset:
                if dataset.crs is None:
                    raise HistoricalMesanError("Historical MESAN GRIB has no CRS")
                projected_x, projected_y = Transformer.from_crs(
                    "EPSG:4326", dataset.crs, always_xy=True
                ).transform(longitude, latitude)
                row, column = dataset.index(projected_x, projected_y)
                if not (0 <= row < dataset.height and 0 <= column < dataset.width):
                    raise HistoricalMesanError("Requested point is outside the MESAN grid")
                center_x, center_y = dataset.xy(row, column)
                center_lon, center_lat = Transformer.from_crs(
                    dataset.crs, "EPSG:4326", always_xy=True
                ).transform(center_x, center_y)
                crs_wkt = dataset.crs.to_wkt()
                resolution = (abs(float(dataset.res[0])), abs(float(dataset.res[1])))
        except HistoricalMesanError:
            raise
        except Exception as exc:
            raise HistoricalMesanError(f"Could not map the historical MESAN grid: {exc}") from exc
        packed_index = _packed_index(row, column, temperature)
        return HistoricalGridSelection(
            requested_point=MesanGridPoint(latitude, longitude),
            grid_point=MesanGridPoint(center_lat, center_lon),
            row=row,
            column=column,
            packed_value_index=packed_index,
            grid_identity=layout.grid_identity,
            crs_wkt=crs_wkt,
            grid_resolution_m=resolution,
        )

    def _read_value(
        self,
        asset: HistoricalMesanAsset,
        expected: GribMessageMetadata,
        selection: HistoricalGridSelection,
    ) -> tuple[float, GribMessageMetadata]:
        prefix = self.range_reader.read(asset.href, expected.offset, 512)
        actual = parse_grib_message_header(prefix, offset=expected.offset)
        if actual.parameter_key != expected.parameter_key:
            raise HistoricalMesanError(
                f"GRIB layout changed in {asset.filename} at byte {expected.offset}"
            )
        if actual.grid_identity != selection.grid_identity:
            raise HistoricalMesanError(
                f"GRIB grid changed in {asset.filename}; a new point mapping is required"
            )
        if actual.bits_per_value == 0:
            raise HistoricalMesanError(
                f"Required GRIB field is a constant/missing field in {asset.filename}"
            )
        if actual.valid_time != asset.timestamp:
            raise HistoricalMesanError(
                f"GRIB valid time {actual.valid_time} differs from feed time {asset.timestamp}"
            )
        packed_index = _packed_index(selection.row, selection.column, actual)
        bit_position = packed_index * actual.bits_per_value
        first_byte = bit_position // 8
        bit_offset = bit_position % 8
        byte_count = math.ceil((bit_offset + actual.bits_per_value) / 8)
        raw = self.range_reader.read(
            asset.href,
            actual.data_offset + first_byte,
            byte_count,
        )
        return (
            _decode_simple_packed_value(raw, bit_offset=bit_offset, metadata=actual),
            actual,
        )

    def sample_asset(
        self,
        asset: HistoricalMesanAsset,
        layout: HistoricalMesanLayout,
        selection: HistoricalGridSelection,
    ) -> HistoricalMesanHour:
        values: dict[str, float] = {}
        metadata: dict[str, GribMessageMetadata] = {}
        for name, expected in layout.required_messages.items():
            value, actual = self._read_value(asset, expected, selection)
            values[name] = value
            metadata[name] = actual
        temperature_c = values["temperature"] - 273.15
        humidity_fraction = values["relative_humidity"]
        if -1e-6 <= humidity_fraction <= 1.0 + 1e-6:
            humidity_fraction = min(1.0, max(0.0, humidity_fraction))
        humidity_percent = humidity_fraction * 100.0
        wind_speed = math.hypot(values["wind_u"], values["wind_v"])
        precipitation_mm = values["precipitation_1h"]
        numeric_values = (
            temperature_c,
            humidity_percent,
            wind_speed,
            precipitation_mm,
        )
        if not all(math.isfinite(value) for value in numeric_values):
            raise HistoricalMesanError(f"Non-finite MESAN value in {asset.filename}")
        if not 0 <= humidity_percent <= 100 or wind_speed < 0 or precipitation_mm < 0:
            raise HistoricalMesanError(f"Out-of-range MESAN value in {asset.filename}")
        return HistoricalMesanHour(
            requested_point=selection.requested_point,
            grid_point=selection.grid_point,
            time=asset.timestamp,
            air_temperature_c=temperature_c,
            precipitation_1h_mm=precipitation_mm,
            relative_humidity_percent=humidity_percent,
            wind_speed_m_s=wind_speed,
            wind_u_m_s=values["wind_u"],
            wind_v_m_s=values["wind_v"],
            source_api=HISTORICAL_SOURCE_API,
            source_product=HISTORICAL_SOURCE_PRODUCT,
            source_version=HISTORICAL_SOURCE_VERSION,
            source_url=asset.href,
            source_file=asset.filename,
            grid_identity=selection.grid_identity,
            sampling_method=selection.sampling_method,
            source_units={
                "air_temperature_raw": "K",
                "air_temperature_stored": "degrees_celsius",
                "precipitation_1h_raw": "mm",
                "precipitation_1h_stored": "mm",
                "relative_humidity_raw": "fraction",
                "relative_humidity_stored": "percent",
                "wind_u_raw": "m/s",
                "wind_v_raw": "m/s",
                "wind_speed_stored": "m/s",
            },
            reference_time=metadata["temperature"].reference_time,
            created_time=(
                self.range_reader.get_last_modified(asset.href) or asset.updated_time
            ),
        )

    def sample_assets(
        self,
        assets: Sequence[HistoricalMesanAsset],
        *,
        latitude: float,
        longitude: float,
        max_workers: int = 8,
    ) -> tuple[HistoricalMesanLayout, HistoricalGridSelection, tuple[HistoricalMesanHour, ...]]:
        if not assets:
            raise HistoricalMesanUnavailable("No historical MESAN assets were discovered")
        if max_workers <= 0:
            raise ValueError("max_workers must be positive")
        # SMHI explicitly notes that the parameters present vary by analysis
        # hour. In practice this changes message offsets, especially before the
        # one-hour precipitation field. Discover one validated layout for every
        # UTC hour represented instead of assuming a single all-day layout.
        representatives: dict[int, HistoricalMesanAsset] = {}
        for asset in assets:
            representatives.setdefault(asset.timestamp.hour, asset)
        layouts_by_hour: dict[int, HistoricalMesanLayout] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            layout_futures = {
                executor.submit(discover_grib_layout, asset, self.range_reader): hour
                for hour, asset in representatives.items()
            }
            for future in as_completed(layout_futures):
                layouts_by_hour[layout_futures[future]] = future.result()
        layout = layouts_by_hour[assets[0].timestamp.hour]
        if any(
            candidate.grid_identity != layout.grid_identity
            for candidate in layouts_by_hour.values()
        ):
            raise HistoricalMesanError(
                "Historical period contains multiple MESAN grids; split the import"
            )
        selection = self.select_grid_point(
            assets[0], layout, latitude=latitude, longitude=longitude
        )
        hours: list[HistoricalMesanHour] = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    self.sample_asset,
                    asset,
                    layouts_by_hour[asset.timestamp.hour],
                    selection,
                ): asset
                for asset in assets
            }
            for future in as_completed(futures):
                asset = futures[future]
                try:
                    hours.append(future.result())
                except Exception as exc:
                    for pending in futures:
                        pending.cancel()
                    if isinstance(exc, HistoricalMesanError):
                        raise
                    raise HistoricalMesanError(
                        f"Could not parse historical MESAN asset {asset.filename}: {exc}"
                    ) from exc
        hours.sort(key=lambda item: item.time)
        return layout, selection, tuple(hours)


def historical_units_json(hour: HistoricalMesanHour) -> str:
    return json.dumps(dict(hour.source_units), sort_keys=True, separators=(",", ":"))


__all__ = [
    "DEFAULT_MESAN_ARCHIVE_CACHE",
    "HISTORICAL_SOURCE_API",
    "HISTORICAL_SOURCE_PRODUCT",
    "HISTORICAL_SOURCE_VERSION",
    "SMHI_GRID_ARCHIVE_FEED_9",
    "CachedHttpRangeReader",
    "GribMessageMetadata",
    "HistoricalGridSelection",
    "HistoricalMesanAsset",
    "HistoricalMesanError",
    "HistoricalMesanHour",
    "HistoricalMesanLayout",
    "HistoricalMesanUnavailable",
    "MesanArchiveFeedClient",
    "MesanHistoricalGribPointReader",
    "discover_grib_layout",
    "historical_units_json",
    "parse_grib_message_header",
]
