#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from lxml import etree

KML_NS = "http://www.opengis.net/kml/2.2"
Q = f"{{{KML_NS}}}"
DATE_TAG = "2026-08-28"
CURRENT_ZAYO_URL = "https://www.dropbox.com/scl/fi/ta5npv2xqodfz6hd5b8nr/Zayo-Network-Map-2.17.26.kmz?rlkey=5bcowqv04w0hkfs5qiu6tygrl&dl=1"


def log(message: str) -> None:
    print(message, flush=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def find_one(root: Path, filename: str) -> Path:
    matches = list(root.rglob(filename))
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one {filename}, found {len(matches)}")
    return matches[0]


def import_builder(path: Path, work_root: Path):
    os.environ["V2_BUILD_ROOT"] = str(work_root)
    spec = importlib.util.spec_from_file_location("global_infrastructure_v2_builder", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load builder module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def append_network_links(doc_path: Path, records: list[dict[str, Any]]) -> None:
    parser = etree.XMLParser(huge_tree=True, recover=False, remove_blank_text=False)
    tree = etree.parse(str(doc_path), parser)
    root = tree.getroot()
    document = root.find(Q + "Document")
    if document is None:
        raise RuntimeError("KMZ doc.kml has no Document")
    folder = etree.SubElement(document, Q + "Folder")
    etree.SubElement(folder, Q + "name").text = "Fiber — injected public operator/member research maps"
    etree.SubElement(folder, Q + "open").text = "0"
    for record in records:
        link = etree.SubElement(folder, Q + "NetworkLink")
        etree.SubElement(link, Q + "name").text = str(record["name"])
        etree.SubElement(link, Q + "visibility").text = "0"
        desc = etree.SubElement(link, Q + "description")
        desc.text = etree.CDATA(
            f"Embedded post-build from {record.get('source_url','')}; reuse class {record.get('reuse_class','unknown')}; "
            f"line parts {int(record.get('line_parts') or 0):,}; vertices {int(record.get('vertex_count') or 0):,}. "
            "Research only; not surveyed or locate-grade geometry."
        )
        lnk = etree.SubElement(link, Q + "Link")
        etree.SubElement(lnk, Q + "href").text = "layers/" + str(record["path"])
        etree.SubElement(lnk, Q + "viewRefreshMode").text = "never"
    tree.write(str(doc_path), xml_declaration=True, encoding="UTF-8", pretty_print=False)


def inject_kmz(broad_kmz: Path, generated_layer_root: Path, records: list[dict[str, Any]]) -> None:
    with tempfile.TemporaryDirectory(prefix="v2-kmz-inject-") as tmp:
        tmpdir = Path(tmp)
        with zipfile.ZipFile(broad_kmz) as zin:
            bad = zin.testzip()
            if bad:
                raise RuntimeError(f"Input broad KMZ corrupt member: {bad}")
            zin.extractall(tmpdir)
        doc = tmpdir / "doc.kml"
        if not doc.exists():
            raise RuntimeError("Input broad KMZ lacks doc.kml")
        for record in records:
            src = generated_layer_root / str(record["path"])
            if not src.exists() or src.stat().st_size == 0:
                raise RuntimeError(f"Missing generated layer {src}")
            dst = tmpdir / "layers" / str(record["path"])
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        append_network_links(doc, records)
        replacement = broad_kmz.with_suffix(".kmz.new")
        with zipfile.ZipFile(replacement, "w", zipfile.ZIP_DEFLATED, compresslevel=7, allowZip64=True) as zout:
            for path in sorted(tmpdir.rglob("*")):
                if path.is_file():
                    zout.write(path, path.relative_to(tmpdir).as_posix())
        with zipfile.ZipFile(replacement) as z:
            bad = z.testzip()
            if bad:
                raise RuntimeError(f"Injected KMZ corrupt member: {bad}")
        replacement.replace(broad_kmz)


def validate_all_kml_members(path: Path) -> dict[str, Any]:
    result = {
        "path": str(path), "size_bytes": path.stat().st_size,
        "zip_ok": False, "doc_kml_ok": False, "internal_links_ok": False,
        "all_kml_members_parse": False, "member_count": 0,
        "kml_member_count": 0, "internal_link_count": 0,
    }
    with zipfile.ZipFile(path) as z:
        result["zip_ok"] = z.testzip() is None
        names = set(z.namelist())
        result["member_count"] = len(names)
        kml_names = [n for n in names if n.lower().endswith(".kml")]
        result["kml_member_count"] = len(kml_names)
        parsed = 0
        for name in kml_names:
            etree.fromstring(z.read(name), parser=etree.XMLParser(huge_tree=True, recover=False))
            parsed += 1
        result["all_kml_members_parse"] = parsed == len(kml_names)
        doc = etree.fromstring(z.read("doc.kml"), parser=etree.XMLParser(huge_tree=True, recover=False))
        result["doc_kml_ok"] = True
        hrefs = [(x.text or "").strip() for x in doc.findall(".//" + Q + "href")]
        internal = [h for h in hrefs if h.startswith("layers/")]
        result["internal_link_count"] = len(internal)
        result["internal_links_ok"] = all(h in names for h in internal)
    return result


def rebuild_packages(out: Path, builder_path: Path) -> tuple[Path, Path]:
    complete = out / f"global_fiber_electric_v2_complete_package_{DATE_TAG}.zip"
    bundle = out / f"global_fiber_electric_v2_download_bundle_FIXED_{DATE_TAG}.zip"
    for p in (complete, bundle):
        p.unlink(missing_ok=True)
    files = [p for p in out.iterdir() if p.is_file() and p not in {complete, bundle}]
    with zipfile.ZipFile(complete, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as z:
        for p in sorted(files):
            z.write(p, p.name)
        z.write(builder_path, "build_global_infrastructure_v2.py")
        z.write(Path(__file__), "postprocess_global_infrastructure_v2.py")
    preferred_names = [
        f"global_fiber_electric_v2_broad_research_{DATE_TAG}.kmz",
        f"global_fiber_electric_v2_open_government_{DATE_TAG}.kmz",
        "global_infrastructure_v2_source_manifest.csv",
        "arcgis_harvest_manifest.csv",
        "vector_tileset_conversion_manifest.csv",
        "top_50_global_fiber_owners_public_map_coverage.csv",
        "embedded_layer_manifest.csv",
        "global_fiber_electric_v2_build_summary.json",
        "global_fiber_electric_v2_validation.json",
        "README_global_fiber_electric_v2.md",
        "SHA256SUMS_global_fiber_electric_v2.txt",
        "postprocess_resolutions.json",
    ]
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as z:
        for name in preferred_names:
            p = out / name
            if p.exists():
                z.write(p, p.name)
    for p in (complete, bundle):
        with zipfile.ZipFile(p) as z:
            bad = z.testzip()
            if bad:
                raise RuntimeError(f"Package corrupt member {bad}: {p}")
    return complete, bundle


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-dir", required=True)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--builder", required=True)
    ap.add_argument("--fna-kmz", required=True)
    ap.add_argument("--zayo-kmz", required=True)
    args = ap.parse_args()

    input_dir = Path(args.input_dir).resolve()
    out = Path(args.output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    builder_path = Path(args.builder).resolve()
    work_root = out.parent / "operator_injection_work"
    if work_root.exists():
        shutil.rmtree(work_root)

    for src in input_dir.rglob("*"):
        if src.is_file():
            shutil.copy2(src, out / src.name)

    broad = find_one(out, f"global_fiber_electric_v2_broad_research_{DATE_TAG}.kmz")
    open_map = find_one(out, f"global_fiber_electric_v2_open_government_{DATE_TAG}.kmz")

    builder = import_builder(builder_path, work_root)
    fna_path = Path(args.fna_kmz).resolve()
    zayo_path = Path(args.zayo_kmz).resolve()
    if not zipfile.is_zipfile(fna_path):
        raise RuntimeError("FNA download is not a valid ZIP/KMZ")
    if not zipfile.is_zipfile(zayo_path):
        raise RuntimeError("Zayo download is not a valid ZIP/KMZ")

    builder.process_fna(fna_path)
    builder.CACHE.mkdir(parents=True, exist_ok=True)
    zayo_dest = builder.CACHE / "Zayo-Network-Map-10.30.25.kmz"
    shutil.copy2(zayo_path, zayo_dest)
    builder.process_zayo()

    injected_records: list[dict[str, Any]] = []
    for record in builder.LAYER_RECORDS:
        if record.get("source_id") in {"FNA2020", "ZAYO2025"}:
            rec = dict(record)
            if rec.get("source_id") == "ZAYO2025":
                rec["name"] = "Zayo published network map — February 2026"
                rec["source_url"] = CURRENT_ZAYO_URL
            injected_records.append(rec)
    if {r.get("source_id") for r in injected_records} != {"FNA2020", "ZAYO2025"}:
        raise RuntimeError(f"Did not generate both operator layers: {injected_records}")

    inject_kmz(broad, builder.LAYERS, injected_records)

    layer_manifest = out / "embedded_layer_manifest.csv"
    rows = read_csv(layer_manifest)
    rows.extend(injected_records)
    write_csv(layer_manifest, rows)

    source_manifest = out / "global_infrastructure_v2_source_manifest.csv"
    source_rows = read_csv(source_manifest)
    source_rows = [r for r in source_rows if r.get("source_id") not in {"FNA2020", "ZAYO2025"}]
    for row in builder.SOURCE_ROWS:
        if row.get("source_id") in {"FNA2020", "ZAYO2025"}:
            rec = dict(row)
            if rec.get("source_id") == "ZAYO2025":
                rec["name"] = "Zayo published network map — February 2026"
                rec["source_url"] = CURRENT_ZAYO_URL
                rec["notes"] = "Sanitized conversion of the current public operator KMZ linked by NCTC; source terms apply."
            source_rows.append(rec)
    write_csv(source_manifest, source_rows)

    top50_path = out / "top_50_global_fiber_owners_public_map_coverage.csv"
    top_rows = read_csv(top50_path)
    for row in top_rows:
        owner = row.get("owner_family", "")
        cov = builder.OPERATOR_COVERAGE.get(owner)
        if not cov:
            continue
        prior_parts = int(float(row.get("line_components_matched") or 0))
        prior_vertices = int(float(row.get("vertices_matched") or 0))
        row["line_components_matched"] = str(prior_parts + int(cov.get("components") or 0))
        row["vertices_matched"] = str(prior_vertices + int(cov.get("vertices") or 0))
        row["coverage_status"] = "route geometry represented"
        for field, values in (
            ("geometry_sources", cov.get("sources") or set()),
            ("license_classes", cov.get("licenses") or set()),
            ("geometry_precision", cov.get("precision") or set()),
            ("source_urls", cov.get("urls") or set()),
        ):
            existing = {x.strip() for x in (row.get(field) or "").split("|") if x.strip()}
            existing.update(str(x).strip() for x in values if str(x).strip())
            row[field] = " | ".join(sorted(existing))
    write_csv(top50_path, top_rows)

    resolutions = {
        "date": DATE_TAG,
        "source_run": "full zoom-6 / 124-layer ArcGIS build",
        "FNA2020": {
            "status": "resolved and injected",
            "reason_original_build_skipped": "gdown API compatibility: deprecated fuzzy keyword",
            "line_parts": next(int(r.get("line_parts") or 0) for r in injected_records if r.get("source_id") == "FNA2020"),
        },
        "ZAYO2025": {
            "status": "resolved and injected from current February 2026 public KMZ",
            "reason_original_build_skipped": "stale October 2025 Dropbox endpoint",
            "source_url": CURRENT_ZAYO_URL,
            "line_parts": next(int(r.get("line_parts") or 0) for r in injected_records if r.get("source_id") == "ZAYO2025"),
        },
    }
    (out / "postprocess_resolutions.json").write_text(json.dumps(resolutions, indent=2), encoding="utf-8")

    summary_path = out / "global_fiber_electric_v2_build_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    added_parts = sum(int(r.get("line_parts") or 0) for r in injected_records)
    added_vertices = sum(int(r.get("vertex_count") or 0) for r in injected_records)
    summary["broad_layers"] = int(summary.get("broad_layers") or 0) + len(injected_records)
    summary["broad_line_parts"] = int(summary.get("broad_line_parts") or 0) + added_parts
    summary["broad_vertices"] = int(summary.get("broad_vertices") or 0) + added_vertices
    summary["broad_kmz_bytes"] = broad.stat().st_size
    summary["fna_operator_layer_injected"] = True
    summary["zayo_current_operator_layer_injected"] = True
    summary["postprocess_added_line_parts"] = added_parts
    summary["postprocess_added_vertices"] = added_vertices
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    readme_path = out / "README_global_fiber_electric_v2.md"
    with readme_path.open("a", encoding="utf-8") as f:
        f.write("\n## Final operator-layer post-processing\n\n")
        f.write("The final broad-research KMZ adds the sanitized Fiber Network Alliance 2020 member-route snapshot and the current Zayo February 2026 public operator KMZ. These private/operator layers remain research-only and off by default; they are excluded from the open/government edition.\n")

    broad_validation = validate_all_kml_members(broad)
    open_validation = validate_all_kml_members(open_map)
    validation_path = out / "global_fiber_electric_v2_validation.json"
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    validation["broad"] = broad_validation
    validation["open"] = open_validation
    gates = validation.setdefault("release_gates", {})
    gates["fna_member_routes_injected"] = any(r.get("source_id") == "FNA2020" and int(r.get("line_parts") or 0) > 0 for r in injected_records)
    gates["current_zayo_operator_map_injected"] = any(r.get("source_id") == "ZAYO2025" and int(r.get("line_parts") or 0) > 0 for r in injected_records)
    gates["all_broad_kml_members_parse"] = broad_validation["all_kml_members_parse"]
    gates["all_open_kml_members_parse"] = open_validation["all_kml_members_parse"]
    gates["broad_internal_links_valid_after_injection"] = broad_validation["internal_links_ok"]
    validation["status"] = "pass" if all(bool(v) for v in gates.values()) else "partial"
    validation_path.write_text(json.dumps(validation, indent=2), encoding="utf-8")
    if validation["status"] != "pass":
        raise RuntimeError(f"Final release gates did not all pass: {gates}")

    for name in (f"global_fiber_electric_v2_complete_package_{DATE_TAG}.zip", f"global_fiber_electric_v2_download_bundle_FIXED_{DATE_TAG}.zip"):
        (out / name).unlink(missing_ok=True)
    checksum_path = out / "SHA256SUMS_global_fiber_electric_v2.txt"
    checksum_path.unlink(missing_ok=True)
    checksum_targets = [p for p in out.iterdir() if p.is_file() and p.name != checksum_path.name and not p.name.endswith("complete_package_2026-08-28.zip") and "download_bundle" not in p.name]
    with checksum_path.open("w", encoding="utf-8") as f:
        for p in sorted(checksum_targets):
            f.write(f"{sha256_file(p)}  {p.name}\n")

    complete, bundle = rebuild_packages(out, builder_path)
    log(json.dumps({
        "broad_kmz": str(broad), "broad_bytes": broad.stat().st_size,
        "open_kmz": str(open_map), "open_bytes": open_map.stat().st_size,
        "complete_package": str(complete), "complete_bytes": complete.stat().st_size,
        "bundle": str(bundle), "bundle_bytes": bundle.stat().st_size,
        "added_layers": [r["name"] for r in injected_records],
        "validation": validation["status"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
