import argparse
import logging
import os
from pathlib import Path

from map_utils import (
    create_export_info,
    find_elevation_tile_urls,
    find_land_cover_tile_urls,
    geometry_from_geojson,
    land_cover_to_rgb,
    load_country_geometry,
    load_elevation,
    load_masked_land_cover,
    save_3d_png,
    save_png,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a land cover export image for a country from ESRI land cover tiles."
    )

    parser.add_argument(
        "--country",
        default=None,
        help="Country ISO code or name to clip the land cover map. Examples: BA, BIH, Bosnia and Herzegovina. "
             "Ignored if --aoi-geojson is given.",
    )
    parser.add_argument(
        "--aoi-geojson",
        default=None,
        help="Custom area of interest as a GeoJSON Geometry/Feature/FeatureCollection string. Overrides --country.",
    )
    parser.add_argument(
        "--output",
        default="out/land-cover-map.png",
        help="Output image path.",
    )
    parser.add_argument(
        "--mode",
        choices=["2d", "3d"],
        default="3d",
        help="Export mode for the generated image.",
    )
    parser.add_argument(
        "--aggregate",
        type=int,
        default=5,
        help="Downsample factor for the output. Use 1 for full resolution, higher values for smaller exports.",
    )
    parser.add_argument(
        "--data-dir",
        default="data",
        help="Directory to cache the country geometry lookup (land cover/elevation are streamed, not cached).",
    )

    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    land_cover_dir = Path(args.data_dir)

    if args.aoi_geojson:
        logging.info("Using custom AOI from GeoJSON input")
        country_gdf = geometry_from_geojson(args.aoi_geojson)
        region_label = args.country or "custom-area"
    else:
        region_label = args.country or "BA"
        logging.info(f"Loading country geometry for {region_label}")
        country_gdf = load_country_geometry(region_label, land_cover_dir)

    logging.info("Finding land cover tiles for this area")
    tile_urls = find_land_cover_tile_urls(country_gdf)

    logging.info("Loading and clipping land cover raster")
    land_cover, land_cover_transform, land_cover_crs = load_masked_land_cover(
        tile_urls, country_gdf, aggregate_factor=args.aggregate
    )

    logging.info("Converting land cover classification to RGB image")
    rgb = land_cover_to_rgb(land_cover)

    if args.mode == "3d":
        output_path = output_path.with_name(output_path.stem + "-3d" + output_path.suffix)

        logging.info("Finding elevation tiles for this area")
        elevation_tile_urls = find_elevation_tile_urls(country_gdf)

        logging.info("Loading elevation data")
        elevation = load_elevation(
            elevation_tile_urls, country_gdf, land_cover.shape, land_cover_transform, land_cover_crs
        )

        logging.info("Saving 3D export PNG")
        pixel_size = abs(land_cover_transform.a)
        save_3d_png(rgb, elevation, output_path, pixel_size=pixel_size, land_cover_array=land_cover)
    else:
        logging.info("Saving 2D export PNG")
        save_png(rgb, output_path, land_cover_array=land_cover)

    create_export_info(region_label, output_path)

    # In "3d" mode output_path was renamed above (the "-3d" suffix), so the
    # caller can't know the real final path from --output alone -- write it
    # to GITHUB_OUTPUT so a later CI step (e.g. emailing the result) can
    # reference the file that actually exists instead of guessing its name.
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"output_path={output_path}\n")


if __name__ == "__main__":
    main()
