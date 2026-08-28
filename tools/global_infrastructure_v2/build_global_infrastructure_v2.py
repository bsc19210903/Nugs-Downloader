#!/usr/bin/env python3
"""Build a real v2 global fiber/electric KML research release.

The build intentionally separates:
- broad research: all publicly reachable route geometry with provenance and warnings;
- open/government: geometry classified as government, public-domain, OSM/ODbL,
  or explicitly open/Creative-Commons.

It harvests bulk GeoJSON, public KMZ/KML, OpenInfraMap vector tiles, and public
ArcGIS FeatureServer/MapServer polyline layers. It does not trace raster maps
and does not represent schematic/public routes as surveyed or locate-grade data.
"""
from __future__ import annotations

import concurrent.futures as cf
import csv
import hashlib
import html
import io
import json
import math
import os
import re
import shutil
import sys
import time
import traceback
import urllib.parse
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import requests
from lxml import etree
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import mapbox_vector_tile
except Exception:
    mapbox_vector_tile = None

DATE_TAG = "2026-08-28"
ROOT = Path(os.environ.get("V2_BUILD_ROOT", "build/global_infrastructure_v2")).resolve()
CACHE = ROOT / "cache"
WORK = ROOT / "work"
OUT = ROOT / "out"
LAYERS = WORK / "layers"
for p in (CACHE, WORK, OUT, LAYERS):
    p.mkdir(parents=True, exist_ok=True)

USER_AGENT = "GlobalInfrastructureResearchMap/2.0 (+public-source research; contact via repository)"
KML_NS = "http://www.opengis.net/kml/2.2"
Q = f"{{{KML_NS}}}"

MVT_ZOOM = int(os.environ.get("MVT_ZOOM", "6"))
MVT_WORKERS = int(os.environ.get("MVT_WORKERS", "16"))
ARCGIS_MAX_ITEMS = int(os.environ.get("ARCGIS_MAX_ITEMS", "300"))
ARCGIS_MAX_LAYERS = int(os.environ.get("ARCGIS_MAX_LAYERS", "140"))
ARCGIS_MAX_PER_LAYER = int(os.environ.get("ARCGIS_MAX_PER_LAYER", "30000"))
ARCGIS_MAX_TOTAL = int(os.environ.get("ARCGIS_MAX_TOTAL", "450000"))

# KML colors are AABBGGRR.
STYLES = {
    "fiber_fna": ("ffff00ff", 2.2),       # magenta
    "fiber_operator": ("ffcc33ff", 2.4),  # pink-purple
    "fiber_open": ("ffffff00", 2.2),      # cyan
    "fiber_tiles": ("ffffcc00", 1.8),     # teal
    "fiber_arcgis": ("ffcc00cc", 2.0),    # purple
    "fiber_subsea": ("ffff6600", 2.0),    # blue
    "power_ehv": ("ff0000ff", 2.4),       # red
    "power_hv": ("ff0088ff", 1.8),        # orange
    "power_tiles": ("ff00ffff", 1.5),     # yellow
    "power_tx_arcgis": ("ff2222cc", 2.2), # dark red
    "power_dist_arcgis": ("ff00aa55", 1.5), # green
    "power_other_arcgis": ("ff999999", 1.6),
    "point": ("ffffffff", 1.0),
}

SOURCE_ROWS: list[dict[str, Any]] = []
ARCGIS_ROWS: list[dict[str, Any]] = []
TILE_ROWS: list[dict[str, Any]] = []
LAYER_RECORDS: list[dict[str, Any]] = []
ERRORS: list[dict[str, str]] = []
OPERATOR_COVERAGE: dict[str, dict[str, Any]] = defaultdict(lambda: {
    "components": 0, "vertices": 0, "sources": set(), "licenses": set(),
    "precision": set(), "urls": set(), "notes": set(),
})


def log(msg: str) -> None:
    print(time.strftime("[%H:%M:%S]"), msg, flush=True)


def err(scope: str, exc: BaseException | str) -> None:
    text = str(exc)
    ERRORS.append({"scope": scope, "error": text[:1500]})
    log(f"WARN {scope}: {text}")


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def cdata(value: Any) -> str:
    return str(value or "").replace("]]>", "]]]]><![CDATA[>")


def clean_text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    s = re.sub(r"\s+", " ", str(value).replace("\u200b", "").replace("\ufeff", "")).strip()
    return s or fallback


def safe_filename(value: str, maxlen: int = 120) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", clean_text(value)).strip("._")
    return (s or "layer")[:maxlen]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def make_session() -> requests.Session:
    s = requests.Session()
    retries = Retry(
        total=5, connect=5, read=5, status=5,
        backoff_factor=0.8,
        status_forcelist=(408, 425, 429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        respect_retry_after_header=True,
    )
    s.mount("https://", HTTPAdapter(max_retries=retries, pool_connections=64, pool_maxsize=64))
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
    return s


SESSION = make_session()


def download(url: str, dest: Path, *, max_bytes: int = 1_500_000_000, force: bool = False) -> Path:
    if dest.exists() and dest.stat().st_size > 0 and not force:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with SESSION.get(url, stream=True, timeout=(20, 180), allow_redirects=True) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        if total and total > max_bytes:
            raise RuntimeError(f"download exceeds cap ({total:,} > {max_bytes:,}): {url}")
        size = 0
        with tmp.open("wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise RuntimeError(f"download exceeded cap while streaming: {url}")
                f.write(chunk)
    if tmp.stat().st_size == 0:
        raise RuntimeError(f"empty download: {url}")
    tmp.replace(dest)
    return dest


def add_source(**kwargs: Any) -> None:
    base = {
        "source_id": "", "name": "", "domain": "", "publisher": "", "source_type": "",
        "source_url": "", "accessed": DATE_TAG, "license": "", "reuse_class": "unknown",
        "evidence_class": "", "geometry_precision": "", "status": "", "feature_count": 0,
        "vertex_count": 0, "included_broad": False, "included_open": False, "notes": "",
    }
    base.update(kwargs)
    SOURCE_ROWS.append(base)


def classify_reuse(text: str, owner: str = "", url: str = "") -> str:
    t = f"{text} {owner} {url}".lower()
    restricted = ("all rights reserved", "do not redistribute", "not for redistribution", "proprietary", "commercial license")
    if any(x in t for x in restricted):
        return "restricted"
    if any(x in t for x in ("public domain", "cc0", "creative commons", "cc-by", "cc by", "odbl", "open database license", "open data", "opendata")):
        return "open"
    gov_markers = (
        ".gov", " gov.", "government", "department of", "ministry of", "state of ",
        "county of ", "city of ", "province of ", "municipality", "national laboratory",
        "energy commission", "public utility commission", "geoscience australia", "transpower",
        "authority", "administration", "bureau of", "commission",
    )
    if any(x in t for x in gov_markers):
        return "government"
    return "unknown"


def include_open(reuse_class: str) -> bool:
    return reuse_class in {"open", "government", "public-domain", "odbl"}


def iter_line_parts(geometry: dict[str, Any] | None) -> Iterator[list[list[float]]]:
    if not geometry:
        return
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "LineString" and isinstance(coords, list):
        yield coords
    elif gtype == "MultiLineString" and isinstance(coords, list):
        for line in coords:
            if isinstance(line, list):
                yield line
    elif gtype == "GeometryCollection":
        for g in geometry.get("geometries") or []:
            yield from iter_line_parts(g)


def valid_line(line: Sequence[Sequence[Any]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for point in line:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        try:
            lon = float(point[0]); lat = float(point[1])
        except Exception:
            continue
        if math.isfinite(lon) and math.isfinite(lat) and -180.0001 <= lon <= 180.0001 and -90.0001 <= lat <= 90.0001:
            out.append((max(-180.0, min(180.0, lon)), max(-90.0, min(90.0, lat))))
    return out if len(out) >= 2 else []


def geometry_hash(lines: Sequence[Sequence[tuple[float, float]]]) -> str:
    h = hashlib.blake2b(digest_size=16)
    for line in lines:
        if not line:
            continue
        a = ";".join(f"{x:.6f},{y:.6f}" for x, y in line)
        b = ";".join(f"{x:.6f},{y:.6f}" for x, y in reversed(line))
        h.update(min(a, b).encode())
        h.update(b"|")
    return h.hexdigest()


def selected_props(props: dict[str, Any], extra: dict[str, Any] | None = None) -> dict[str, str]:
    allowed_patterns = (
        "name", "operator", "owner", "provider", "network", "status", "voltage", "circuits",
        "cables", "capacity", "length", "type", "class", "ref", "rfs", "ready", "country",
        "source", "license", "location", "medium", "burial", "feature", "objectid", "id",
    )
    out: dict[str, str] = {}
    for k, v in props.items():
        lk = str(k).lower()
        if not any(p in lk for p in allowed_patterns):
            continue
        if any(p in lk for p in ("phone", "email", "address", "contact", "telephone", "fax")):
            continue
        if v is None or isinstance(v, (dict, list)):
            continue
        s = clean_text(v)
        if s and len(s) <= 500:
            out[str(k)[:80]] = s
        if len(out) >= 24:
            break
    if extra:
        for k, v in extra.items():
            s = clean_text(v)
            if s:
                out[str(k)[:80]] = s[:1000]
    return out


def choose_name(props: dict[str, Any], fallback: str) -> str:
    keys = [
        "name_en", "NAME_EN", "english_name", "EnglishName", "name", "Name", "NAME",
        "route_name", "RouteName", "line_name", "LineName", "network", "NETWORK",
        "operator", "Operator", "OWNER", "owner", "provider", "Provider", "ref", "REF",
    ]
    for key in keys:
        val = clean_text(props.get(key))
        if val:
            return val[:200]
    return fallback


def choose_operator(props: dict[str, Any], fallback: str = "") -> str:
    for key in ("operator", "Operator", "OPERATOR", "owner", "Owner", "OWNER", "provider", "Provider", "carrier", "Carrier", "company", "Company"):
        val = clean_text(props.get(key))
        if val:
            return val[:200]
    return fallback


class KmlLayerWriter:
    def __init__(self, path: Path, title: str, description: str, style_id: str, *, visibility: int = 0):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.title = title
        self.description = description
        self.style_id = style_id
        self.visibility = visibility
        self.count = 0
        self.vertices = 0
        self.line_parts = 0
        self._f = self.path.open("w", encoding="utf-8", newline="\n")
        color, width = STYLES[style_id]
        self._f.write(f'''<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="{KML_NS}">\n<Document>\n''')
        self._f.write(f"<name>{esc(title)}</name><visibility>{visibility}</visibility>")
        self._f.write(f"<description><![CDATA[{cdata(description)}]]></description>\n")
        self._f.write(f'<Style id="route"><LineStyle><color>{color}</color><width>{width}</width></LineStyle><LabelStyle><scale>0.68</scale></LabelStyle></Style>\n')
        self._f.write('<Style id="point"><IconStyle><scale>0.7</scale></IconStyle><LabelStyle><scale>0.72</scale></LabelStyle></Style>\n')

    def write_lines(self, name: str, lines: Sequence[Sequence[tuple[float, float]]], props: dict[str, Any]) -> bool:
        clean_lines = [list(line) for line in lines if len(line) >= 2]
        if not clean_lines:
            return False
        self.count += 1
        self.line_parts += len(clean_lines)
        self.vertices += sum(len(x) for x in clean_lines)
        desc_rows = "".join(f"<tr><th align='left'>{esc(k)}</th><td>{esc(v)}</td></tr>" for k, v in props.items())
        self._f.write("<Placemark>")
        self._f.write(f"<name>{esc(name)}</name><styleUrl>#route</styleUrl>")
        self._f.write(f"<description><![CDATA[<table>{desc_rows}</table>]]></description>")
        self._f.write("<ExtendedData>")
        for k, v in props.items():
            self._f.write(f'<Data name="{esc(k)}"><value>{esc(v)}</value></Data>')
        self._f.write("</ExtendedData>")
        if len(clean_lines) > 1:
            self._f.write("<MultiGeometry>")
        for line in clean_lines:
            coords = " ".join(f"{lon:.6f},{lat:.6f},0" for lon, lat in line)
            self._f.write(f"<LineString><tessellate>1</tessellate><altitudeMode>clampToGround</altitudeMode><coordinates>{coords}</coordinates></LineString>")
        if len(clean_lines) > 1:
            self._f.write("</MultiGeometry>")
        self._f.write("</Placemark>\n")
        return True

    def write_point(self, name: str, lon: float | None, lat: float | None, props: dict[str, Any]) -> None:
        self.count += 1
        desc_rows = "".join(f"<tr><th align='left'>{esc(k)}</th><td>{esc(v)}</td></tr>" for k, v in props.items())
        self._f.write("<Placemark>")
        self._f.write(f"<name>{esc(name)}</name><styleUrl>#point</styleUrl>")
        self._f.write(f"<description><![CDATA[<table>{desc_rows}</table>]]></description>")
        self._f.write("<ExtendedData>")
        for k, v in props.items():
            self._f.write(f'<Data name="{esc(k)}"><value>{esc(v)}</value></Data>')
        self._f.write("</ExtendedData>")
        if lon is not None and lat is not None:
            self._f.write(f"<Point><coordinates>{lon:.6f},{lat:.6f},0</coordinates></Point>")
        self._f.write("</Placemark>\n")

    def close(self) -> None:
        if self._f:
            self._f.write("</Document></kml>\n")
            self._f.close()
            self._f = None

    def __enter__(self) -> "KmlLayerWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def register_operator(text: str, source: str, components: int, vertices: int, license_name: str, precision: str, url: str = "", note: str = "") -> None:
    name = clean_text(text)
    if not name:
        return
    key = canonical_operator(name)
    d = OPERATOR_COVERAGE[key]
    d["components"] += int(components)
    d["vertices"] += int(vertices)
    d["sources"].add(source)
    if license_name:
        d["licenses"].add(license_name)
    if precision:
        d["precision"].add(precision)
    if url:
        d["urls"].add(url)
    if note:
        d["notes"].add(note)


TOP50 = [
    (1, "Lumen Technologies / Level 3", "Global", ["lumen", "level 3", "level3", "centurylink"], "https://www.lumen.com/en-us/resources/network-maps.html"),
    (2, "Zayo Group", "North America / Europe", ["zayo"], "https://www.zayo.com/global-network/"),
    (3, "AT&T", "Global / United States", ["at&t", "att"], "https://www.business.att.com/products/att-network.html"),
    (4, "Verizon", "Global / United States", ["verizon", "mci"], "https://www.verizon.com/business/why-verizon/global-network/"),
    (5, "Crown Castle", "United States", ["crown castle", "lightower"], "https://www.crowncastle.com/fiber"),
    (6, "Comcast Business", "United States", ["comcast"], "https://business.comcast.com/enterprise/products/data-networking"),
    (7, "Charter Communications / Spectrum", "United States", ["charter", "spectrum"], "https://enterprise.spectrum.com/"),
    (8, "Cogent Communications", "Global", ["cogent"], "https://www.cogentco.com/en/network/network-map"),
    (9, "GTT Communications", "Global", ["gtt"], "https://www.gtt.net/us-en/network/"),
    (10, "Arelion", "Global", ["arelion", "telia carrier", "twelve99"], "https://www.arelion.com/our-network"),
    (11, "NTT Communications / NTT Ltd.", "Global", ["ntt communications", "ntt ltd", "ntt"], "https://services.global.ntt/en-us/services-and-products/global-network"),
    (12, "KDDI", "Global / Japan", ["kddi"], "https://www.kddi.com/english/corporate/kddi/network/"),
    (13, "SoftBank", "Japan / Global", ["softbank", "softbank corp"], "https://www.softbank.jp/en/corp/business/network/"),
    (14, "Colt Technology Services", "Europe / Asia", ["colt"], "https://www.colt.net/network/"),
    (15, "euNetworks", "Europe", ["eunetworks", "eu networks"], "https://eunetworks.com/network/"),
    (16, "EXA Infrastructure", "Europe", ["exa infrastructure", "interoute"], "https://exainfra.net/network/"),
    (17, "RETN", "Europe / Eurasia", ["retn"], "https://retn.net/network-map"),
    (18, "Orange / Orange International Networks", "Global", ["orange", "orange business"], "https://www.orange-business.com/en/global-network"),
    (19, "BT Group / Openreach", "United Kingdom / Global", ["bt", "openreach", "bt group"], "https://www.bt.com/about/bt/our-company/our-network"),
    (20, "Deutsche Telekom", "Europe / Global", ["deutsche telekom", "t-systems", "telekom"], "https://www.telekom.com/en/company/worldwide"),
    (21, "Telefónica / Telxius", "Europe / Americas", ["telefonica", "telefónica", "telxius"], "https://telxius.com/network/"),
    (22, "Sparkle / Telecom Italia", "Global", ["sparkle", "telecom italia", "ti sparkles"], "https://www.tisparkle.com/network"),
    (23, "Swisscom", "Switzerland / Europe", ["swisscom"], "https://www.swisscom.ch/en/business/enterprise/offer/connectivity.html"),
    (24, "Telia Company", "Nordics / Baltics", ["telia company", "telia"], "https://www.teliacompany.com/en/about-the-company/markets-and-brands"),
    (25, "Telenor", "Nordics / Asia", ["telenor"], "https://www.telenor.com/about/connectivity/"),
    (26, "Liquid Intelligent Technologies", "Africa", ["liquid intelligent", "liquid telecom", "liquid"], "https://liquid.tech/about-us/our-network/"),
    (27, "Bayobab / MTN GlobalConnect", "Africa", ["bayobab", "mtn globalconnect", "mtn"], "https://bayobab.africa/"),
    (28, "Airtel Africa", "Africa", ["airtel africa", "airtel"], "https://www.airtel.africa/"),
    (29, "MainOne / Equinix", "West Africa", ["mainone", "main one"], "https://www.mainone.net/network/"),
    (30, "Openserve / Telkom South Africa", "South Africa", ["openserve", "telkom south africa", "telkom sa"], "https://openserve.co.za/"),
    (31, "Safaricom", "East Africa", ["safaricom"], "https://www.safaricom.co.ke/"),
    (32, "Telstra", "Australia / Global", ["telstra"], "https://www.telstra.com.au/business-enterprise/about-enterprise/our-network"),
    (33, "Vocus", "Australia / New Zealand", ["vocus"], "https://www.vocus.com.au/network"),
    (34, "Spark New Zealand", "New Zealand", ["spark new zealand", "spark nz"], "https://www.spark.co.nz/online/shop/business/network"),
    (35, "China Telecom", "China / Global", ["china telecom"], "https://www.chinatelecomglobal.com/"),
    (36, "China Unicom", "China / Global", ["china unicom"], "https://www.chinaunicomglobal.com/"),
    (37, "China Mobile International", "China / Global", ["china mobile", "cmi"], "https://www.cmi.chinamobile.com/"),
    (38, "PCCW Global / Console Connect", "Asia / Global", ["pccw", "console connect"], "https://www.consoleconnect.com/network/"),
    (39, "Tata Communications", "Global / India", ["tata communications", "vsnl"], "https://www.tatacommunications.com/solutions/network/"),
    (40, "Reliance Jio", "India", ["reliance jio", "jio"], "https://www.jio.com/business/jio-dedicated-internet-access"),
    (41, "Bharti Airtel", "India / Global", ["bharti airtel", "airtel"], "https://www.airtel.in/business/b2b/network"),
    (42, "Türk Telekom", "Türkiye", ["turk telekom", "türk telekom"], "https://www.turktelekom.com.tr/"),
    (43, "stc Group", "Middle East", ["stc", "saudi telecom"], "https://www.stc.com.sa/"),
    (44, "e& / Etisalat", "Middle East / Africa / Asia", ["etisalat", "e and"], "https://www.eand.com/"),
    (45, "Ooredoo", "Middle East / North Africa / Asia", ["ooredoo"], "https://www.ooredoo.com/"),
    (46, "América Móvil / Telmex / Claro", "Latin America", ["america movil", "américa móvil", "telmex", "claro"], "https://www.americamovil.com/"),
    (47, "Internet2", "United States / R&E", ["internet2", "abilene"], "https://internet2.edu/network/"),
    (48, "GÉANT", "Europe / Global R&E", ["geant", "géant"], "https://network.geant.org/"),
    (49, "CANARIE", "Canada / R&E", ["canarie"], "https://www.canarie.ca/network/"),
    (50, "RNP", "Brazil / R&E", ["rnp", "rede nacional de ensino e pesquisa"], "https://www.rnp.br/en/network/infrastructure"),
]


def canonical_operator(name: str) -> str:
    n = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
    best = ""
    best_len = 0
    for _, canonical, _, aliases, _ in TOP50:
        for alias in aliases + [canonical.lower()]:
            a = re.sub(r"[^a-z0-9]+", " ", alias.lower()).strip()
            if a and (a in n or n in a) and len(a) > best_len:
                best = canonical
                best_len = len(a)
    return best or clean_text(name)


def process_geojson_layer(
    source_id: str, source_name: str, source_url: str, path: Path, output_rel: str,
    domain: str, style_id: str, license_name: str, reuse_class: str,
    precision: str, evidence_class: str, *, points_output_rel: str | None = None,
) -> list[dict[str, Any]]:
    log(f"Converting GeoJSON: {source_name}")
    data = json.loads(path.read_text(encoding="utf-8"))
    features = data.get("features") or []
    out_path = LAYERS / output_rel
    point_writer = None
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    with KmlLayerWriter(out_path, source_name, f"Source: {source_url}. License: {license_name}. Precision: {precision}.", style_id) as w:
        if points_output_rel:
            point_writer = KmlLayerWriter(LAYERS / points_output_rel, source_name + " — nodes", f"Point features from {source_url}", "point")
        try:
            for idx, feat in enumerate(features):
                geom = feat.get("geometry") or {}
                props = feat.get("properties") or {}
                gtype = geom.get("type")
                if gtype in {"LineString", "MultiLineString", "GeometryCollection"}:
                    lines = [valid_line(x) for x in iter_line_parts(geom)]
                    lines = [x for x in lines if x]
                    if not lines:
                        continue
                    digest = geometry_hash(lines)
                    if digest in seen:
                        continue
                    seen.add(digest)
                    name = choose_name(props, f"{source_name} route {idx + 1:,}")
                    operator = choose_operator(props)
                    p = selected_props(props, {
                        "Source": source_name, "Source URL": source_url, "License": license_name,
                        "Evidence class": evidence_class, "Geometry precision": precision,
                    })
                    w.write_lines(name, lines, p)
                    if operator:
                        register_operator(operator, source_name, len(lines), sum(len(x) for x in lines), license_name, precision, source_url)
                elif point_writer and gtype == "Point":
                    coords = geom.get("coordinates") or []
                    if len(coords) >= 2:
                        try:
                            lon, lat = float(coords[0]), float(coords[1])
                        except Exception:
                            continue
                        point_writer.write_point(choose_name(props, f"{source_name} node {idx+1}"), lon, lat, selected_props(props, {
                            "Source": source_name, "License": license_name,
                        }))
        finally:
            if point_writer:
                point_writer.close()
    rec = {
        "path": output_rel, "name": source_name, "domain": domain, "style": style_id,
        "reuse_class": reuse_class, "source_id": source_id, "source_url": source_url,
        "license": license_name, "feature_count": w.count, "line_parts": w.line_parts,
        "vertex_count": w.vertices, "broad": True, "open": include_open(reuse_class),
        "precision": precision, "evidence_class": evidence_class,
    }
    records.append(rec); LAYER_RECORDS.append(rec)
    if point_writer and point_writer.count:
        prec = dict(rec)
        prec.update({"path": points_output_rel, "name": source_name + " — nodes", "domain": domain + "_nodes", "style": "point", "feature_count": point_writer.count, "line_parts": 0, "vertex_count": 0})
        records.append(prec); LAYER_RECORDS.append(prec)
    add_source(
        source_id=source_id, name=source_name, domain=domain, publisher=source_name, source_type="GeoJSON",
        source_url=source_url, license=license_name, reuse_class=reuse_class, evidence_class=evidence_class,
        geometry_precision=precision, status="harvested", feature_count=w.line_parts, vertex_count=w.vertices,
        included_broad=True, included_open=include_open(reuse_class), notes=f"Converted from {path.name}; {w.count:,} placemarks / {w.line_parts:,} line parts.",
    )
    return records


def acquire_core_sources() -> dict[str, Path]:
    files: dict[str, Path] = {}
    urls = {
        "power_ehv": "https://raw.githubusercontent.com/lyralai/global-power-lines/master/data/global_ehv_simple.geojson",
        "power_hv": "https://raw.githubusercontent.com/lyralai/global-power-lines/master/data/global_hv_simple.geojson",
        "openfiber": "https://raw.githubusercontent.com/Jastman/OpenFiberMap/claude/openfibermap-project-8z3UL/public/data/fiber-global.geojson",
        "subsea_routes": "https://raw.githubusercontent.com/JesseCallahanBryant/undersea-cables/main/data/cable-geo.json",
        "subsea_meta": "https://raw.githubusercontent.com/JesseCallahanBryant/undersea-cables/main/data/cables.csv",
        "subsea_points": "https://raw.githubusercontent.com/JesseCallahanBryant/undersea-cables/main/data/landing-point-geo.json",
    }
    for key, url in urls.items():
        suffix = ".csv" if key == "subsea_meta" else ".geojson"
        dest = CACHE / f"{key}{suffix}"
        try:
            files[key] = download(url, dest)
            log(f"Downloaded {key}: {dest.stat().st_size:,} bytes")
        except Exception as e:
            err(f"download:{key}", e)
            add_source(source_id=key, name=key, source_url=url, status="failed", notes=str(e))
    return files


def acquire_fna() -> Path | None:
    dest = CACHE / "AllMemberFiber.kmz"
    if dest.exists() and zipfile.is_zipfile(dest):
        return dest
    try:
        import gdown
        log("Downloading Fiber Network Alliance public KMZ")
        result = gdown.download(id="1ulsCVWisT6Yy4_XQET0RN82qeOpoOuc9", output=str(dest), quiet=False, fuzzy=True)
        if not result or not dest.exists() or not zipfile.is_zipfile(dest):
            raise RuntimeError("gdown did not return a valid KMZ")
        return dest
    except Exception as e:
        err("FNA download", e)
        return None


def iter_kml_line_placemarks(kml_path: Path) -> Iterator[tuple[str, list[list[tuple[float, float]]], dict[str, str]]]:
    context = etree.iterparse(str(kml_path), events=("end",), tag=Q + "Placemark", huge_tree=True, recover=True)
    for _, pm in context:
        name = clean_text(pm.findtext(Q + "name"), "Unnamed route")
        lines: list[list[tuple[float, float]]] = []
        for ls in pm.findall(".//" + Q + "LineString"):
            text = ls.findtext(Q + "coordinates") or ""
            pts: list[tuple[float, float]] = []
            for tok in text.split():
                parts = tok.split(",")
                if len(parts) < 2:
                    continue
                try:
                    lon, lat = float(parts[0]), float(parts[1])
                except Exception:
                    continue
                if -180 <= lon <= 180 and -90 <= lat <= 90:
                    pts.append((lon, lat))
            if len(pts) >= 2:
                lines.append(pts)
        props: dict[str, str] = {}
        for d in pm.findall(".//" + Q + "Data"):
            key = clean_text(d.get("name"))
            val = clean_text(d.findtext(Q + "value"))
            if key and val and not any(x in key.lower() for x in ("phone", "email", "address", "contact", "telephone", "fax")):
                props[key] = val
        if lines:
            yield name, lines, props
        parent = pm.getparent()
        pm.clear()
        if parent is not None:
            while pm.getprevious() is not None:
                del parent[0]


def process_fna(kmz: Path) -> None:
    temp = WORK / "fna_doc.kml"
    with zipfile.ZipFile(kmz) as z:
        candidates = [n for n in z.namelist() if n.lower().endswith(".kml")]
        if not candidates:
            raise RuntimeError("FNA KMZ has no KML")
        with z.open(candidates[0]) as src, temp.open("wb") as dst:
            shutil.copyfileobj(src, dst)
    out_rel = "fiber/fna_member_routes_research_only.kml"
    seen_by_provider: dict[str, set[str]] = defaultdict(set)
    provider_counts: Counter[str] = Counter()
    with KmlLayerWriter(
        LAYERS / out_rel,
        "Fiber Network Alliance member routes — 2020 research snapshot",
        "Publicly downloadable operator/member map. No explicit open-data licence located; broad-research edition only. Contact/address fields removed. Not surveyed geometry.",
        "fiber_fna",
    ) as w:
        for name, lines, props in iter_kml_line_placemarks(temp):
            provider = clean_text(name, "Unnamed FNA member")
            digest = geometry_hash(lines)
            if digest in seen_by_provider[provider]:
                continue
            seen_by_provider[provider].add(digest)
            cleanprops = selected_props(props, {
                "Provider": provider,
                "Source": "Fiber Network Alliance public member map",
                "Snapshot": "2020-02-21",
                "License boundary": "No explicit open-data license located; research use only",
                "Evidence class": "G3 operator/member-published map",
            })
            w.write_lines(provider, lines, cleanprops)
            provider_counts[provider] += len(lines)
            register_operator(provider, "FNA member map", len(lines), sum(len(x) for x in lines), "Unclear/publicly downloadable", "operator/member cartography", "https://drive.google.com/file/d/1ulsCVWisT6Yy4_XQET0RN82qeOpoOuc9/view")
    rec = {
        "path": out_rel, "name": "Fiber Network Alliance member routes — 2020 research snapshot", "domain": "fiber",
        "style": "fiber_fna", "reuse_class": "unknown", "source_id": "FNA2020",
        "source_url": "https://drive.google.com/file/d/1ulsCVWisT6Yy4_XQET0RN82qeOpoOuc9/view",
        "license": "No explicit open-data license located", "feature_count": w.count, "line_parts": w.line_parts,
        "vertex_count": w.vertices, "broad": True, "open": False, "precision": "operator/member cartography",
        "evidence_class": "G3",
    }
    LAYER_RECORDS.append(rec)
    add_source(
        source_id="FNA2020", name=rec["name"], domain="fiber", publisher="Fiber Network Alliance",
        source_type="KMZ", source_url=rec["source_url"], license=rec["license"], reuse_class="unknown",
        evidence_class="G3", geometry_precision=rec["precision"], status="harvested",
        feature_count=w.line_parts, vertex_count=w.vertices, included_broad=True, included_open=False,
        notes=f"{len(provider_counts)} providers; contact/address fields removed; exact within-provider duplicates removed.",
    )
    stats_path = OUT / "fna_provider_stats_v2.csv"
    with stats_path.open("w", newline="", encoding="utf-8") as f:
        dw = csv.DictWriter(f, fieldnames=["provider", "line_components"])
        dw.writeheader()
        for provider, count in provider_counts.most_common():
            dw.writerow({"provider": provider, "line_components": count})
    temp.unlink(missing_ok=True)


def process_zayo() -> None:
    url = "https://www.dropbox.com/scl/fi/ubw1bkngb77pb4q5jxfhj/Zayo-Network-Map-10.30.25.kmz?rlkey=8v72usduruuyxrv60sr2y0zy0&dl=1"
    dest = CACHE / "Zayo-Network-Map-10.30.25.kmz"
    try:
        download(url, dest, max_bytes=500_000_000)
        if not zipfile.is_zipfile(dest):
            raise RuntimeError("Zayo response is not a KMZ")
        temp = WORK / "zayo_doc.kml"
        with zipfile.ZipFile(dest) as z:
            candidates = [n for n in z.namelist() if n.lower().endswith(".kml")]
            if not candidates:
                raise RuntimeError("Zayo KMZ has no KML")
            with z.open(candidates[0]) as src, temp.open("wb") as dst:
                shutil.copyfileobj(src, dst)
        out_rel = "fiber/zayo_operator_map_2025_research_only.kml"
        with KmlLayerWriter(LAYERS / out_rel, "Zayo published network map — 2025", "Operator-published public KMZ; source terms apply; broad-research edition only.", "fiber_operator") as w:
            seen: set[str] = set()
            for name, lines, props in iter_kml_line_placemarks(temp):
                digest = geometry_hash(lines)
                if digest in seen:
                    continue
                seen.add(digest)
                p = selected_props(props, {"Operator": "Zayo", "Source URL": url, "Evidence class": "G3 operator-published map", "License": "Publisher terms / unclear redistribution"})
                w.write_lines(name or "Zayo route", lines, p)
                register_operator("Zayo", "Zayo public KMZ", len(lines), sum(len(x) for x in lines), "Publisher terms", "operator cartography", url)
        rec = {"path": out_rel, "name": "Zayo published network map — 2025", "domain": "fiber", "style": "fiber_operator", "reuse_class": "unknown", "source_id": "ZAYO2025", "source_url": url, "license": "Publisher terms", "feature_count": w.count, "line_parts": w.line_parts, "vertex_count": w.vertices, "broad": True, "open": False, "precision": "operator cartography", "evidence_class": "G3"}
        LAYER_RECORDS.append(rec)
        add_source(source_id="ZAYO2025", name=rec["name"], domain="fiber", publisher="Zayo", source_type="KMZ", source_url=url, license="Publisher terms / unclear redistribution", reuse_class="unknown", evidence_class="G3", geometry_precision="operator cartography", status="harvested", feature_count=w.line_parts, vertex_count=w.vertices, included_broad=True, included_open=False, notes="Sanitized conversion of operator-published public KMZ.")
        temp.unlink(missing_ok=True)
    except Exception as e:
        err("Zayo", e)
        add_source(source_id="ZAYO2025", name="Zayo published network map", domain="fiber", publisher="Zayo", source_type="KMZ", source_url=url, reuse_class="unknown", status="failed", notes=str(e))


def process_subsea(files: dict[str, Path]) -> None:
    path = files.get("subsea_routes")
    if not path:
        return
    meta: dict[str, dict[str, str]] = {}
    if files.get("subsea_meta"):
        with files["subsea_meta"].open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                keys = [clean_text(row.get(k)) for k in ("id", "cable_id", "slug", "name")]
                for k in keys:
                    if k:
                        meta[k.lower()] = row
    data = json.loads(path.read_text(encoding="utf-8"))
    out_rel = "fiber/submarine_cables_2026_cc_by_nc_sa.kml"
    with KmlLayerWriter(LAYERS / out_rel, "Global submarine fiber cables — refreshed 2026", "TeleGeography-derived public route dataset, CC BY-NC-SA 3.0. Broad research/noncommercial use; cartographic routes, not surveyed as-builts.", "fiber_subsea") as w:
        for i, feat in enumerate(data.get("features") or []):
            geom = feat.get("geometry") or {}
            props = feat.get("properties") or {}
            lines = [valid_line(x) for x in iter_line_parts(geom)]
            lines = [x for x in lines if x]
            if not lines:
                continue
            name = choose_name(props, f"Submarine cable {i+1}")
            fid = clean_text(props.get("id") or props.get("slug") or name).lower()
            row = meta.get(fid) or meta.get(name.lower()) or {}
            owner = clean_text(row.get("owners") or row.get("owner") or props.get("owners"))
            p = selected_props({**row, **props}, {
                "Source": "JesseCallahanBryant/undersea-cables (TeleGeography-derived)",
                "Source URL": "https://github.com/JesseCallahanBryant/undersea-cables",
                "License": "CC BY-NC-SA 3.0",
                "Evidence class": "G2/G3 cartographic route",
                "Geometry precision": "schematic/cartographic",
            })
            w.write_lines(name, lines, p)
            if owner:
                for part in re.split(r"[,;/]|\band\b", owner):
                    if clean_text(part):
                        register_operator(part, "Global submarine cable dataset", len(lines), sum(len(x) for x in lines), "CC BY-NC-SA 3.0", "cartographic", "https://github.com/JesseCallahanBryant/undersea-cables")
    rec = {"path": out_rel, "name": "Global submarine fiber cables — refreshed 2026", "domain": "fiber", "style": "fiber_subsea", "reuse_class": "noncommercial", "source_id": "SUBSEA2026", "source_url": "https://github.com/JesseCallahanBryant/undersea-cables", "license": "CC BY-NC-SA 3.0", "feature_count": w.count, "line_parts": w.line_parts, "vertex_count": w.vertices, "broad": True, "open": False, "precision": "schematic/cartographic", "evidence_class": "G2/G3"}
    LAYER_RECORDS.append(rec)
    add_source(source_id="SUBSEA2026", name=rec["name"], domain="fiber", publisher="TeleGeography-derived public dataset", source_type="GeoJSON", source_url=rec["source_url"], license=rec["license"], reuse_class="noncommercial", evidence_class="G2/G3", geometry_precision=rec["precision"], status="harvested", feature_count=w.line_parts, vertex_count=w.vertices, included_broad=True, included_open=False, notes="Refreshed 2026 route dataset; noncommercial/share-alike boundary retained.")


def mvt_local_to_lonlat(coord: Sequence[Any], z: int, tx: int, ty: int, extent: int) -> list[float]:
    lx = float(coord[0]); ly = float(coord[1])
    n = 2 ** z
    gx = (tx + lx / extent) / n
    gy = (ty + ly / extent) / n
    lon = gx * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * gy))))
    return [lon, lat]


def mvt_geom_to_lonlat(geom: dict[str, Any], z: int, x: int, y: int, extent: int) -> dict[str, Any]:
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if gtype == "LineString":
        return {"type": gtype, "coordinates": [mvt_local_to_lonlat(p, z, x, y, extent) for p in coords or []]}
    if gtype == "MultiLineString":
        return {"type": gtype, "coordinates": [[mvt_local_to_lonlat(p, z, x, y, extent) for p in line] for line in coords or []]}
    return geom


def fetch_tile(tile: tuple[int, int, int]) -> tuple[tuple[int, int, int], bytes | None, str | None]:
    z, x, y = tile
    url = f"https://openinframap.org/tiles/{z}/{x}/{y}.pbf"
    try:
        r = SESSION.get(url, timeout=(15, 45))
        if r.status_code in (204, 404):
            return tile, None, None
        r.raise_for_status()
        ct = (r.headers.get("content-type") or "").lower()
        if "text/html" in ct or not r.content:
            return tile, None, f"unexpected content-type {ct}"
        return tile, r.content, None
    except Exception as e:
        return tile, None, str(e)


def process_openinframap_tiles() -> None:
    if mapbox_vector_tile is None:
        err("OpenInfraMap", "mapbox_vector_tile unavailable")
        return
    z = MVT_ZOOM
    tiles = [(z, x, y) for x in range(2 ** z) for y in range(2 ** z)]
    out_tele = "fiber/openinframap_vector_tiles_telecom_z%d.kml" % z
    out_power = "power/openinframap_vector_tiles_power_z%d.kml" % z
    tele_seen: set[str] = set(); power_seen: set[str] = set()
    success = empty = failed = 0; bytes_downloaded = 0
    log(f"Decoding {len(tiles):,} OpenInfraMap vector tiles at z={z}")
    with KmlLayerWriter(LAYERS / out_tele, f"OpenInfraMap / OSM telecom lines — vector tiles z{z}", "Actual MVT-to-KML conversion of the public OpenInfraMap telecoms_communication_line layer. OSM/ODbL; tile-clipped generalized display geometry.", "fiber_tiles") as tw, \
         KmlLayerWriter(LAYERS / out_power, f"OpenInfraMap / OSM power lines — vector tiles z{z}", "Actual MVT-to-KML conversion of the public OpenInfraMap power_line layer. OSM/ODbL; tile-clipped generalized display geometry.", "power_tiles") as pw:
        with cf.ThreadPoolExecutor(max_workers=MVT_WORKERS) as ex:
            futs = {ex.submit(fetch_tile, t): t for t in tiles}
            for idx, fut in enumerate(cf.as_completed(futs), 1):
                tile, payload, ferr = fut.result()
                if ferr:
                    failed += 1
                    if failed <= 20:
                        err(f"MVT {tile}", ferr)
                    continue
                if not payload:
                    empty += 1
                    continue
                success += 1; bytes_downloaded += len(payload)
                z0, x0, y0 = tile
                try:
                    decoded = mapbox_vector_tile.decode(payload, default_options={"y_coord_down": True})
                except TypeError:
                    decoded = mapbox_vector_tile.decode(payload, y_coord_down=True)
                except Exception as e:
                    failed += 1
                    if failed <= 20:
                        err(f"MVT decode {tile}", e)
                    continue
                for layer_name, writer, domain, seen in (
                    ("telecoms_communication_line", tw, "fiber", tele_seen),
                    ("power_line", pw, "power", power_seen),
                ):
                    layer = decoded.get(layer_name) or {}
                    extent = int(layer.get("extent") or 4096)
                    for feat in layer.get("features") or []:
                        geom = mvt_geom_to_lonlat(feat.get("geometry") or {}, z0, x0, y0, extent)
                        lines = [valid_line(x) for x in iter_line_parts(geom)]
                        lines = [x for x in lines if x]
                        if not lines:
                            continue
                        props = feat.get("properties") or {}
                        source_id = feat.get("id")
                        digest = geometry_hash(lines)
                        dedupe_key = f"{source_id}:{digest}" if source_id is not None else digest
                        if dedupe_key in seen:
                            continue
                        seen.add(dedupe_key)
                        fallback = "OSM telecom line" if domain == "fiber" else "OSM power line"
                        name = choose_name(props, fallback)
                        operator = choose_operator(props)
                        p = selected_props(props, {
                            "Source": "OpenInfraMap vector tile",
                            "Tile": f"{z0}/{x0}/{y0}",
                            "Source feature ID": source_id,
                            "License": "ODbL 1.0 / © OpenStreetMap contributors",
                            "Evidence class": "G2 community-mapped display tile",
                            "Geometry precision": f"z{z0} generalized and tile-clipped",
                        })
                        writer.write_lines(name, lines, p)
                        if operator:
                            register_operator(operator, "OpenInfraMap vector tiles", len(lines), sum(len(x) for x in lines), "ODbL 1.0", f"z{z0} generalized", "https://openinframap.org/")
                if idx % 250 == 0:
                    log(f"MVT progress {idx:,}/{len(tiles):,}; successes={success:,}; telecom pieces={tw.line_parts:,}; power pieces={pw.line_parts:,}")
    for rel, name, domain, style, w in (
        (out_tele, f"OpenInfraMap / OSM telecom lines — vector tiles z{z}", "fiber", "fiber_tiles", tw),
        (out_power, f"OpenInfraMap / OSM power lines — vector tiles z{z}", "power", "power_tiles", pw),
    ):
        rec = {"path": rel, "name": name, "domain": domain, "style": style, "reuse_class": "odbl", "source_id": f"OIM-MVT-{domain}-z{z}", "source_url": "https://openinframap.org/tiles/{z}/{x}/{y}.pbf", "license": "ODbL 1.0 / © OpenStreetMap contributors", "feature_count": w.count, "line_parts": w.line_parts, "vertex_count": w.vertices, "broad": True, "open": True, "precision": f"z{z} generalized tile geometry", "evidence_class": "G2"}
        LAYER_RECORDS.append(rec)
        add_source(source_id=rec["source_id"], name=name, domain=domain, publisher="OpenInfraMap / OpenStreetMap contributors", source_type="Mapbox Vector Tiles", source_url=rec["source_url"], license=rec["license"], reuse_class="odbl", evidence_class="G2", geometry_precision=rec["precision"], status="harvested" if success else "failed", feature_count=w.line_parts, vertex_count=w.vertices, included_broad=True, included_open=True, notes=f"Decoded at zoom {z}; {success:,} non-empty tiles, {empty:,} empty, {failed:,} failed; {bytes_downloaded:,} bytes downloaded. Geometry is tile-clipped.")
    TILE_ROWS.append({
        "tileset": "OpenInfraMap", "endpoint": "https://openinframap.org/tiles/{z}/{x}/{y}.pbf", "zoom": z,
        "tiles_requested": len(tiles), "tiles_success": success, "tiles_empty": empty, "tiles_failed": failed,
        "bytes_downloaded": bytes_downloaded, "telecom_line_parts": tw.line_parts, "telecom_vertices": tw.vertices,
        "power_line_parts": pw.line_parts, "power_vertices": pw.vertices, "decoder": "mapbox_vector_tile",
        "conversion": "MVT tile coordinates transformed to EPSG:4326; features retained as tile-clipped line pieces",
        "license": "ODbL 1.0 / © OpenStreetMap contributors",
    })


FIBER_TERMS = [
    "fiber optic", "fibre optic", "fiber route", "fibre route", "dark fiber", "dark fibre", "middle mile",
    "broadband backbone", "telecommunications cable", "communication cable", "communication line", "optical fiber",
    "optical fibre", "fiber network", "fibre network", "fiber infrastructure", "fibre infrastructure",
    "red de fibra óptica", "fibra óptica", "ligne fibre optique", "réseau fibre", "linha de fibra", "fibra optica",
    "glasfasernetz", "glasfaser", "光ファイバー", "光纤网络",
]
POWER_TERMS = [
    "electric transmission line", "power transmission line", "electric distribution line", "power distribution line",
    "overhead power line", "underground electric cable", "high voltage line", "medium voltage line", "low voltage line",
    "electricity network", "electric grid line", "transmission lines", "distribution lines", "power lines",
    "línea de transmisión", "red eléctrica", "ligne de transport électrique", "ligne électrique",
    "linha de transmissão", "rede elétrica", "stromleitung", "hochspannungsleitung", "送電線", "输电线路",
]


def classify_domain(text: str) -> tuple[str | None, int]:
    t = text.lower()
    fiber_hits = sum(1 for term in FIBER_TERMS if term in t)
    power_hits = sum(1 for term in POWER_TERMS if term in t)
    # Broader signals for layer names.
    if any(x in t for x in ("fiber", "fibre", "optic", "telecom", "broadband", "communication cable", "glasfaser", "fibra")):
        fiber_hits += 2
    if any(x in t for x in ("power", "electric", "transmission", "distribution", "voltage", "grid", "utility line", "strom", "送電", "输电")):
        power_hits += 2
    if fiber_hits == power_hits == 0:
        return None, 0
    if fiber_hits > power_hits:
        return "fiber", fiber_hits
    return "power", power_hits


def classify_power_subtype(text: str) -> str:
    t = text.lower()
    if any(x in t for x in ("distribution", "medium voltage", "low voltage", "minor line", "primary line", "secondary line", "lv ", "mv ")):
        return "distribution"
    if any(x in t for x in ("transmission", "high voltage", "extra high", "ehv", "hv ")):
        return "transmission"
    return "other"


def arcgis_search_items() -> list[dict[str, Any]]:
    queries = FIBER_TERMS + POWER_TERMS
    items: dict[str, dict[str, Any]] = {}
    for qi, term in enumerate(queries, 1):
        q = f'({term}) AND (type:"Feature Service" OR type:"Map Service") AND access:public'
        try:
            r = SESSION.get("https://www.arcgis.com/sharing/rest/search", params={
                "q": q, "num": 100, "start": 1, "sortField": "numViews", "sortOrder": "desc", "f": "json",
            }, timeout=(15, 60))
            r.raise_for_status()
            data = r.json()
            for item in data.get("results") or []:
                url = clean_text(item.get("url"))
                if url and ("FeatureServer" in url or "MapServer" in url):
                    items[item["id"]] = item
        except Exception as e:
            err(f"ArcGIS search {term}", e)
        if qi % 10 == 0:
            log(f"ArcGIS catalogue searches {qi}/{len(queries)}; unique services={len(items):,}")
        time.sleep(0.08)
    ranked: list[tuple[int, dict[str, Any]]] = []
    for item in items.values():
        text = " ".join(clean_text(item.get(k)) for k in ("title", "snippet", "description", "tags", "typeKeywords"))
        domain, score = classify_domain(text)
        if not domain:
            continue
        score += min(10, int(math.log10(max(1, int(item.get("numViews") or 0))) * 2))
        if item.get("type") == "Feature Service":
            score += 2
        item["_domain"] = domain; item["_score"] = score
        ranked.append((score, item))
    ranked.sort(key=lambda x: (x[0], int(x[1].get("numViews") or 0)), reverse=True)
    selected = [x[1] for x in ranked[:ARCGIS_MAX_ITEMS]]
    (OUT / "arcgis_catalogue_search_results.json").write_text(json.dumps(selected, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"ArcGIS candidate services selected: {len(selected):,} of {len(items):,}")
    return selected


def arcgis_item_metadata(item_id: str) -> dict[str, Any]:
    try:
        r = SESSION.get(f"https://www.arcgis.com/sharing/rest/content/items/{item_id}", params={"f": "json"}, timeout=(10, 30))
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


def arcgis_layer_lines(layer_url: str, layer_meta: dict[str, Any], cap: int) -> tuple[Iterator[tuple[dict[str, Any], list[list[tuple[float, float]]]]], int, bool]:
    oid_field = clean_text(layer_meta.get("objectIdField") or layer_meta.get("objectIdFieldName"))
    if not oid_field:
        for f in layer_meta.get("fields") or []:
            if f.get("type") == "esriFieldTypeOID":
                oid_field = f.get("name"); break
    max_record = min(2000, int(layer_meta.get("maxRecordCount") or 1000))
    try:
        cr = SESSION.get(layer_url + "/query", params={"where": "1=1", "returnCountOnly": "true", "f": "json"}, timeout=(10, 45))
        cr.raise_for_status(); count = int(cr.json().get("count") or 0)
    except Exception:
        count = 0
    partial = count > cap if count else False

    def iterator() -> Iterator[tuple[dict[str, Any], list[list[tuple[float, float]]]]]:
        emitted = 0; offset = 0
        while emitted < cap:
            page = min(max_record, cap - emitted)
            params = {
                "where": "1=1", "outFields": "*", "returnGeometry": "true", "outSR": "4326",
                "resultOffset": offset, "resultRecordCount": page, "f": "json",
            }
            if oid_field:
                params["orderByFields"] = oid_field
            r = SESSION.get(layer_url + "/query", params=params, timeout=(15, 120))
            r.raise_for_status(); data = r.json()
            if data.get("error"):
                raise RuntimeError(data["error"])
            feats = data.get("features") or []
            if not feats:
                break
            for feat in feats:
                geom = feat.get("geometry") or {}
                paths = geom.get("paths") or []
                lines = [valid_line(p) for p in paths]
                lines = [x for x in lines if x]
                if lines:
                    yield feat.get("attributes") or {}, lines
                    emitted += 1
                    if emitted >= cap:
                        break
            offset += len(feats)
            if len(feats) < page and not data.get("exceededTransferLimit"):
                break
            if not layer_meta.get("advancedQueryCapabilities", {}).get("supportsPagination", True) and offset >= max_record:
                break
    return iterator(), count, partial


def harvest_arcgis() -> None:
    items = arcgis_search_items()
    successful = 0; total_features = 0
    for item_idx, item in enumerate(items, 1):
        if successful >= ARCGIS_MAX_LAYERS or total_features >= ARCGIS_MAX_TOTAL:
            break
        item_id = item["id"]; service_url = clean_text(item.get("url")); title = clean_text(item.get("title"), item_id)
        meta_item = arcgis_item_metadata(item_id)
        license_text = " ".join(clean_text(meta_item.get(k) or item.get(k)) for k in ("licenseInfo", "accessInformation", "termsOfUse", "description", "snippet"))
        owner = clean_text(meta_item.get("owner") or item.get("owner"))
        reuse = classify_reuse(license_text, owner, service_url)
        try:
            sr = SESSION.get(service_url, params={"f": "json"}, timeout=(15, 60))
            sr.raise_for_status(); service = sr.json()
            if service.get("error"):
                raise RuntimeError(service["error"])
        except Exception as e:
            ARCGIS_ROWS.append({"item_id": item_id, "item_title": title, "service_url": service_url, "layer_id": "", "layer_name": "", "domain": item.get("_domain"), "reuse_class": reuse, "status": "service_failed", "reported_count": "", "harvested_features": 0, "line_parts": 0, "vertices": 0, "partial": "", "error": str(e)[:500]})
            continue
        layers = service.get("layers") or []
        for layer_stub in layers:
            if successful >= ARCGIS_MAX_LAYERS or total_features >= ARCGIS_MAX_TOTAL:
                break
            layer_id = layer_stub.get("id"); layer_name = clean_text(layer_stub.get("name"), f"Layer {layer_id}")
            layer_url = service_url.rstrip("/") + f"/{layer_id}"
            try:
                lr = SESSION.get(layer_url, params={"f": "json"}, timeout=(10, 45)); lr.raise_for_status(); layer_meta = lr.json()
                if layer_meta.get("geometryType") != "esriGeometryPolyline":
                    continue
                combined = " ".join([title, layer_name, clean_text(layer_meta.get("description")), clean_text(layer_meta.get("copyrightText"))])
                domain, score = classify_domain(combined)
                if not domain or score < 1:
                    continue
                cap = min(ARCGIS_MAX_PER_LAYER, ARCGIS_MAX_TOTAL - total_features)
                iterator, reported_count, partial = arcgis_layer_lines(layer_url, layer_meta, cap)
                subtype = classify_power_subtype(combined) if domain == "power" else "fiber"
                style = "fiber_arcgis" if domain == "fiber" else ("power_tx_arcgis" if subtype == "transmission" else "power_dist_arcgis" if subtype == "distribution" else "power_other_arcgis")
                rel = f"arcgis/{domain}/{safe_filename(title)}__{item_id}_{layer_id}.kml"
                source_name = f"{title} — {layer_name}"
                harvested = 0
                with KmlLayerWriter(LAYERS / rel, source_name, f"Public ArcGIS service: {layer_url}. Reuse classification: {reuse}. Source metadata controls.", style) as w:
                    for attrs, lines in iterator:
                        oid = attrs.get(layer_meta.get("objectIdField") or "OBJECTID")
                        name = choose_name(attrs, f"{layer_name} feature {oid if oid is not None else harvested + 1}")
                        operator = choose_operator(attrs, owner)
                        p = selected_props(attrs, {
                            "Source item": title, "Source layer": layer_name, "Source URL": layer_url,
                            "Publisher/owner": owner, "Reuse class": reuse,
                            "License/attribution": clean_text(license_text)[:1000] or "Not stated in item metadata",
                            "Evidence class": "G1/G2 public GIS service",
                            "Geometry precision": "source GIS geometry; positional accuracy not independently verified",
                        })
                        w.write_lines(name, lines, p); harvested += 1
                        if operator:
                            register_operator(operator, source_name, len(lines), sum(len(x) for x in lines), clean_text(license_text)[:150] or reuse, "source GIS geometry", layer_url)
                if harvested == 0:
                    (LAYERS / rel).unlink(missing_ok=True)
                    continue
                total_features += harvested; successful += 1
                rec = {"path": rel, "name": source_name, "domain": domain, "style": style, "reuse_class": reuse, "source_id": f"AGOL-{item_id}-{layer_id}", "source_url": layer_url, "license": clean_text(license_text)[:1000] or "Not stated", "feature_count": w.count, "line_parts": w.line_parts, "vertex_count": w.vertices, "broad": True, "open": include_open(reuse), "precision": "source GIS geometry", "evidence_class": "G1/G2", "power_subtype": subtype}
                LAYER_RECORDS.append(rec)
                ARCGIS_ROWS.append({"item_id": item_id, "item_title": title, "service_url": service_url, "layer_id": layer_id, "layer_name": layer_name, "domain": domain, "power_subtype": subtype, "owner": owner, "reuse_class": reuse, "license_summary": clean_text(license_text)[:500], "status": "harvested", "reported_count": reported_count, "harvested_features": harvested, "line_parts": w.line_parts, "vertices": w.vertices, "partial": partial or (reported_count and harvested < reported_count), "output_layer": rel, "error": ""})
                add_source(source_id=rec["source_id"], name=source_name, domain=domain, publisher=owner or title, source_type="ArcGIS REST polyline layer", source_url=layer_url, license=rec["license"], reuse_class=reuse, evidence_class="G1/G2", geometry_precision="source GIS geometry; accuracy unverified", status="partial" if partial else "harvested", feature_count=w.line_parts, vertex_count=w.vertices, included_broad=True, included_open=include_open(reuse), notes=f"Reported count {reported_count}; harvested {harvested}; query cap {cap}.")
                if successful % 10 == 0:
                    log(f"ArcGIS harvested layers={successful}; features={total_features:,}; last={source_name}")
            except Exception as e:
                ARCGIS_ROWS.append({"item_id": item_id, "item_title": title, "service_url": service_url, "layer_id": layer_id, "layer_name": layer_name, "domain": item.get("_domain"), "reuse_class": reuse, "status": "layer_failed", "reported_count": "", "harvested_features": 0, "line_parts": 0, "vertices": 0, "partial": "", "output_layer": "", "error": str(e)[:500]})
                if len([x for x in ARCGIS_ROWS if x.get("status") == "layer_failed"]) <= 30:
                    err(f"ArcGIS layer {item_id}/{layer_id}", e)
    log(f"ArcGIS completed: {successful} layers, {total_features:,} features")


def write_csv(path: Path, rows: list[dict[str, Any]], preferred: list[str] | None = None) -> None:
    keys: list[str] = []
    if preferred:
        keys.extend(preferred)
    for row in rows:
        for k in row:
            if k not in keys:
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)


def build_top50() -> None:
    # Add direct ArcGIS catalogue discovery signals even when no geometry was harvested.
    arc_text = defaultdict(list)
    for row in ARCGIS_ROWS:
        text = f"{row.get('item_title','')} {row.get('layer_name','')} {row.get('owner','')}"
        canon = canonical_operator(text)
        if canon in {x[1] for x in TOP50}:
            arc_text[canon].append(row)
    rows: list[dict[str, Any]] = []
    rel = "audit/top_50_global_fiber_owner_coverage_index.kml"
    with KmlLayerWriter(LAYERS / rel, "Top 50 global fiber-owner public-map coverage index", "Research-priority operator set, not a precise route-mile ranking. Entries report which owners have public route geometry represented or source leads discovered.", "point") as w:
        for rank, canonical, region, aliases, map_url in TOP50:
            cov = OPERATOR_COVERAGE.get(canonical, {"components": 0, "vertices": 0, "sources": set(), "licenses": set(), "precision": set(), "urls": set(), "notes": set()})
            leads = arc_text.get(canonical, [])
            components = int(cov.get("components") or 0)
            sources = sorted(cov.get("sources") or set())
            status = "route geometry represented" if components else ("public ArcGIS source lead(s) found" if leads else "public map/source page catalogued; route geometry unresolved")
            row = {
                "priority_order": rank, "owner_family": canonical, "region": region,
                "aliases": " | ".join(aliases), "public_map_or_network_page": map_url,
                "coverage_status": status, "line_components_matched": components,
                "vertices_matched": int(cov.get("vertices") or 0),
                "geometry_sources": " | ".join(sources),
                "license_classes": " | ".join(sorted(cov.get("licenses") or set())),
                "geometry_precision": " | ".join(sorted(cov.get("precision") or set())),
                "source_urls": " | ".join(sorted((cov.get("urls") or set()) | {map_url})),
                "arcgis_catalogue_leads": len(leads),
                "notes": "Research-priority set; operator ownership and branding can change; absence from public geometry is not evidence of no network.",
            }
            rows.append(row)
            w.write_point(f"{rank:02d} — {canonical}", None, None, {
                "Region": region, "Coverage status": status, "Matched line components": components,
                "Geometry sources": row["geometry_sources"] or "None matched",
                "Public map/network page": map_url,
                "Caveat": row["notes"],
            })
    rec = {"path": rel, "name": "Top 50 global fiber-owner public-map coverage index", "domain": "audit", "style": "point", "reuse_class": "metadata", "source_id": "TOP50", "source_url": "", "license": "Research metadata", "feature_count": w.count, "line_parts": 0, "vertex_count": 0, "broad": True, "open": True, "precision": "not route geometry", "evidence_class": "audit"}
    LAYER_RECORDS.append(rec)
    write_csv(OUT / "top_50_global_fiber_owners_public_map_coverage.csv", rows)


def network_link(href: str, name: str, visibility: int = 0, description: str = "") -> str:
    return f"<NetworkLink><name>{esc(name)}</name><visibility>{visibility}</visibility><description><![CDATA[{cdata(description)}]]></description><Link><href>{esc(href)}</href><viewRefreshMode>never</viewRefreshMode></Link></NetworkLink>"


LIVE_LINKS = [
    ("fiber", "Zayo live publisher KMZ", "https://www.dropbox.com/scl/fi/ubw1bkngb77pb4q5jxfhj/Zayo-Network-Map-10.30.25.kmz?rlkey=8v72usduruuyxrv60sr2y0zy0&dl=1", 0, "Operator-published map; source terms apply."),
    ("fiber", "NOAA submarine-cable context", "https://hub.arcgis.com/api/download/v1/items/232eb6b84c644fa09e7bd2d4d623b2cb/kml", 0, "Government coastal submarine-cable context; generalized."),
    ("power", "WWF-SIGHT / OSM global power-line service", "https://wwf-sight-maps.org/arcgis/rest/services/Global/Global_Powerlines/MapServer/generateKml?docName=Global%20Powerlines&layers=0&layerOptions=nonComposite", 0, "External broad baseline; OSM attribution applies."),
    ("power", "United States HIFLD transmission lines", "https://hub.arcgis.com/api/download/v1/items/7759b0df07274f30a422e86dc11d4761/kml?layers=0", 0, "National U.S. transmission overlay."),
    ("power", "Bonneville Power Administration transmission lines", "https://hub.arcgis.com/api/download/v1/items/7015db75205b4b729d88574fea59293a/kml", 0, "Official federal utility layer."),
    ("power", "British Columbia transmission lines", "https://openmaps.gov.bc.ca/kml/geo/layers/WHSE_BASEMAPPING.GBA_TRANSMISSION_LINES_SP_loader.kml", 0, "Official provincial GIS layer."),
    ("power", "New Brunswick power utilities", "https://gnb.socrata.com/api/geospatial/y3vu-vr3p?method=export&format=KML", 0, "Official provincial open-data layer."),
    ("power", "Yukon power lines", "https://mapservices.gov.yk.ca/arcgis/rest/services/GeoYukon/GY_UtilitiesCommunications/MapServer/generateKml?docName=Yukon%20Power%20Lines&layers=9&layerOptions=nonComposite", 0, "Official government layer."),
    ("power", "Yukon primary distribution lines", "https://mapservices.gov.yk.ca/arcgis/rest/services/GeoYukon/GY_UtilitiesCommunications/MapServer/generateKml?docName=Yukon%20Primary%20Distribution%20Lines&layers=11&layerOptions=nonComposite", 0, "Official government primary-distribution layer."),
]


def create_doc_kml(selected_layers: list[dict[str, Any]], edition: str) -> bytes:
    folders = [
        ("Fiber — operator/private research", lambda r: r["domain"] == "fiber" and r.get("reuse_class") in {"unknown", "noncommercial", "restricted"}),
        ("Fiber — open, OSM and public GIS", lambda r: r["domain"] == "fiber" and r.get("reuse_class") not in {"unknown", "noncommercial", "restricted"}),
        ("Electric — global OSM/bulk and vector tiles", lambda r: r["domain"] == "power" and ("arcgis" not in r["path"])),
        ("Electric — ArcGIS government and public sources", lambda r: r["domain"] == "power" and ("arcgis" in r["path"])),
        ("Source and top-50 coverage audit", lambda r: r["domain"] == "audit"),
    ]
    description = (
        f"<b>Global Fiber & Electric Networks v2 — {edition}</b><br/>"
        "Actual embedded geometry harvested from bulk GeoJSON, public KML/KMZ, OpenInfraMap vector tiles, and public ArcGIS polyline services. "
        "All labels use English wrappers; source attributes are retained where useful. Routes may be generalized, schematic, stale, incomplete, or tile-clipped. "
        "Not for utility locating, excavation, design, switching, navigation, security targeting, capacity claims, or proof of service availability."
    )
    parts = [f'<?xml version="1.0" encoding="UTF-8"?><kml xmlns="{KML_NS}"><Document><name>Global Fiber &amp; Electric Networks v2 — {esc(edition)}</name><open>1</open><description><![CDATA[{description}]]></description>']
    for folder_name, pred in folders:
        subset = [r for r in selected_layers if pred(r)]
        if not subset:
            continue
        parts.append(f"<Folder><name>{esc(folder_name)}</name><open>0</open>")
        for r in sorted(subset, key=lambda x: x["name"].lower()):
            parts.append(network_link("layers/" + r["path"], r["name"], 0, f"Source: {r.get('source_url','')}; license/reuse: {r.get('license','')} / {r.get('reuse_class','')}; line parts: {r.get('line_parts',0):,}; vertices: {r.get('vertex_count',0):,}."))
        parts.append("</Folder>")
    parts.append("<Folder><name>Live publisher and government links</name><open>0</open>")
    for domain, name, url, vis, desc in LIVE_LINKS:
        parts.append(network_link(url, f"{domain.title()} — {name}", vis, desc))
    parts.append("</Folder></Document></kml>")
    return "".join(parts).encode("utf-8")


def build_kmz(path: Path, selected_layers: list[dict[str, Any]], edition: str) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=7, allowZip64=True) as z:
        z.writestr("doc.kml", create_doc_kml(selected_layers, edition))
        for r in selected_layers:
            src = LAYERS / r["path"]
            if src.exists() and src.stat().st_size:
                z.write(src, "layers/" + r["path"])


def validate_kmz(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(path), "size_bytes": path.stat().st_size, "zip_ok": False, "doc_kml_ok": False, "internal_links_ok": False, "member_count": 0, "kml_member_count": 0}
    with zipfile.ZipFile(path) as z:
        bad = z.testzip()
        result["zip_ok"] = bad is None
        names = set(z.namelist()); result["member_count"] = len(names); result["kml_member_count"] = sum(1 for n in names if n.lower().endswith(".kml"))
        doc = z.read("doc.kml"); etree.fromstring(doc); result["doc_kml_ok"] = True
        root = etree.fromstring(doc)
        hrefs = [clean_text(x.text) for x in root.findall(".//" + Q + "href")]
        internal = [h for h in hrefs if h.startswith("layers/")]
        result["internal_links_ok"] = all(h in names for h in internal)
        result["internal_link_count"] = len(internal)
    return result


def write_readme(summary: dict[str, Any]) -> None:
    text = f"""# Global Fiber & Electric Networks v2 — {DATE_TAG}

This is the rebuilt v2 release, not a renamed copy of v1.

## Editions

- `global_fiber_electric_v2_broad_research_{DATE_TAG}.kmz`: includes all public-reachable geometry harvested by this build, including public operator maps and noncommercial/unclear-reuse layers. Those layers are separated, off by default, and retain source warnings.
- `global_fiber_electric_v2_open_government_{DATE_TAG}.kmz`: includes only layers classified as government, public-domain, OSM/ODbL, or explicitly open/Creative-Commons.

## Actual v2 acquisition methods

1. Bulk global OSM high- and extra-high-voltage GeoJSON.
2. OpenInfraMap Mapbox Vector Tile decoding at zoom {MVT_ZOOM}; the `power_line` and `telecoms_communication_line` layers are converted into EPSG:4326 KML line pieces.
3. Public ArcGIS catalogue searches in English, Spanish, Portuguese, French, German, Japanese, and Chinese; polyline FeatureServer/MapServer layers are queried and embedded.
4. Fiber Network Alliance public member KMZ, sanitized and research-only.
5. Zayo's public operator KMZ, sanitized and research-only when reachable.
6. OpenFiberMap/AfTerFibre/RNP open GeoJSON.
7. A 2026-refreshed global submarine-cable route dataset with its CC BY-NC-SA boundary retained.
8. A top-50 operator-family coverage matrix showing represented geometry and unresolved gaps.

## Counts

- Broad embedded layer files: {summary.get('broad_layers', 0):,}
- Open/government embedded layer files: {summary.get('open_layers', 0):,}
- Broad line components: {summary.get('broad_line_parts', 0):,}
- Open/government line components: {summary.get('open_line_parts', 0):,}
- Broad coordinate vertices: {summary.get('broad_vertices', 0):,}
- ArcGIS layers harvested: {summary.get('arcgis_layers', 0):,}
- Vector tiles successfully decoded: {summary.get('mvt_tiles_success', 0):,}
- Top-50 operator rows: 50

## Evidence and safety boundary

A visible line is not an as-built or utility-locate record. Routes can be generalized, schematic, clipped at vector-tile boundaries, duplicated across sources, stale, or incomplete. Source absence is not proof that infrastructure does not exist. Do not use this release for excavation, locating, engineering design, switching, navigation, security targeting, capacity claims, or service-availability decisions.

Raster-only maps are catalogued where discovered but are not automatically traced as exact centerlines. Reliable raster conversion requires source-specific legend calibration, georeferencing, skeletonization, tile-edge stitching, and human QA.
"""
    (OUT / "README_global_fiber_electric_v2.md").write_text(text, encoding="utf-8")


def package_outputs() -> tuple[Path, Path]:
    complete = OUT / f"global_fiber_electric_v2_complete_package_{DATE_TAG}.zip"
    bundle = OUT / f"global_fiber_electric_v2_download_bundle_FIXED_{DATE_TAG}.zip"
    all_files = [p for p in OUT.iterdir() if p.is_file() and p not in {complete, bundle}]
    with zipfile.ZipFile(complete, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as z:
        for p in sorted(all_files):
            z.write(p, p.name)
        # Rebuild script is included from repository working tree.
        script = Path(__file__)
        z.write(script, "build_global_infrastructure_v2.py")
    recommended = [
        OUT / f"global_fiber_electric_v2_broad_research_{DATE_TAG}.kmz",
        OUT / f"global_fiber_electric_v2_open_government_{DATE_TAG}.kmz",
        OUT / "global_infrastructure_v2_source_manifest.csv",
        OUT / "arcgis_harvest_manifest.csv",
        OUT / "vector_tileset_conversion_manifest.csv",
        OUT / "top_50_global_fiber_owners_public_map_coverage.csv",
        OUT / "README_global_fiber_electric_v2.md",
        OUT / "global_fiber_electric_v2_validation.json",
        OUT / "SHA256SUMS_global_fiber_electric_v2.txt",
    ]
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as z:
        for p in recommended:
            if p.exists():
                z.write(p, p.name)
    return complete, bundle


def main() -> int:
    start = time.time()
    log(f"Starting Global Infrastructure v2 build in {ROOT}")
    core = acquire_core_sources()

    # Bulk/open core layers.
    if core.get("power_ehv"):
        process_geojson_layer("GPL-EHV", "Global OSM extra-high-voltage power lines", "https://github.com/lyralai/global-power-lines", core["power_ehv"], "power/global_osm_ehv_lines.kml", "power", "power_ehv", "ODbL 1.0 / © OpenStreetMap contributors", "odbl", "OSM mapped geometry; source simplification applied", "G2")
    if core.get("power_hv"):
        process_geojson_layer("GPL-HV", "Global OSM high-voltage power lines", "https://github.com/lyralai/global-power-lines", core["power_hv"], "power/global_osm_hv_lines.kml", "power", "power_hv", "ODbL 1.0 / © OpenStreetMap contributors", "odbl", "OSM mapped geometry; source simplification applied", "G2")
    if core.get("openfiber"):
        process_geojson_layer("OPENFIBER", "OpenFiberMap terrestrial fiber routes", "https://github.com/Jastman/OpenFiberMap", core["openfiber"], "fiber/openfibermap_terrestrial_routes.kml", "fiber", "fiber_open", "Per-feature source licenses; merged open data", "open", "Approximate/public cartography; per-feature notes retained", "G2/G3", points_output_rel="fiber/openfibermap_network_nodes.kml")
    process_subsea(core)

    # Operator/private sources.
    fna = acquire_fna()
    if fna:
        try:
            process_fna(fna)
        except Exception as e:
            err("FNA processing", e)
    process_zayo()

    # Actual vector-tile conversion and broad ArcGIS harvesting.
    try:
        process_openinframap_tiles()
    except Exception as e:
        err("OpenInfraMap fatal", e); traceback.print_exc()
    try:
        harvest_arcgis()
    except Exception as e:
        err("ArcGIS fatal", e); traceback.print_exc()

    build_top50()

    # Write manifests before packaging.
    write_csv(OUT / "global_infrastructure_v2_source_manifest.csv", SOURCE_ROWS)
    write_csv(OUT / "arcgis_harvest_manifest.csv", ARCGIS_ROWS)
    write_csv(OUT / "vector_tileset_conversion_manifest.csv", TILE_ROWS)
    write_csv(OUT / "embedded_layer_manifest.csv", LAYER_RECORDS)
    (OUT / "build_errors.json").write_text(json.dumps(ERRORS, indent=2, ensure_ascii=False), encoding="utf-8")

    broad_layers = [r for r in LAYER_RECORDS if r.get("broad") and (LAYERS / r["path"]).exists()]
    open_layers = [r for r in LAYER_RECORDS if r.get("open") and (LAYERS / r["path"]).exists()]
    broad_kmz = OUT / f"global_fiber_electric_v2_broad_research_{DATE_TAG}.kmz"
    open_kmz = OUT / f"global_fiber_electric_v2_open_government_{DATE_TAG}.kmz"
    build_kmz(broad_kmz, broad_layers, "Broad Research Edition")
    build_kmz(open_kmz, open_layers, "Open/Government Edition")

    tile = TILE_ROWS[0] if TILE_ROWS else {}
    summary = {
        "date": DATE_TAG, "elapsed_seconds": round(time.time() - start, 2),
        "broad_layers": len(broad_layers), "open_layers": len(open_layers),
        "broad_line_parts": sum(int(r.get("line_parts") or 0) for r in broad_layers),
        "open_line_parts": sum(int(r.get("line_parts") or 0) for r in open_layers),
        "broad_vertices": sum(int(r.get("vertex_count") or 0) for r in broad_layers),
        "open_vertices": sum(int(r.get("vertex_count") or 0) for r in open_layers),
        "arcgis_layers": sum(1 for r in ARCGIS_ROWS if r.get("status") == "harvested"),
        "arcgis_features": sum(int(r.get("harvested_features") or 0) for r in ARCGIS_ROWS),
        "mvt_tiles_success": int(tile.get("tiles_success") or 0),
        "mvt_telecom_line_parts": int(tile.get("telecom_line_parts") or 0),
        "mvt_power_line_parts": int(tile.get("power_line_parts") or 0),
        "sources_total": len(SOURCE_ROWS), "errors_total": len(ERRORS),
        "broad_kmz_bytes": broad_kmz.stat().st_size, "open_kmz_bytes": open_kmz.stat().st_size,
    }
    (OUT / "global_fiber_electric_v2_build_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    write_readme(summary)

    validation = {
        "broad": validate_kmz(broad_kmz), "open": validate_kmz(open_kmz),
        "release_gates": {
            "new_bulk_power_present": any(r.get("source_id") in {"GPL-EHV", "GPL-HV"} and int(r.get("line_parts") or 0) > 0 for r in LAYER_RECORDS),
            "openfiber_present": any(r.get("source_id") == "OPENFIBER" and int(r.get("line_parts") or 0) > 0 for r in LAYER_RECORDS),
            "subsea_present": any(r.get("source_id") == "SUBSEA2026" and int(r.get("line_parts") or 0) > 0 for r in LAYER_RECORDS),
            "vector_tiles_converted": bool(TILE_ROWS and (int(tile.get("telecom_line_parts") or 0) + int(tile.get("power_line_parts") or 0) > 0)),
            "arcgis_geometry_harvested": summary["arcgis_layers"] > 0,
            "top50_exactly_50": len(TOP50) == 50,
            "broad_has_multiple_new_layers": len(broad_layers) >= 6,
            "broad_not_empty": broad_kmz.stat().st_size > 1_000_000,
            "internal_links_valid": validate_kmz(broad_kmz)["internal_links_ok"] and validate_kmz(open_kmz)["internal_links_ok"],
        },
    }
    validation["status"] = "pass" if all(validation["release_gates"].values()) else "partial"
    (OUT / "global_fiber_electric_v2_validation.json").write_text(json.dumps(validation, indent=2), encoding="utf-8")

    # Checksums before bundle creation; checksum list intentionally excludes itself and bundles.
    checksum_targets = [p for p in OUT.iterdir() if p.is_file() and p.name not in {"SHA256SUMS_global_fiber_electric_v2.txt"} and not p.name.endswith("complete_package_2026-08-28.zip") and "download_bundle" not in p.name]
    with (OUT / "SHA256SUMS_global_fiber_electric_v2.txt").open("w", encoding="utf-8") as f:
        for p in sorted(checksum_targets):
            f.write(f"{sha256_file(p)}  {p.name}\n")

    complete, bundle = package_outputs()
    summary.update({"complete_package_bytes": complete.stat().st_size, "download_bundle_bytes": bundle.stat().st_size, "validation_status": validation["status"]})
    (OUT / "global_fiber_electric_v2_build_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    # Regenerate checksums after the final summary, then rebuild both packages so
    # the packaged checksum file matches the packaged metadata exactly.
    checksum_targets = [p for p in OUT.iterdir() if p.is_file() and p.name not in {"SHA256SUMS_global_fiber_electric_v2.txt"} and not p.name.endswith("complete_package_2026-08-28.zip") and "download_bundle" not in p.name]
    with (OUT / "SHA256SUMS_global_fiber_electric_v2.txt").open("w", encoding="utf-8") as f:
        for p in sorted(checksum_targets):
            f.write(f"{sha256_file(p)}  {p.name}\n")
    complete, bundle = package_outputs()

    # Final archive integrity checks.
    for p in (complete, bundle):
        with zipfile.ZipFile(p) as z:
            bad = z.testzip()
            if bad:
                raise RuntimeError(f"archive integrity failure {p.name}: {bad}")
    log("BUILD COMPLETE: " + json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
