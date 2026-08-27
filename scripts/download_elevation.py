"""Discover and download Lantmäteriet Markhöjdmodell tiles through STAC."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

STAC_ROOT = "https://api.lantmateriet.se/stac-hojd/v1/"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIRECTORY = PROJECT_ROOT / "src" / "data" / "elevation"
MANIFEST_FILENAME = "manifest.json"
USER_AGENT = "FungiFind-elevation-downloader/0.1"
CHUNK_SIZE = 1024 * 1024

JsonObject = dict[str, Any]
UrlOpener = Callable[..., Any]


class ElevationDownloadError(RuntimeError):
    """Raised for explicit STAC, metadata, or download validation failures."""


@dataclass(frozen=True, slots=True)
class CollectionDiscovery:
    available: tuple[JsonObject, ...]
    intersecting_elevation: tuple[JsonObject, ...]
    selected: JsonObject


@dataclass(frozen=True, slots=True)
class AssetPlan:
    collection_id: str
    item_id: str
    item_href: str | None
    item_bbox: list[float] | None
    asset_key: str
    source_href: str
    local_filename: str
    media_type: str | None
    roles: tuple[str, ...]
    title: str | None
    datetime: str | None
    start_datetime: str | None
    end_datetime: str | None
    proj_epsg: int | None
    proj_bbox: list[float] | None
    file_size: int | None
    checksum: str | None

    def manifest_record(self, *, status: str, actual_size: int) -> JsonObject:
        return {
            "collection_id": self.collection_id,
            "item_id": self.item_id,
            "item_href": self.item_href,
            "item_bbox_wgs84": self.item_bbox,
            "asset_key": self.asset_key,
            "source_href": self.source_href,
            "local_filename": self.local_filename,
            "media_type": self.media_type,
            "roles": list(self.roles),
            "title": self.title,
            "datetime": self.datetime,
            "start_datetime": self.start_datetime,
            "end_datetime": self.end_datetime,
            "proj:epsg": self.proj_epsg,
            "proj:bbox": self.proj_bbox,
            "file_size": self.file_size,
            "actual_file_size": actual_size,
            "checksum": self.checksum,
            "status": status,
            "recorded_at": datetime.now(UTC).isoformat(),
        }


class StacClient:
    """Small STAC client using only the Python standard library."""

    def __init__(self, root_url: str = STAC_ROOT, *, timeout: float = 60.0) -> None:
        self.root_url = root_url.rstrip("/") + "/"
        self.timeout = timeout

    def _get_json(self, url: str) -> JsonObject:
        request = Request(
            url,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                status = getattr(response, "status", 200)
                if not 200 <= status < 300:
                    raise ElevationDownloadError(f"HTTP {status} for {url}")
                raw = response.read()
        except HTTPError as exc:
            raise ElevationDownloadError(f"HTTP {exc.code} for {url}: {exc.reason}") from exc
        except URLError as exc:
            raise ElevationDownloadError(f"Could not reach {url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise ElevationDownloadError(f"Timed out while requesting {url}") from exc

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ElevationDownloadError(f"Invalid JSON returned by {url}") from exc
        if not isinstance(payload, dict):
            raise ElevationDownloadError(f"Expected a JSON object from {url}")
        return payload

    def list_collections(self) -> list[JsonObject]:
        payload = self._get_json(urljoin(self.root_url, "collections"))
        collections = payload.get("collections")
        if not isinstance(collections, list):
            raise ElevationDownloadError("STAC collections response has no collection list")
        return [item for item in collections if isinstance(item, dict)]

    def get_collection(self, collection_id: str) -> JsonObject:
        safe_id = collection_id.replace("/", "%2F")
        return self._get_json(urljoin(self.root_url, f"collections/{safe_id}"))

    def search(
        self,
        collection_id: str,
        bbox: tuple[float, float, float, float],
        *,
        max_items: int,
    ) -> list[JsonObject]:
        params = urlencode(
            {
                "collections": collection_id,
                "bbox": ",".join(format(value, ".12g") for value in bbox),
                "limit": min(max_items + 1, 100),
            }
        )
        next_url: str | None = urljoin(self.root_url, "search") + "?" + params
        features: list[JsonObject] = []
        while next_url is not None:
            payload = self._get_json(next_url)
            page = payload.get("features")
            if not isinstance(page, list):
                raise ElevationDownloadError("STAC search response has no feature list")
            features.extend(item for item in page if isinstance(item, dict))
            if len(features) > max_items:
                raise ElevationDownloadError(
                    f"Search exceeds the safety limit of {max_items} items; "
                    "use a smaller bbox or raise --max-items explicitly"
                )
            next_url = _next_get_link(payload, current_url=next_url)
        return features

    def content_length(self, href: str) -> int | None:
        request = Request(
            href,
            method="HEAD",
            headers={"Accept-Encoding": "identity", "User-Agent": USER_AGENT},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                value = response.headers.get("Content-Length")
        except (HTTPError, URLError, TimeoutError):
            return None
        return _optional_positive_int(value)


def _next_get_link(payload: JsonObject, *, current_url: str) -> str | None:
    links = payload.get("links", [])
    if not isinstance(links, list):
        return None
    for link in links:
        if not isinstance(link, dict) or link.get("rel") != "next":
            continue
        method = str(link.get("method", "GET")).upper()
        if method != "GET":
            raise ElevationDownloadError(
                f"STAC pagination requires unsupported HTTP method {method}"
            )
        href = link.get("href")
        if isinstance(href, str) and href:
            return urljoin(current_url, href)
    return None


def validate_bbox(values: tuple[float, float, float, float]) -> None:
    min_lon, min_lat, max_lon, max_lat = values
    if not all(math.isfinite(value) for value in values):
        raise ValueError("bbox coordinates must be finite")
    if not -180 <= min_lon < max_lon <= 180:
        raise ValueError("require -180 <= min-lon < max-lon <= 180")
    if not -90 <= min_lat < max_lat <= 90:
        raise ValueError("require -90 <= min-lat < max-lat <= 90")


def _fold_text(value: object) -> str:
    normalized = unicodedata.normalize("NFKD", str(value))
    return "".join(char for char in normalized if not unicodedata.combining(char)).casefold()


def _spatial_bboxes(collection: JsonObject) -> list[tuple[float, float, float, float]]:
    raw_bboxes = collection.get("extent", {}).get("spatial", {}).get("bbox", [])
    result: list[tuple[float, float, float, float]] = []
    if not isinstance(raw_bboxes, list):
        return result
    for raw in raw_bboxes:
        if not isinstance(raw, list):
            continue
        try:
            if len(raw) == 4:
                bbox = tuple(float(value) for value in raw)
            elif len(raw) == 6:
                bbox = (float(raw[0]), float(raw[1]), float(raw[3]), float(raw[4]))
            else:
                continue
        except (TypeError, ValueError):
            continue
        result.append(bbox)
    return result


def _intersects(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> bool:
    return (
        first[0] <= second[2]
        and first[2] >= second[0]
        and first[1] <= second[3]
        and first[3] >= second[1]
    )


def _is_elevation_grid_collection(collection: JsonObject) -> bool:
    text = _fold_text(
        " ".join(
            str(collection.get(name, ""))
            for name in ("title", "description", "keywords")
        )
    )
    if "markhojdmodell" not in text:
        return False
    item_assets = collection.get("item_assets", {})
    advertises_raster = isinstance(item_assets, dict) and any(
        _is_raster_asset(asset)
        for asset in item_assets.values()
        if isinstance(asset, dict)
    )
    return advertises_raster or "hojdgrid" in text or "geotiff" in text


def _is_raster_asset(asset: JsonObject) -> bool:
    media_type = str(asset.get("type", "")).casefold()
    href_path = urlparse(str(asset.get("href", ""))).path.casefold()
    return "geotiff" in media_type or href_path.endswith((".tif", ".tiff"))


def _collection_rank(collection: JsonObject) -> tuple[int, str]:
    title = _fold_text(collection.get("title", "")).strip()
    item_assets = collection.get("item_assets", {})
    if not isinstance(item_assets, dict):
        item_assets = {}
    raster_assets = [
        asset
        for asset in item_assets.values()
        if isinstance(asset, dict) and _is_raster_asset(asset)
    ]
    advertises_data_raster = any(
        "data" in {str(role).casefold() for role in asset.get("roles", [])}
        for asset in raster_assets
    )
    score = 0
    if title == "markhojdmodell":
        score += 100
    if raster_assets:
        score += 50
    if advertises_data_raster:
        score += 25
    if str(collection.get("stac_version", "")).startswith("1.1"):
        score += 5
    return score, str(collection.get("id", ""))


def discover_collection(
    client: StacClient,
    bbox: tuple[float, float, float, float],
) -> CollectionDiscovery:
    available = client.list_collections()
    candidate_summaries = [
        collection
        for collection in available
        if _is_elevation_grid_collection(collection)
        and any(_intersects(extent, bbox) for extent in _spatial_bboxes(collection))
    ]
    if not candidate_summaries:
        raise ElevationDownloadError(
            "No Markhöjdmodell collection extent intersects the requested bbox"
        )

    details = [client.get_collection(str(item["id"])) for item in candidate_summaries]
    details = [item for item in details if _is_elevation_grid_collection(item)]
    if not details:
        raise ElevationDownloadError("Intersecting collections failed product validation")
    selected = max(details, key=_collection_rank)
    return CollectionDiscovery(tuple(available), tuple(details), selected)


def _asset_score(key: str, asset: JsonObject) -> int | None:
    if not _is_raster_asset(asset):
        return None
    roles = {str(role).casefold() for role in asset.get("roles", [])}
    media_type = str(asset.get("type", "")).casefold()
    score = 100
    if "cloud-optimized" in media_type:
        score += 30
    if "data" in roles:
        score += 20
    if key.casefold() == "data":
        score += 10
    if roles & {"metadata", "thumbnail", "overview"}:
        score -= 100
    return score


def select_elevation_asset(item: JsonObject) -> tuple[str, JsonObject]:
    assets = item.get("assets")
    if not isinstance(assets, dict):
        raise ElevationDownloadError(f"Item {item.get('id')} has no assets")
    candidates: list[tuple[int, str, JsonObject]] = []
    for key, asset in assets.items():
        if not isinstance(key, str) or not isinstance(asset, dict):
            continue
        score = _asset_score(key, asset)
        if score is not None:
            candidates.append((score, key, asset))
    if not candidates:
        raise ElevationDownloadError(
            f"Item {item.get('id')} has no GeoTIFF elevation/grid asset"
        )
    _, key, asset = max(candidates, key=lambda candidate: (candidate[0], candidate[1]))
    return key, asset


def _self_href(item: JsonObject) -> str | None:
    links = item.get("links", [])
    if not isinstance(links, list):
        return None
    for link in links:
        if isinstance(link, dict) and link.get("rel") == "self":
            href = link.get("href")
            if isinstance(href, str):
                return href
    return None


def _optional_positive_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _proj_epsg(asset: JsonObject, properties: JsonObject) -> int | None:
    for source in (asset, properties):
        direct = _optional_positive_int(source.get("proj:epsg"))
        if direct is not None:
            return direct
        code = source.get("proj:code")
        if isinstance(code, str) and code.upper().startswith("EPSG:"):
            parsed = _optional_positive_int(code.split(":", 1)[1])
            if parsed is not None:
                return parsed
    return None


def _number_list(value: object) -> list[float] | None:
    if not isinstance(value, list):
        return None
    try:
        numbers = [float(item) for item in value]
    except (TypeError, ValueError):
        return None
    return numbers if all(math.isfinite(item) for item in numbers) else None


def _safe_filename(href: str, *, item_id: str, asset_key: str) -> str:
    parsed = urlparse(href)
    if parsed.scheme not in {"http", "https"}:
        raise ElevationDownloadError(f"Unsupported asset URL scheme for {href}")
    name = Path(unquote(parsed.path)).name
    if not name:
        name = f"{item_id}_{asset_key}.tif"
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    if safe in {"", ".", ".."}:
        raise ElevationDownloadError(f"Could not derive a safe filename from {href}")
    return safe


def build_asset_plans(
    items: list[JsonObject],
    *,
    size_lookup: Callable[[str], int | None] | None = None,
) -> list[AssetPlan]:
    plans: list[AssetPlan] = []
    seen_hrefs: set[str] = set()
    used_filenames: dict[str, str] = {}
    for item in items:
        item_id = str(item.get("id", ""))
        collection_id = str(item.get("collection", ""))
        properties = item.get("properties", {})
        if not item_id or not collection_id or not isinstance(properties, dict):
            raise ElevationDownloadError("STAC item lacks id, collection, or properties")
        asset_key, asset = select_elevation_asset(item)
        href = asset.get("href")
        if not isinstance(href, str) or not href:
            raise ElevationDownloadError(f"Selected asset {item_id}/{asset_key} has no href")
        if href in seen_hrefs:
            continue
        seen_hrefs.add(href)
        filename = _safe_filename(href, item_id=item_id, asset_key=asset_key)
        prior_href = used_filenames.get(filename.casefold())
        if prior_href is not None and prior_href != href:
            prefix = re.sub(r"[^A-Za-z0-9._-]", "_", f"{item_id}_{asset_key}")
            filename = f"{prefix}_{filename}"
        used_filenames[filename.casefold()] = href

        size = _optional_positive_int(asset.get("file:size"))
        if size is None and size_lookup is not None:
            size = size_lookup(href)
        plans.append(
            AssetPlan(
                collection_id=collection_id,
                item_id=item_id,
                item_href=_self_href(item),
                item_bbox=_number_list(item.get("bbox")),
                asset_key=asset_key,
                source_href=href,
                local_filename=filename,
                media_type=str(asset["type"]) if asset.get("type") is not None else None,
                roles=tuple(str(role) for role in asset.get("roles", [])),
                title=str(asset["title"]) if asset.get("title") is not None else None,
                datetime=properties.get("datetime"),
                start_datetime=properties.get("start_datetime"),
                end_datetime=properties.get("end_datetime"),
                proj_epsg=_proj_epsg(asset, properties),
                proj_bbox=_number_list(asset.get("proj:bbox"))
                or _number_list(properties.get("proj:bbox")),
                file_size=size,
                checksum=str(asset["file:checksum"])
                if asset.get("file:checksum") is not None
                else None,
            )
        )
    return plans


def _checksum_spec(value: str | None) -> tuple[str, str] | None:
    if value is None:
        return None
    normalized = value.strip().casefold()
    if normalized.startswith("1220") and len(normalized) == 68:
        return "sha256", normalized[4:]
    if ":" in normalized:
        algorithm, digest = normalized.split(":", 1)
        algorithm = algorithm.replace("-", "")
        if algorithm in hashlib.algorithms_available and digest:
            return algorithm, digest
    if len(normalized) == 64 and all(char in "0123456789abcdef" for char in normalized):
        return "sha256", normalized
    return None


def _file_digest(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _existing_is_complete(path: Path, plan: AssetPlan) -> bool:
    if not path.is_file():
        return False
    if plan.file_size is not None and path.stat().st_size != plan.file_size:
        return False
    checksum = _checksum_spec(plan.checksum)
    if checksum is not None:
        algorithm, expected = checksum
        return _file_digest(path, algorithm) == expected
    return plan.file_size is not None


def stream_download(
    plan: AssetPlan,
    destination: Path,
    *,
    timeout: float,
    opener: UrlOpener = urlopen,
) -> tuple[str, int]:
    if _existing_is_complete(destination, plan):
        return "already_present_validated", destination.stat().st_size

    destination.parent.mkdir(parents=True, exist_ok=True)
    part_path = destination.with_name(destination.name + ".part")
    request = Request(
        plan.source_href,
        headers={
            "Accept": "application/octet-stream",
            "Accept-Encoding": "identity",
            "User-Agent": USER_AGENT,
        },
    )
    checksum = _checksum_spec(plan.checksum)
    digest = hashlib.new(checksum[0]) if checksum is not None else None
    downloaded = 0
    try:
        try:
            response_context = opener(request, timeout=timeout)
            with response_context as response, part_path.open("wb") as output:
                status = getattr(response, "status", 200)
                if not 200 <= status < 300:
                    raise ElevationDownloadError(
                        f"HTTP {status} while downloading {plan.source_href}"
                    )
                response_size = _optional_positive_int(response.headers.get("Content-Length"))
                while chunk := response.read(CHUNK_SIZE):
                    output.write(chunk)
                    downloaded += len(chunk)
                    if digest is not None:
                        digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
        except HTTPError as exc:
            raise ElevationDownloadError(
                f"HTTP {exc.code} for {plan.source_href}: {exc.reason}"
            ) from exc
        except URLError as exc:
            raise ElevationDownloadError(
                f"Could not download {plan.source_href}: {exc.reason}"
            ) from exc
        except TimeoutError as exc:
            raise ElevationDownloadError(
                f"Timed out while downloading {plan.source_href}"
            ) from exc

        expected_sizes = {
            value for value in (plan.file_size, response_size) if value is not None
        }
        if any(downloaded != expected for expected in expected_sizes):
            expected_text = ", ".join(str(value) for value in sorted(expected_sizes))
            raise ElevationDownloadError(
                f"Incomplete download for {plan.item_id}/{plan.asset_key}: "
                f"received {downloaded} bytes, expected {expected_text}"
            )
        if checksum is not None and digest is not None and digest.hexdigest() != checksum[1]:
            raise ElevationDownloadError(
                f"Checksum mismatch for {plan.item_id}/{plan.asset_key}"
            )
        os.replace(part_path, destination)
    except Exception:
        part_path.unlink(missing_ok=True)
        raise
    return "downloaded", downloaded


def _format_size(size: int | None) -> str:
    if size is None:
        return "unknown"
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def print_discovery(discovery: CollectionDiscovery) -> None:
    print(f"Available STAC collections ({len(discovery.available)}):")
    for collection in sorted(discovery.available, key=lambda item: str(item.get("id", ""))):
        print(f"  {collection.get('id')}: {collection.get('title', '<untitled>')}")
    print("\nIntersecting Markhöjdmodell collections:")
    for collection in sorted(
        discovery.intersecting_elevation,
        key=lambda item: str(item.get("id", "")),
    ):
        print(
            f"  {collection.get('id')}: {collection.get('title')} "
            f"(STAC {collection.get('stac_version')}, license {collection.get('license')})"
        )
    selected = discovery.selected
    print("\nSelected collection (from collection metadata):")
    print(f"  id: {selected.get('id')}")
    print(f"  title: {selected.get('title')}")
    print(f"  description: {selected.get('description')}")
    print(f"  license: {selected.get('license')}")
    print(f"  STAC version: {selected.get('stac_version')}")
    print(f"  extent: {selected.get('extent')}")
    print(f"  advertised item assets: {selected.get('item_assets', {})}")


def print_items(items: list[JsonObject], plans: list[AssetPlan]) -> None:
    plan_by_item = {plan.item_id: plan for plan in plans}
    print(f"\nMatching items: {len(items)}")
    for item in items:
        item_id = str(item.get("id"))
        print(f"\nItem {item_id} (collection {item.get('collection')}):")
        assets = item.get("assets", {})
        if isinstance(assets, dict):
            for key, asset in assets.items():
                if not isinstance(asset, dict):
                    continue
                print(
                    f"  {key}: type={asset.get('type')}, roles={asset.get('roles')}, "
                    f"size={_format_size(_optional_positive_int(asset.get('file:size')))}, "
                    f"href={asset.get('href')}"
                )
        selected = plan_by_item[item_id]
        print(
            f"  -> selected {selected.asset_key}: {selected.local_filename} "
            f"({_format_size(selected.file_size)})"
        )


def _collection_manifest_metadata(collection: JsonObject) -> JsonObject:
    return {
        "id": collection.get("id"),
        "title": collection.get("title"),
        "description": collection.get("description"),
        "license": collection.get("license"),
        "stac_version": collection.get("stac_version"),
        "extent": collection.get("extent"),
        "self_href": next(
            (
                link.get("href")
                for link in collection.get("links", [])
                if isinstance(link, dict) and link.get("rel") == "self"
            ),
            None,
        ),
    }


def update_manifest(
    manifest_path: Path,
    *,
    collection: JsonObject,
    bbox: tuple[float, float, float, float],
    record: JsonObject,
) -> None:
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ElevationDownloadError(
                f"Cannot safely update invalid manifest {manifest_path}"
            ) from exc
        if not isinstance(manifest, dict):
            raise ElevationDownloadError(f"Invalid manifest root in {manifest_path}")
    else:
        manifest = {
            "schema_version": 1,
            "stac_catalog": STAC_ROOT,
            "collections": {},
            "assets": [],
        }
    collections = manifest.setdefault("collections", {})
    assets = manifest.setdefault("assets", [])
    if not isinstance(collections, dict) or not isinstance(assets, list):
        raise ElevationDownloadError(f"Invalid manifest structure in {manifest_path}")
    collection_id = str(collection.get("id"))
    collections[collection_id] = _collection_manifest_metadata(collection)
    record["requested_bbox_wgs84"] = list(bbox)
    assets[:] = [
        item
        for item in assets
        if not isinstance(item, dict) or item.get("source_href") != record["source_href"]
    ]
    assets.append(record)
    assets.sort(key=lambda item: (str(item.get("collection_id")), str(item.get("item_id"))))
    manifest["updated_at"] = datetime.now(UTC).isoformat()

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest_path.with_name(manifest_path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, manifest_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Discover and download Lantmäteriet Markhöjdmodell GeoTIFF/COG tiles "
            "intersecting a WGS84 bounding box."
        )
    )
    parser.add_argument("--min-lon", type=float, required=True)
    parser.add_argument("--min-lat", type=float, required=True)
    parser.add_argument("--max-lon", type=float, required=True)
    parser.add_argument("--max-lat", type=float, required=True)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect and list downloads without creating files",
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=25,
        help="Safety limit before downloading (default: 25)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="HTTP timeout in seconds (default: 60)",
    )
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = _parser()
    args = parser.parse_args()
    bbox = (args.min_lon, args.min_lat, args.max_lon, args.max_lat)
    try:
        validate_bbox(bbox)
        if args.max_items < 1:
            raise ValueError("--max-items must be at least 1")
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise ValueError("--timeout must be a positive finite number")

        client = StacClient(timeout=args.timeout)
        discovery = discover_collection(client, bbox)
        print_discovery(discovery)
        selected_id = str(discovery.selected["id"])
        items = client.search(selected_id, bbox, max_items=args.max_items)
        if not items:
            raise ElevationDownloadError(
                f"No items in collection {selected_id} intersect the requested bbox"
            )
        plans = build_asset_plans(items, size_lookup=client.content_length)
        print_items(items, plans)
        known_size = sum(plan.file_size or 0 for plan in plans)
        unknown_count = sum(plan.file_size is None for plan in plans)
        print(f"\nSelected elevation assets: {len(plans)}")
        print(f"Known total download size: {_format_size(known_size)}")
        if unknown_count:
            print(f"Assets with unknown size: {unknown_count}")

        if args.dry_run:
            print("\nDRY RUN: no files or manifest were written.")
            return 0

        manifest_path = OUTPUT_DIRECTORY / MANIFEST_FILENAME
        for plan in plans:
            destination = OUTPUT_DIRECTORY / plan.local_filename
            print(f"\nDownloading {plan.item_id}/{plan.asset_key} -> {destination}")
            status, actual_size = stream_download(
                plan,
                destination,
                timeout=args.timeout,
            )
            print(f"  {status}: {_format_size(actual_size)}")
            update_manifest(
                manifest_path,
                collection=discovery.selected,
                bbox=bbox,
                record=plan.manifest_record(status=status, actual_size=actual_size),
            )
        print(f"\nManifest: {manifest_path}")
        return 0
    except (ElevationDownloadError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
