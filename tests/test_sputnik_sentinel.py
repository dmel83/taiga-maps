"""Проверки чистых частей tools-sputnik-sentinel.py. Без сети и без rasterio.

Запуск из корня репозитория:
    python3 -m unittest discover -s tests -v
Нужны только numpy и Pillow (версии — requirements-sputnik.txt).
"""

import datetime
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


S = load("sputnik_sentinel", "tools-sputnik-sentinel.py")
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
                                            "Sentinel-2 L2A, лето 2026.", {2026, 2025}))
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
            self.assertIn("лето 2026", meta["description"])
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
        # Collection 1: смещение в числах есть — берём −0.1 из каталога.
        # Старая коллекция с флагом boa_offset_applied — смещение ноль.
        asset = {"href": "https://sentinel-cogs.s3.us-west-2.amazonaws.com/p/B04.tif",
                 "raster:bands": [{"nodata": 0, "scale": 0.0001, "offset": -0.1}]}
        feature = {"id": "S2A_T36VUM_20260716T093147_L2A", "bbox": [0, 0, 1, 1],
                   "properties": {"earthsearch:boa_offset_applied": True,
                                  "grid:code": "MGRS-36VUM", "eo:cloud_cover": 0.8,
                                  "datetime": "2026-07-16T09:44:00Z"},
                   "assets": {k: asset for k in ("red", "green", "blue", "scl")}}
        self.assertEqual(S.l2a_item(feature).offset, 0.0)
        del feature["properties"]["earthsearch:boa_offset_applied"]
        it = S.l2a_item(feature)
        self.assertEqual(it.offset, -0.1)
        self.assertEqual((it.grid, it.year, it.pass_key), ("MGRS-36VUM", 2026, "2026-07-16"))

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
            items = S.cdse_search((26.39, 58.367, 35.751, 61.379), 2026, 3)
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
            self.assertIn("без слова CDSE", text)
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


def scene(i, grid="MGRS-36VUM", cloud=0.0, empty=0.0, day="2026-07-01", bbox=(30, 59, 32, 60)):
    return S.Item("s%d" % i, list(bbox), {}, 0, 1e-4, -0.1, "scl", grid=grid,
                  cloud=cloud, empty=empty, when=day + "T09:00:00Z")


class Composite(unittest.TestCase):
    def test_normalize_dn(self):
        dn = np.array([0, 1000, 1169, 500, 60000], np.uint16)
        # Collection 1: число − 1000 = отражение × 10000.
        out = S.normalize_dn(dn, 1e-4, -0.1)
        self.assertEqual(out.tolist(), [0, 1, 169, 1, 59000])
        # Смещение уже снято — числа как есть.
        self.assertEqual(S.normalize_dn(dn, 1e-4, 0.0).tolist(), [0, 1000, 1169, 500, 60000])

    def test_scl_mask(self):
        clear = S.scl_clear(np.arange(12, dtype=np.uint8))
        self.assertEqual([int(c) for c in np.flatnonzero(clear)], [2, 4, 5, 6])
        self.assertFalse(clear[11])          # снег летом — выкидываем
        self.assertFalse(clear[3])           # тень облака

    def test_masked_median(self):
        # Точка 1: ясные 100, 300, 200 -> 200 (облако 5000 не в счёт).
        # Точка 2: ясные 100, 300 -> 200 (среднее двух средних).
        # Точка 3: ясных нет -> NaN, k = 0.
        stack = np.array([[100, 100, 7], [5000, 300, 8], [300, 9000, 9], [200, 9000, 9]],
                         np.uint16).reshape(4, 1, 3)
        clear = np.array([[1, 1, 0], [0, 1, 0], [1, 0, 0], [1, 0, 0]], bool).reshape(4, 1, 3)
        med, k = S.masked_median(stack, clear)
        self.assertEqual(med[0, 0], 200.0)
        self.assertEqual(med[0, 1], 200.0)
        self.assertTrue(np.isnan(med[0, 2]))
        self.assertEqual(k[0].tolist(), [3, 2, 0])

    def test_median_ignores_one_missed_cloud(self):
        # Пять наблюдений леса, одно — облако, пропущенное SCL (ярко).
        stack = np.array([170, 165, 4800, 180, 175], np.uint16).reshape(5, 1, 1)
        med, _ = S.masked_median(stack, np.ones((5, 1, 1), bool))
        self.assertEqual(med[0, 0], 175.0)

    def test_composite_fallback_to_best_scene(self):
        # Два снимка (первый — лучший). Точка 0: ясно во втором — медиана.
        # Точка 1: оба облачные — берём лучший как есть (первый).
        # Точка 2: данных нет нигде — пусто.
        def band(a, b):
            return np.array([a, b], np.uint16).reshape(2, 1, 3)
        stacks = [band([400, 3000, 0], [200, 5000, 0]) for _ in range(3)]
        clear = np.array([[False, False, False], [True, False, False]]).reshape(2, 1, 3)
        med, med_ok, raw, raw_ok = S.composite(stacks, clear)
        self.assertEqual(med_ok[0].tolist(), [True, False, False])
        self.assertAlmostEqual(float(med[0][0, 0]), 0.02, places=6)
        self.assertEqual(raw_ok[0].tolist(), [True, True, False])
        self.assertAlmostEqual(float(raw[0][0, 1]), 0.30, places=6)
        self.assertTrue(np.isnan(raw[0][0, 2]))

    def test_pick_scenes_per_tile_best_first(self):
        items = [scene(1, cloud=50), scene(2, cloud=1), scene(3, cloud=10), scene(4, cloud=1),
                 scene(5, grid="MGRS-35VPG", cloud=5),
                 scene(6, grid="MGRS-35VPG", cloud=0, empty=90),   # край полосы — хуже
                 scene(7, cloud=0, bbox=(40, 40, 41, 41))]          # далеко от окна
        for i, it in enumerate(items):
            it.pass_key = "день%d" % i                              # все пролёты разные
        got = S.pick_scenes(items, (30.5, 59.5, 30.6, 59.6), 2)
        self.assertEqual([i.id for i in got], ["s2", "s4", "s5", "s6"])

    def test_pick_scenes_same_pass_once(self):
        a, b = scene(1, cloud=1), scene(2, cloud=2)          # один день, один квадрат
        got = S.pick_scenes([a, b, scene(3, cloud=3, day="2026-08-01")],
                            (30.5, 59.5, 30.6, 59.6), 5)
        self.assertEqual([i.id for i in got], ["s1", "s3"])

    def test_last_summer(self):
        self.assertEqual(S.last_summer(datetime.date(2026, 10, 22)), 2026)
        self.assertEqual(S.last_summer(datetime.date(2026, 9, 15)), 2025)
        self.assertEqual(S.last_summer(datetime.date(2026, 9, 16)), 2026)
        self.assertEqual(S.summer_interval(2026),
                         "2026-06-01T00:00:00Z/2026-09-15T23:59:59Z")

    def test_gap_box_and_sub_window(self):
        valid = np.ones((8, 8), bool)
        self.assertIsNone(S.gap_box(valid))
        valid[2, 5] = valid[4, 3] = False
        box = S.gap_box(valid)
        self.assertEqual(box, (2, 5, 3, 6))
        win = (0.0, 0.0, 80.0, 80.0, 8, 8)            # 10 м на точку
        self.assertEqual(S.sub_window(win, box), (30.0, 30.0, 60.0, 60.0, 3, 3))

    def test_aws_search_pages_by_post_body(self):
        f = {"id": "S2A_T36VUM_20260716T093147_L2A", "bbox": [0, 0, 1, 1],
             "properties": {"datetime": "2026-07-16T09:44:00Z"},
             "assets": {k: {"href": "https://b.s3.us-west-2.amazonaws.com/x/%s.tif" % k}
                        for k in ("red", "green", "blue", "scl")}}
        g = dict(f, id="S2B_T36VUM_20260801T093147_L2A")
        calls = []

        def fake(url, body=None):
            calls.append((url, body))
            if body and body.get("next") == "t2":
                return {"features": [g], "links": []}
            return {"features": [f], "links": [{"rel": "next", "method": "POST",
                                                "href": url, "body": {"next": "t2"}}]}
        saved = S.http_json
        try:
            S.http_json = fake
            items = S.aws_search((30, 59, 31, 60), 2026, "sentinel-2-c1-l2a")
        finally:
            S.http_json = saved
        self.assertEqual([i.id for i in items], [f["id"], g["id"]])
        self.assertEqual(calls[0][1]["datetime"], "2026-06-01T00:00:00Z/2026-09-15T23:59:59Z")
        self.assertEqual(calls[0][1]["collections"], ["sentinel-2-c1-l2a"])
        self.assertEqual(calls[1][1], {"next": "t2"})


if __name__ == "__main__":
    unittest.main()
