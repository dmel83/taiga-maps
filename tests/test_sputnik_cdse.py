"""Проверки чистых частей tools-sputnik-cdse.py. Без сети и без rasterio.

Запуск из корня репозитория:
    python3 -m unittest discover -s tests -v
Нужны только numpy и Pillow (версии — requirements-sputnik.txt).
"""

import importlib.util
import io
import math
import os
import sqlite3
import tempfile
import unittest

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


S = load("sputnik_cdse", "tools-sputnik-cdse.py")
OLD = load("sputnik_eox", "tools-sputnik.py")


class TileMath(unittest.TestCase):
    def test_known_tiles(self):
        self.assertEqual((S.tile_x(0.0, 1), S.tile_y(0.0, 1)), (1, 1))
        self.assertEqual(S.tile_x(-180.0, 5), 0)
        self.assertEqual(S.tile_x(180.0, 5), 31)          # край мира не вылезает
        # Тосно, 59.54 с. ш. 30.88 в. д. — сверено с пробной сборкой.
        self.assertEqual((S.tile_x(30.88, 13), S.tile_y(59.54, 13)), (4798, 2399))

    def test_same_count_as_eox_builder(self):
        # Обе программы должны резать область на одни и те же плитки,
        # иначе новый снимок не ляжет на место старого.
        bbox, _ = S.region_bbox(os.path.join(ROOT, "regions.json"), "LENOBL")
        old = sum(1 for z in range(6, 14) for _ in OLD.tiles_in(bbox, z))
        self.assertEqual(S.count_tiles(bbox, 6, 13), old)

    def test_bounds_roundtrip(self):
        z, x, y = 13, 4798, 2399
        w, s, e, n = S.tile_bounds_lonlat(z, x, y)
        self.assertTrue(w <= 30.88 < e and s < 59.54 <= n)
        mw, ms, me, mn = S.tile_bounds_3857(z, x, y)
        self.assertAlmostEqual(me - mw, 2 * S.ПОЛОВИНА_МИРА / 8192, places=6)
        self.assertAlmostEqual(mn - ms, me - mw, places=6)
        # Запад в метрах ↔ запад в градусах.
        self.assertAlmostEqual(mw / S.ПОЛОВИНА_МИРА * 180.0, w, places=9)
        lat = math.degrees(2 * math.atan(math.exp(mn / 6378137.0)) - math.pi / 2)
        self.assertAlmostEqual(lat, n, places=9)

    def test_tms_flip(self):
        self.assertEqual(S.tms_row(13, 0), 8191)
        self.assertEqual(S.tms_row(13, 8191), 0)
        self.assertEqual(S.tms_row(0, 0), 0)
        for y in (0, 5, 2399):
            self.assertEqual(S.tms_row(13, S.tms_row(13, y)), y)

    def test_bbox_intersects(self):
        self.assertTrue(S.bbox_intersects((0, 0, 2, 2), (1, 1, 3, 3)))
        self.assertFalse(S.bbox_intersects((0, 0, 1, 1), (2, 2, 3, 3)))


class Quarters(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(S.parse_quarter("2026-Q3"), (2026, 3))
        self.assertEqual(S.parse_quarter(" 2025-q2 "), (2025, 2))
        self.assertIsNone(S.parse_quarter(""))
        for bad in ("2026", "2026-Q5", "Q3-2026", "2019-Q3"):
            with self.assertRaises(SystemExit):
                S.parse_quarter(bad)

    def test_fallback_order(self):
        # Лето → прошлое лето → весна того же года (см. ЗАПАСНЫЕ_КВАРТАЛЫ).
        self.assertEqual(S.quarter_order(2026, 3), [(2026, 3), (2025, 3), (2026, 2)])
        # Раньше 2020 мозаик нет — такой запасной не предлагаем.
        self.assertEqual(S.quarter_order(2020, 3), [(2020, 3), (2020, 2)])

    def test_quarter_start(self):
        self.assertEqual(S.quarter_start(2026, 3), "2026-07-01T00:00:00Z")
        self.assertEqual(S.quarter_start(2026, 1), "2026-01-01T00:00:00Z")


class GapFill(unittest.TestCase):
    def test_fills_only_holes(self):
        dst = [np.array([[1.0, np.nan]], np.float32) for _ in range(3)]
        dvalid = np.array([[True, False]])
        src = [np.array([[9.0, 2.0]], np.float32) for _ in range(3)]
        svalid = np.array([[True, True]])
        n = S.fill_gaps(dst, dvalid, src, svalid)
        self.assertEqual(n, 1)
        self.assertEqual(dst[0][0, 0], 1.0)       # главный квартал не перетёрт
        self.assertEqual(dst[0][0, 1], 2.0)       # дыра залатана
        self.assertTrue(dvalid.all())

    def test_invalid_source_does_not_fill(self):
        dst = [np.full((1, 2), np.nan, np.float32) for _ in range(3)]
        dvalid = np.zeros((1, 2), bool)
        src = [np.array([[5.0, 6.0]], np.float32) for _ in range(3)]
        n = S.fill_gaps(dst, dvalid, src, np.array([[False, True]]))
        self.assertEqual(n, 1)
        self.assertTrue(np.isnan(dst[1][0, 0]))
        self.assertEqual(dvalid.tolist(), [[False, True]])


class Colour(unittest.TestCase):
    def test_scaling_cdse(self):
        dn = np.array([1700, -32768, 0], np.int16)
        r = S.dn_to_reflectance(dn, -32768, 1e-4, 0.0)
        self.assertAlmostEqual(float(r[0]), 0.17, places=6)
        self.assertTrue(np.isnan(r[1]))
        self.assertEqual(float(r[2]), 0.0)

    def test_stretch_endpoints_and_order(self):
        v = np.array([np.nan, -0.05, 0.0, 0.02, 0.08, 0.30, 0.9], np.float32)
        out = S.stretch_channel(v, 0.0, 0.30)
        self.assertEqual(out.dtype, np.uint8)
        self.assertEqual(out[0], 0)       # пусто → чёрное
        self.assertEqual(out[1], 0)
        self.assertEqual(out[5], 255)
        self.assertEqual(out[6], 255)     # ярче белого — белое
        self.assertTrue(all(out[i] <= out[i + 1] for i in range(1, 6)))

    def test_forest_is_dark_green_not_black(self):
        # Хвойный лес летом: R 0.02, G 0.04, B 0.02.
        rgb = S.to_rgb8(np.array([[0.02]]), np.array([[0.04]]), np.array([[0.02]]))[0, 0]
        r, g, b = (int(c) for c in rgb)
        self.assertTrue(g > r > b, (r, g, b))
        self.assertTrue(50 <= r <= 110 and 80 <= g <= 130, (r, g, b))

    def test_fixed_constants(self):
        # Одна формула на всю Россию: числа не должны «уплыть» незаметно.
        self.assertEqual(S.ЧЁРНОЕ, (0.0, 0.0, 0.01))
        self.assertEqual(S.БЕЛОЕ, (0.30, 0.30, 0.30))
        self.assertEqual(S.ГАММА, 2.2)

    def test_downsample_ignores_holes(self):
        rgb = np.zeros((2, 2, 3), np.uint8)
        rgb[0, 0] = (100, 100, 100)
        rgb[0, 1] = (200, 200, 200)
        valid = np.array([[True, True], [False, False]])
        out, ok = S.downsample2(rgb, valid)
        self.assertEqual(out.shape, (1, 1, 3))
        self.assertEqual(int(out[0, 0, 0]), 150)    # пустые не тянут к чёрному
        self.assertTrue(ok[0, 0])
        out, ok = S.downsample2(rgb, np.zeros((2, 2), bool))
        self.assertFalse(ok[0, 0])


class MBTilesWriter(unittest.TestCase):
    def test_tms_rows_and_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.mbtiles")
            mb = S.MBTiles(path)
            tile = S.encode_jpeg(np.full((256, 256, 3), 90, np.uint8))
            mb.put(13, 4798, 2399, tile)
            mb.put(6, 37, 18, tile)
            mb.set_metadata(S.make_metadata("Проба", (30.7, 59.45, 31.06, 59.63), 6, 13,
                                            ["2026-Q3", "2025-Q3"], {2026, 2025}))
            mb.close()
            db = sqlite3.connect(path)
            rows = db.execute("SELECT zoom_level, tile_column, tile_row FROM tiles"
                              " ORDER BY zoom_level").fetchall()
            self.assertEqual(rows, [(6, 37, 63 - 18), (13, 4798, 8191 - 2399)])
            meta = dict(db.execute("SELECT name, value FROM metadata"))
            self.assertEqual(meta["format"], "jpg")
            self.assertEqual(meta["version"], "1.3")
            self.assertEqual((meta["minzoom"], meta["maxzoom"]), ("6", "13"))
            self.assertEqual(meta["bounds"], "30.700000,59.450000,31.060000,59.630000")
            self.assertEqual(meta["attribution"],
                             "Contains modified Copernicus Sentinel data 2025, 2026")
            self.assertIn("2026-Q3", meta["description"])
            data = db.execute("SELECT tile_data FROM tiles").fetchone()[0]
            self.assertEqual(data[:3], b"\xff\xd8\xff")
            from PIL import Image
            self.assertEqual(Image.open(io.BytesIO(data)).size, (256, 256))
            db.close()


class Catalogue(unittest.TestCase):
    # Обрезок настоящего ответа каталога CDSE от 22.10.2026.
    FEATURE = {
        "id": "Sentinel-2_mosaic_2026_Q3_36VUM_0_0",
        "bbox": [29.370638334482376, 59.49008782422631, 31.23367623992023, 60.42409807001252],
        "properties": {"datetime": "2026-07-01T00:00:00Z", "proj:code": "EPSG:32636"},
        "assets": {k: {"href": "s3://eodata/Global-Mosaics/Sentinel-2/S2MSI_L3__MCQ/2026/07/01/"
                               "Sentinel-2_mosaic_2026_Q3_36VUM_0_0/%s.tif" % k,
                       "nodata": 0 if k == "observations" else -32768}
                   for k in ("B02", "B03", "B04", "B08", "observations")},
    }

    def test_cdse_item(self):
        it = S.cdse_item(self.FEATURE)
        self.assertEqual(it.hrefs["r"], "/vsis3/eodata/Global-Mosaics/Sentinel-2/S2MSI_L3__MCQ/"
                                        "2026/07/01/Sentinel-2_mosaic_2026_Q3_36VUM_0_0/B04.tif")
        self.assertTrue(it.hrefs["b"].endswith("/B02.tif"))
        self.assertTrue(it.hrefs["mask"].endswith("/observations.tif"))
        self.assertEqual((it.nodata, it.scale, it.offset, it.mask),
                         (-32768, 1e-4, 0.0, "observations"))

    def test_catalogue_scale_wins(self):
        f = {**self.FEATURE, "assets": {k: dict(v) for k, v in self.FEATURE["assets"].items()}}
        f["assets"]["B04"]["raster:bands"] = [{"scale": 0.0001, "offset": -0.1}]
        it = S.cdse_item(f)
        self.assertEqual(it.offset, -0.1)

    def test_vsis3(self):
        self.assertEqual(S.s3_to_vsis3("s3://eodata/a/b.tif"), "/vsis3/eodata/a/b.tif")
        self.assertEqual(S.s3_to_vsis3(
            "https://sentinel-cogs.s3.us-west-2.amazonaws.com/sentinel-s2-l2a-cogs/x/B04.tif"),
            "/vsis3/sentinel-cogs/sentinel-s2-l2a-cogs/x/B04.tif")
        with self.assertRaises(ValueError):
            S.s3_to_vsis3("https://example.com/x.tif")

    def test_element84_offset_flag(self):
        asset = {"href": "https://sentinel-cogs.s3.us-west-2.amazonaws.com/p/B04.tif",
                 "raster:bands": [{"nodata": 0, "scale": 0.0001, "offset": -0.1}]}
        feature = {"id": "S2A_36VUM_20260716_1_L2A", "bbox": [0, 0, 1, 1],
                   "properties": {"earthsearch:boa_offset_applied": True},
                   "assets": {k: asset for k in ("red", "green", "blue", "scl")}}
        saved = S.http_json
        try:
            S.http_json = lambda url, body=None: feature
            self.assertEqual(S.element84_item("x").offset, 0.0)
            feature["properties"]["earthsearch:boa_offset_applied"] = False
            self.assertEqual(S.element84_item("x").offset, -0.1)
        finally:
            S.http_json = saved

    def test_stac_search_filters_quarter_and_pages(self):
        other = {**self.FEATURE, "id": "Sentinel-2_mosaic_2026_Q2_36VUM_0_0"}
        pages = {
            "first": {"features": [self.FEATURE, other],
                      "links": [{"rel": "next", "href": "second"}]},
            "second": {"features": [self.FEATURE], "links": []},
        }
        calls = []

        def fake(url, body=None):
            calls.append(url)
            return pages["second"] if url == "second" else pages["first"]
        saved = S.http_json
        try:
            S.http_json = fake
            items = S.stac_search((26.39, 58.367, 35.751, 61.379), 2026, 3)
        finally:
            S.http_json = saved
        self.assertEqual([i.id for i in items], ["Sentinel-2_mosaic_2026_Q3_36VUM_0_0"])
        self.assertEqual(len(calls), 2)
        self.assertIn("datetime=2026-07-01T00%3A00%3A00Z", calls[0])


class Credentials(unittest.TestCase):
    def test_missing_keys_explained(self):
        saved = {k: os.environ.pop(k, None) for k in ("CDSE_S3_ACCESS_KEY", "CDSE_S3_SECRET_KEY")}
        try:
            with self.assertRaises(SystemExit) as cm:
                S.require_credentials()
            text = str(cm.exception)
            self.assertIn("CDSE_S3_ACCESS_KEY", text)
            self.assertIn("eodata-s3keysmanager.dataspace.copernicus.eu", text)
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
