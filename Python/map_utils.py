import logging
import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Tuple

import geopandas as gpd
import numpy as np
import pycountry
import requests
import rasterio
from affine import Affine
from rasterio.enums import Resampling
from rasterio.merge import merge
from rasterio.mask import mask
from rasterio.io import MemoryFile
from rasterio.vrt import WarpedVRT
from shapely.geometry import box, mapping

# Esri/Impact Observatory Sentinel-2 10m land cover, queried dynamically per-AOI
# via Microsoft Planetary Computer's public STAC API. The search itself is
# public/no-auth, but the returned asset hrefs point at a gated storage account
# and need a short-lived SAS token from PC's (also public/no-auth) signing
# endpoint before they're downloadable.
LAND_COVER_STAC_SEARCH_URL = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
LAND_COVER_COLLECTION = "io-lulc-annual-v02"
PC_SAS_SIGN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/sign"

NATURAL_EARTH_COUNTRIES_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/"
    "geojson/ne_10m_admin_0_countries.geojson"
)

# Copernicus GLO-30 DEM (30m global elevation), public/no-auth, tiled by 1x1 degree cell.
COPERNICUS_DEM_BASE_URL = "https://copernicus-dem-30m.s3.amazonaws.com"

# Upper bound on the working array's pixel count, regardless of AOI size. Without
# this, a large country (or a low --aggregate value on one) can require several
# GB of memory and easily exceed what a CI runner has available. When an AOI
# would exceed this, the effective resolution is coarsened automatically so the
# tool works for any area size rather than only ones a user happened to pick a
# large enough --aggregate for.
MAX_OUTPUT_PIXELS = 16_000_000

# Esri/Impact Observatory Sentinel-2 10m LULC classification scheme. Classes 3
# and 6 are not used by this dataset. Colors match the official legend:
# https://collections.sentinel-hub.com/impact-observatory-lulc-map/readme.html
LAND_COVER_COLORS = {
    1: (65, 155, 223),    # Water
    2: (57, 125, 73),     # Trees
    4: (122, 135, 198),   # Flooded vegetation
    5: (228, 150, 53),    # Crops
    7: (196, 40, 27),     # Built area
    8: (165, 155, 143),   # Bare ground
    9: (168, 235, 255),   # Snow / ice
    10: (97, 97, 97),     # Clouds
    11: (227, 226, 195),  # Rangeland
}
LAND_COVER_LABELS = {
    1: "Water",
    2: "Trees",
    4: "Flooded vegetation",
    5: "Crops",
    7: "Built area",
    8: "Bare ground",
    9: "Snow / ice",
    10: "Clouds",
    11: "Rangeland",
}
DEFAULT_COLOR = (128, 128, 128)


def resolve_country_code(country: str) -> str:
    candidate = country.strip()

    if len(candidate) == 2:
        match = pycountry.countries.get(alpha_2=candidate.upper())
    elif len(candidate) == 3:
        match = pycountry.countries.get(alpha_3=candidate.upper())
    else:
        match = None

    if match is None:
        try:
            match = pycountry.countries.search_fuzzy(candidate)[0]
        except LookupError:
            match = None

    if match is None:
        raise ValueError(f"Could not resolve country code for '{country}'")

    return match.alpha_3


def _download_natural_earth_countries(dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Derived from the URL (rather than hardcoded) so a stale cached file from
    # a previous resolution tier never gets silently reused after the URL
    # constant changes.
    output_path = dest_dir / NATURAL_EARTH_COUNTRIES_URL.rsplit("/", 1)[-1]

    if not output_path.exists():
        logging.info("Downloading Natural Earth countries dataset")
        response = requests.get(NATURAL_EARTH_COUNTRIES_URL, timeout=120)
        response.raise_for_status()
        output_path.write_bytes(response.content)

    return output_path


def load_country_geometry(country: str, data_dir: Path = Path("data")) -> gpd.GeoDataFrame:
    iso3 = resolve_country_code(country)
    geometry_path = _download_natural_earth_countries(Path(data_dir))
    world = gpd.read_file(geometry_path)
    # ADM0_A3 is used instead of ISO_A3 because Natural Earth leaves ISO_A3
    # as "-99" for several countries (e.g. France, Norway) over disputed territories.
    selected = world[world["ADM0_A3"] == iso3]

    if selected.empty:
        raise ValueError(f"Country not found in Natural Earth dataset: {country}")

    return selected[['geometry']].copy()


def geometry_from_geojson(geojson_text: str) -> gpd.GeoDataFrame:
    """Build an AOI GeoDataFrame (EPSG:4326) from a GeoJSON Geometry, Feature, or FeatureCollection string."""
    import json
    from shapely.geometry import shape

    data = json.loads(geojson_text)
    obj_type = data.get("type")

    if obj_type == "FeatureCollection":
        geometries = [shape(feature["geometry"]) for feature in data["features"]]
    elif obj_type == "Feature":
        geometries = [shape(data["geometry"])]
    else:
        geometries = [shape(data)]

    if not geometries:
        raise ValueError("GeoJSON input contains no geometry")

    return gpd.GeoDataFrame(geometry=geometries, crs="EPSG:4326")


def _disjoint_parts(country_gdf: gpd.GeoDataFrame) -> list:
    """AOI as a list of disjoint polygon parts, in WGS84.

    A country whose parts straddle the antimeridian (e.g. Russia) has a
    combined total_bounds() spanning roughly the entire globe's longitude
    ([-180, 180]), since it naively takes the min/max across parts on
    opposite sides of the 180th meridian -- even though no individual part
    is actually that wide (confirmed against Natural Earth's Russia geometry:
    14 disjoint parts, largest single-part width ~153 degrees, well under the
    360 a broken combined bbox would suggest). Working per-part instead of on
    the combined geometry sidesteps this entirely.
    """
    wgs84 = country_gdf.to_crs("EPSG:4326")
    geometry = wgs84.geometry.union_all()
    return list(geometry.geoms) if hasattr(geometry, "geoms") else [geometry]


def find_land_cover_tile_urls(country_gdf: gpd.GeoDataFrame, year: int = None) -> List[str]:
    """Find the Esri/Impact Observatory 10m land cover tiles covering an AOI.

    Queries Microsoft Planetary Computer's public STAC API rather than relying
    on a fixed tile list, so this works for any area on Earth, not just a
    hardcoded region.
    """
    latest_by_tile = {}

    for part in _disjoint_parts(country_gdf):
        minx, miny, maxx, maxy = part.bounds
        query = {"collections": [LAND_COVER_COLLECTION], "bbox": [minx, miny, maxx, maxy], "limit": 100}
        if year is not None:
            query["datetime"] = f"{year}-01-01T00:00:00Z/{year}-12-31T23:59:59Z"

        response = requests.post(LAND_COVER_STAC_SEARCH_URL, json=query, timeout=60)
        response.raise_for_status()
        features = response.json().get("features", [])

        # Item ids look like "<tile-id>-<year>" (e.g. "33T-2023"). Several years
        # can cover the same tile; keep only the most recent per tile.
        for feature in features:
            tile_id, _, item_year = feature["id"].rpartition("-")
            href = feature["assets"]["data"]["href"]
            if tile_id not in latest_by_tile or item_year > latest_by_tile[tile_id][0]:
                latest_by_tile[tile_id] = (item_year, href)

    if not latest_by_tile:
        raise ValueError("No land cover tiles found for this area")

    return [_sign_pc_href(href) for _, href in latest_by_tile.values()]


def _sign_pc_href(href: str) -> str:
    response = requests.get(PC_SAS_SIGN_URL, params={"href": href}, timeout=30)
    response.raise_for_status()
    return response.json()["href"]


def _dem_tile_id(lat: int, lon: int) -> str:
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM"


def find_elevation_tile_urls(country_gdf: gpd.GeoDataFrame) -> List[str]:
    """Find Copernicus DEM tile URLs actually intersecting the AOI.

    Filters against the real geometry rather than just its bounding box (per
    disjoint part -- see _disjoint_parts), since for an elongated/irregular
    or antimeridian-crossing AOI the combined box can suggest cells the AOI
    doesn't actually touch, or in Russia's case span the entire globe.
    """
    tile_cells = set()

    for part in _disjoint_parts(country_gdf):
        minx, miny, maxx, maxy = part.bounds
        for lat in range(math.floor(miny), math.floor(maxy) + 1):
            for lon in range(math.floor(minx), math.floor(maxx) + 1):
                if part.intersects(box(lon, lat, lon + 1, lat + 1)):
                    tile_cells.add((lat, lon))

    urls = []
    for lat, lon in tile_cells:
        tile_id = _dem_tile_id(lat, lon)
        urls.append(f"{COPERNICUS_DEM_BASE_URL}/{tile_id}/{tile_id}.tif")

    return urls


def load_elevation(
    tile_urls: List[str],
    country_gdf: gpd.GeoDataFrame,
    ref_shape: Tuple[int, int],
    ref_transform: Affine,
    ref_crs,
) -> np.ndarray:
    """Build an elevation array aligned to the land cover grid.

    Reads each DEM tile directly from its remote URL through a WarpedVRT and
    merges at the target resolution in one pass, rather than downloading full
    -resolution tiles to disk first -- for a large AOI (a big country can
    intersect thousands of 1x1 degree DEM cells) that download step alone can
    exhaust a CI runner's disk space long before anything is even resampled.
    """
    if not tile_urls:
        return np.zeros(ref_shape, dtype=np.float32)

    raster_files = []
    warped_sources = []

    # GDAL_HTTP_MAX_RETRY/DELAY is a safety net for genuine transient network
    # errors across hundreds of concurrent remote tile fetches, covering both
    # the initial open below and any reads merge() does afterward.
    # AWS_NO_SIGN_REQUEST tells GDAL's S3 driver this bucket is public and
    # needs no credentials -- without it, rasterio probes for boto3 on every
    # single tile open and logs a "falling back to a DummySession" warning
    # each time (boto3 isn't a project dependency; it's not needed here).
    with rasterio.Env(GDAL_HTTP_MAX_RETRY=5, GDAL_HTTP_RETRY_DELAY=1, AWS_NO_SIGN_REQUEST="YES"):
        try:
            # Opening is network I/O (a remote metadata fetch per tile), not CPU
            # work -- a large AOI can need thousands of DEM tiles, and opening
            # those one at a time would make total wall-clock time scale linearly
            # with tile count for no reason. Threads parallelize the I/O wait.
            def _open(url):
                try:
                    return rasterio.open(url)
                except rasterio.errors.RasterioIOError:
                    logging.info(f"No elevation data available at {url}, skipping")
                    return None

            with ThreadPoolExecutor(max_workers=16) as executor:
                for result in executor.map(_open, tile_urls):
                    if result is not None:
                        raster_files.append(result)

            if not raster_files:
                return np.zeros(ref_shape, dtype=np.float32)

            warped_sources = [
                WarpedVRT(src, crs=ref_crs, resampling=Resampling.bilinear)
                for src in raster_files
            ]

            bounds = tuple(country_gdf.to_crs(ref_crs).total_bounds)
            target_res = (abs(ref_transform.a), abs(ref_transform.e))

            mosaic, _ = merge(warped_sources, bounds=bounds, res=target_res, method='first')
            merged = mosaic[0].astype(np.float32)

            # merge()'s bounds-derived shape can be off by a pixel from ref_shape
            # due to floating point rounding; align to ref_shape exactly so this
            # lines up pixel-for-pixel with the land cover array.
            elevation = np.zeros(ref_shape, dtype=np.float32)
            h = min(ref_shape[0], merged.shape[0])
            w = min(ref_shape[1], merged.shape[1])
            elevation[:h, :w] = merged[:h, :w]

            return elevation
        finally:
            for vrt in warped_sources:
                vrt.close()
            for src in raster_files:
                src.close()


def load_masked_land_cover(tile_urls: List[str], country_gdf: gpd.GeoDataFrame, aggregate_factor: int = 5):
    # Opened directly from their (signed) remote URLs -- no local download.
    # Land cover tiles are tens of thousands of pixels per side; a large
    # country can need dozens of them, which downloaded in full would risk
    # exhausting a CI runner's disk long before anything gets resampled.
    raster_files = [rasterio.open(url) for url in tile_urls]
    warped_sources = []

    try:
        # Source tiles can fall in different UTM zones (rasterio.merge requires a
        # shared CRS and does not reproject). Reproject onto a common CRS via
        # WarpedVRT and restrict the merge to the country's bounding box so GDAL
        # only reads the small windowed region needed, instead of materializing
        # entire multi-gigabyte tiles in memory.
        #
        # That windowed region is still read at native (10m) resolution unless we
        # tell it otherwise -- for a large country the bounding box alone can be
        # several billion pixels (e.g. Nepal: ~3.8B), enough to exhaust a CI
        # runner's memory or take excessively long. Passing `res=` resamples
        # directly to the aggregated resolution during the read itself, so the
        # array materialized in memory is already the small, final size.
        target_crs = raster_files[0].crs
        source_res = raster_files[0].res[0]
        target_res = source_res * max(aggregate_factor, 1)

        if country_gdf.crs is None:
            country_gdf = country_gdf.set_crs("EPSG:4326")

        country_in_target_crs = country_gdf.to_crs(target_crs)
        bounds = tuple(country_in_target_crs.total_bounds)

        est_pixels = ((bounds[2] - bounds[0]) / target_res) * ((bounds[3] - bounds[1]) / target_res)
        if est_pixels > MAX_OUTPUT_PIXELS:
            scale = (est_pixels / MAX_OUTPUT_PIXELS) ** 0.5
            coarsened_res = target_res * scale
            logging.info(
                f"Area is large (~{est_pixels / 1e6:.0f}M px at {target_res:.0f}m/px); "
                f"coarsening to {coarsened_res:.0f}m/px to keep memory use bounded."
            )
            target_res = coarsened_res

        warped_sources = [
            WarpedVRT(src, crs=target_crs, resampling=Resampling.nearest)
            for src in raster_files
        ]

        mosaic, mosaic_transform = merge(
            warped_sources, bounds=bounds, res=(target_res, target_res), method='first'
        )
        mosaic_meta = raster_files[0].meta.copy()
        mosaic_meta.update({
            "driver": "GTiff",
            "height": mosaic.shape[1],
            "width": mosaic.shape[2],
            "transform": mosaic_transform,
            "count": 1,
        })

        with MemoryFile() as memfile:
            with memfile.open(**mosaic_meta) as dataset:
                dataset.write(mosaic)
                shapes = [mapping(geom) for geom in country_in_target_crs.geometry]
                masked, crop_transform = mask(dataset, shapes, crop=True, nodata=0)

        land_cover = masked[0]

        return land_cover, crop_transform, target_crs
    finally:
        for vrt in warped_sources:
            vrt.close()
        for src in raster_files:
            src.close()


def land_cover_to_rgb(land_cover_array: np.ndarray) -> np.ndarray:
    height, width = land_cover_array.shape
    rgb = np.zeros((height, width, 3), dtype=np.uint8)

    for class_code, color in LAND_COVER_COLORS.items():
        mask_arr = land_cover_array == class_code
        rgb[mask_arr] = color

    rgb[land_cover_array == 0] = (255, 255, 255)
    unknown_mask = np.logical_and(land_cover_array != 0, np.logical_not(np.isin(land_cover_array, list(LAND_COVER_COLORS.keys()))))
    rgb[unknown_mask] = DEFAULT_COLOR

    return rgb


def _draw_legend(image, class_codes):
    from PIL import Image, ImageDraw, ImageFont

    codes = sorted({int(c) for c in class_codes} & LAND_COVER_COLORS.keys())
    if not codes:
        return image

    base = image.convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Scale the legend relative to image size (baseline: a 2000px-wide image)
    # so it stays legible on both small previews and full-resolution exports.
    scale = max(0.6, min(3.0, base.width / 2000))
    font_size = round(18 * scale)
    swatch = round(18 * scale)
    padding = round(14 * scale)
    margin = round(16 * scale)
    text_gap = round(10 * scale)
    row_gap = round(10 * scale)

    try:
        font = ImageFont.load_default(size=font_size)
    except TypeError:
        font = ImageFont.load_default()

    row_h = swatch + row_gap
    text_widths = [draw.textlength(LAND_COVER_LABELS[c], font=font) for c in codes]
    panel_w = int(padding * 2 + swatch + text_gap + max(text_widths))
    panel_h = int(padding * 2 + row_h * len(codes))

    x0, y0 = margin, base.height - panel_h - margin
    draw.rounded_rectangle(
        [x0, y0, x0 + panel_w, y0 + panel_h], radius=round(10 * scale), fill=(255, 255, 255, 225)
    )

    for i, code in enumerate(codes):
        ry = y0 + padding + i * row_h
        color = LAND_COVER_COLORS[code]
        draw.rectangle([x0 + padding, ry, x0 + padding + swatch, ry + swatch], fill=color + (255,))
        draw.text(
            (x0 + padding + swatch + text_gap, ry + round(2 * scale)),
            LAND_COVER_LABELS[code],
            fill=(17, 24, 39, 255),
            font=font,
        )

    return Image.alpha_composite(base, overlay)


def save_png(rgb_array: np.ndarray, output_path: Path, land_cover_array: np.ndarray = None) -> None:
    from PIL import Image

    image = Image.fromarray(rgb_array, mode="RGB").convert("RGBA")

    if land_cover_array is not None:
        image = _draw_legend(image, np.unique(land_cover_array).tolist())

    if Path(output_path).suffix.lower() in (".jpg", ".jpeg"):
        image = image.convert("RGB")

    image.save(output_path)


def save_3d_png(
    rgb_array: np.ndarray,
    elevation_array: np.ndarray,
    output_path: Path,
    pixel_size: float = 30.0,
    azimuth: float = 315.0,
    altitude: float = 45.0,
    land_cover_array: np.ndarray = None,
) -> None:
    """Render land cover colored by a hillshaded relief derived from elevation.

    A literal tilted 3D mesh (e.g. matplotlib's plot_surface) can't render a
    dense, steep terrain grid without severe z-fighting/streaking artifacts.
    A hillshade-and-color blend -- the same technique rayshader uses -- gives
    a convincing relief look with none of those artifacts.
    """
    from PIL import Image

    # float32 throughout (not float64): halves the memory footprint of these
    # full-size intermediate arrays, which matters for large AOIs.
    elevation = elevation_array.astype(np.float32)
    gy, gx = np.gradient(elevation, pixel_size)
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)

    az_rad = np.float32(np.radians(360.0 - azimuth + 90.0))
    alt_rad = np.float32(np.radians(altitude))

    shaded = (
        np.sin(alt_rad) * np.cos(slope)
        + np.cos(alt_rad) * np.sin(slope) * np.cos(az_rad - aspect)
    )
    shaded = np.clip(shaded, 0.0, 1.0, out=shaded)
    shaded = 0.35 + 0.65 * shaded  # keep shadowed areas from going fully black

    shaded_rgb = rgb_array.astype(np.float32)
    shaded_rgb *= shaded[..., None]
    np.clip(shaded_rgb, 0, 255, out=shaded_rgb)
    shaded_rgb = shaded_rgb.astype(np.uint8)

    if land_cover_array is not None:
        # Outside the AOI, land_cover is 0 (nodata). shaded_rgb still holds a
        # hillshade computed from the real elevation of that surrounding
        # terrain (white * that shading factor), so it must be overwritten
        # here -- not just masked via alpha -- or anything that drops/ignores
        # alpha (JPEG output, some viewers) reveals a hillshaded image of the
        # neighboring, non-AOI landscape instead of a clean background.
        shaded_rgb[land_cover_array == 0] = (255, 255, 255)

    image = Image.fromarray(shaded_rgb, mode="RGB")

    if land_cover_array is not None:
        image = _draw_legend(image.convert("RGBA"), np.unique(land_cover_array).tolist()).convert("RGB")

    image.save(output_path)


def create_export_info(country: str, output_path: Path) -> None:
    logging.info(f"Generated map for {country}")
    logging.info(f"Saved land cover export to {output_path}")
