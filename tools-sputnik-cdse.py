#!/usr/bin/env python3
"""Собрать спутниковый снимок области из мозаик Copernicus в MBTiles.

ДОБАВЛЕНО 22.10.2026 — второй источник спутника, свежий и без чужого
сервера плиток. Старый сборщик tools-sputnik.py (EOX 2017) остаётся
запасным, его не трогаем.

ИСТОЧНИК. Copernicus Data Space Ecosystem (CDSE), коллекция
«Sentinel-2 Global Mosaics» — квартальные безоблачные мозаики уровня
L3 (тип продукта S2MSI_L3__MCQ), 10 м на пиксель, каналы B02, B03, B04,
B08 и observations (сколько чистых снимков легло в пиксель).
Сверено 22.10.2026:
  каталог  https://stac.dataspace.copernicus.eu/v1
           коллекция sentinel-2-global-mosaics; у предмета datetime —
           первый день квартала, id вида
           Sentinel-2_mosaic_2026_Q3_36VUM_0_0, своя UTM-проекция
           (proj:code), ресурсы B02/B03/B04 — int16, nodata −32768,
           observations — uint16, nodata 0, COG 10008×10008;
  файлы    s3://eodata/Global-Mosaics/Sentinel-2/S2MSI_L3__MCQ/
           ГГГГ/ММ/01/<id>/B04.tif, адрес S3
           https://eodata.dataspace.copernicus.eu — только с ключами,
           без ключей отвечает 403;
  масштаб  документация CDSE, раздел «Sentinel-2 Level 3 Quarterly
           Mosaics» (documentation.dataspace.copernicus.eu/Data/
           SentinelMissions/Sentinel2.html): значения каналов сначала
           переведены в отражение, затем взят первый квартиль и
           «multiply it by 10000». Смещения (как −1000 у L2A после
           2022 года) в описании мозаики НЕТ — значит отражение =
           число / 10000, без сдвига. Если в каталоге у ресурса
           появятся raster:scale/raster:offset — берём их (см.
           Item.scale/offset), они главнее;
  пределы  documentation.dataspace.copernicus.eu/Quotas.html:
           S3 — 4 одновременных соединения, 20 МБ/с на соединение,
           2000 запросов в минуту, 12 ТБ за 30 дней. Мы читаем в одно
           соединение — с запасом.
  лицензия Copernicus Sentinel Data Legal Notice: можно раздавать
           и изменять, подпись обязательна — ATTRIBUTION ниже.

ЧТО ДЕЛАЕТ.
  1. Ищет в каталоге (без ключей — каталог открыт) предметы квартала,
     задевающие рамку области. Квартал по умолчанию — самое свежее
     лето (Q3), у которого есть хоть один предмет над областью.
  2. Идёт по области кусками: кусок = одна плитка 9-го приближения
     (4096×4096 точек на 13-м). Для куска через GDAL (/vsis3/) читает
     ровно нужные окна B04/B03/B02/observations всех задевающих его
     предметов, перепроецирует в EPSG:3857 и сшивает.
  3. Дыры (observations == 0 — за квартал не было ни одного чистого
     снимка) латает следующими кварталами по порядку ЗАПАСНЫЕ_КВАРТАЛЫ.
  4. Переводит отражение в цвет ОДНОЙ формулой на всю Россию
     (stretch_channel), режет на плитки 256×256 c 13-го по 9-е, а 8–6
     собирает из 9-го в конце. JPEG, качество 80.
  5. Пишет MBTiles 1.3, ряды TMS (снизу вверх) — ровно как ждёт
     приложение (region/SatPack, osmdroid): файл
     TAIGA-<КЛЮЧ>-satellite.mbtiles.

ЗАПУСК.
  python3 tools-sputnik-cdse.py --key LENOBL --count-only   # без ключей
  CDSE_S3_ACCESS_KEY=… CDSE_S3_SECRET_KEY=… \
      python3 tools-sputnik-cdse.py --key LENOBL [--quarter 2026-Q3]
Проба без ключей на одной открытой сцене Element84 (только чтобы
проверить цвет и нарезку, данные не те — это L2A, а не L3):
  python3 tools-sputnik-cdse.py --source element84-proba \
      --scene S2A_36VUM_20260716_1_L2A --key TOSNO \
      --bbox 30.70,59.45,31.06,59.63

Нужны numpy, Pillow и rasterio (в нём свой GDAL) — версии закреплены
в requirements-sputnik.txt. Чистые части (счёт плиток, цвет, латание,
запись MBTiles) от rasterio не зависят и проверяются тестами
tests/test_sputnik_cdse.py без сети.
"""

import argparse
import datetime
import io
import json
import math
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

import numpy as np

# ---------------------------------------------------------------- источник

STAC_URL = "https://stac.dataspace.copernicus.eu/v1"
COLLECTION = "sentinel-2-global-mosaics"
S3_ENDPOINT = "eodata.dataspace.copernicus.eu"
# Подпись по условиям Copernicus Sentinel Data Legal Notice. Год —
# год снимка; он подставляется по фактически взятым кварталам.
ATTRIBUTION = "Contains modified Copernicus Sentinel data {years}"
USER_AGENT = "TAIGA-offline-maps/1.0 (+https://github.com/dmel83/taiga-maps)"

# ЗАПАСНЫЕ КВАРТАЛЫ — чем латать дыры главного квартала, по порядку.
# Сначала то же лето год назад, потом весна того же года. Почему лето
# прошлого года раньше весны: весенний снимок другого цвета (в апреле
# на севере ещё снег, листвы нет), заплатка из него видна пятном.
# Лето к лету по цвету почти не отличается.
ЗАПАСНЫЕ_КВАРТАЛЫ = ((-1, 3), (0, 2))

# ---------------------------------------------------------------- цвет
#
# Один набор чисел на всю Россию. РАЗБИРАЕМЫЙ ДЕФЕКТ, которого здесь
# нет нарочно: если растягивать цвет по каждой области отдельно
# (по её гистограмме), на стыке Ленобласти и Карелии лес будет двух
# разных зелёных — граница видна как шов. Поэтому формула жёсткая.
#
# Откуда числа. Это отражение у поверхности (0…1) после атмосферной
# поправки. Летом в средней полосе и тайге:
#   хвойный лес   R≈0.02  G≈0.04  B≈0.02 — самое частое, и оно тёмное;
#   луг, поле     R≈0.05  G≈0.08  B≈0.03;
#   вода          0.01–0.03 во всех трёх;
#   песок, дорога, крыши  0.10–0.30.
# Обычный «2.5 × отражение» (белое = 0.4) делает тайгу почти чёрной
# (0.02 → 13 из 255), на ней не видно ни просек, ни болот. Поэтому:
#   БЕЛОЕ 0.30 — всё, что ярче (крыши, песок, облака-недобитки),
#     уходит в белое; таких мест на нашей карте мало;
#   ГАММА 2.2 — как у экрана (sRGB): тёмное вытягивается, лес
#     становится тёмно-зелёным (≈70,100,55), а не чёрным;
#   ЧЁРНОЕ у синего 0.01 — после поправки в синем остаётся немного
#     дымки, без этого вычета весь снимок голубоватый и блёклый.
# Проверено на сцене у Тосно (июль 2026), см. описание PR.
ЧЁРНОЕ = (0.0, 0.0, 0.01)       # R, G, B
БЕЛОЕ = (0.30, 0.30, 0.30)
ГАММА = 2.2
JPEG_КАЧЕСТВО = 80

TILE = 256


# ---------------------------------------------------------------- плитки

def tile_x(lon, zoom):
    n = 1 << zoom
    return min(n - 1, max(0, int(math.floor((lon + 180.0) / 360.0 * n))))


def tile_y(lat, zoom):
    n = 1 << zoom
    rad = math.radians(lat)
    y = (1.0 - math.log(math.tan(rad) + 1.0 / math.cos(rad)) / math.pi) / 2.0 * n
    return min(n - 1, max(0, int(math.floor(y))))


def tile_range(bbox, zoom):
    """(x0, y0, x1, y1) включительно — плитки XYZ, задевающие рамку."""
    west, south, east, north = bbox
    return (tile_x(west, zoom), tile_y(north, zoom),
            tile_x(east, zoom), tile_y(south, zoom))


def tms_row(zoom, y):
    """MBTiles хранит ряды снизу вверх (TMS), а считаем мы сверху (XYZ)."""
    return (1 << zoom) - 1 - y


ПОЛОВИНА_МИРА = 20037508.342789244   # метров, EPSG:3857


def tile_bounds_3857(zoom, x, y):
    """(запад, юг, восток, север) плитки XYZ в метрах EPSG:3857."""
    size = 2 * ПОЛОВИНА_МИРА / (1 << zoom)
    west = -ПОЛОВИНА_МИРА + x * size
    north = ПОЛОВИНА_МИРА - y * size
    return west, north - size, west + size, north


def tile_bounds_lonlat(zoom, x, y):
    n = 1 << zoom

    def lat(row):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * row / n))))
    return x / n * 360.0 - 180.0, lat(y + 1), (x + 1) / n * 360.0 - 180.0, lat(y)


def bbox_intersects(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def count_tiles(bbox, minzoom, maxzoom):
    total = 0
    for z in range(minzoom, maxzoom + 1):
        x0, y0, x1, y1 = tile_range(bbox, z)
        total += (x1 - x0 + 1) * (y1 - y0 + 1)
    return total


# ---------------------------------------------------------------- кварталы

def parse_quarter(text):
    """'2026-Q3' → (2026, 3). Пусто → None (выбрать самому)."""
    if not text:
        return None
    t = text.strip().upper().replace("_", "-")
    try:
        year, q = t.split("-Q")
        year, q = int(year), int(q)
    except ValueError:
        raise SystemExit("квартал пишется так: 2026-Q3, а не «%s»" % text)
    if not 1 <= q <= 4 or year < 2020:
        raise SystemExit("нет такого квартала: %s" % text)
    return year, q


def quarter_name(year, q):
    return "%d-Q%d" % (year, q)


def quarter_order(year, q, extra=ЗАПАСНЫЕ_КВАРТАЛЫ):
    """Главный квартал и запасные по порядку, без повторов."""
    order = [(year, q)]
    for dy, qq in extra:
        cand = (year + dy, qq)
        if cand not in order and cand[0] >= 2020:
            order.append(cand)
    return order


def quarter_start(year, q):
    return "%04d-%02d-01T00:00:00Z" % (year, 3 * (q - 1) + 1)


# ---------------------------------------------------------------- цвет

def dn_to_reflectance(dn, nodata, scale, offset):
    """Целые числа файла → отражение float32; nodata → NaN."""
    refl = dn.astype(np.float32) * np.float32(scale) + np.float32(offset)
    refl[dn == nodata] = np.nan
    return refl


def stretch_channel(refl, black, white, gamma=ГАММА):
    """Отражение → 0…255. Одна и та же формула для всех областей."""
    v = (np.nan_to_num(refl, nan=0.0) - black) / (white - black)
    v = np.clip(v, 0.0, 1.0) ** (1.0 / gamma)
    return np.round(v * 255.0).astype(np.uint8)


def to_rgb8(r, g, b):
    return np.dstack([stretch_channel(c, ЧЁРНОЕ[i], БЕЛОЕ[i])
                      for i, c in enumerate((r, g, b))])


# ---------------------------------------------------------------- латание

def fill_gaps(dst, dst_valid, src, src_valid):
    """Заполнить дыры dst из src там, где dst пуст, а src есть.

    dst, src — списки массивов (R, G, B) одной формы; *_valid — bool.
    Уже заполненное НЕ перетирается: главный квартал важнее запасных.
    Возвращает число залатанных точек.
    """
    take = (~dst_valid) & src_valid
    n = int(take.sum())
    if n:
        for d, s in zip(dst, src):
            d[take] = s[take]
        dst_valid |= take
    return n


def downsample2(rgb, valid):
    """Уменьшить вдвое: среднее по четвёркам, пустые точки не в счёт."""
    h, w = valid.shape
    v = valid.reshape(h // 2, 2, w // 2, 2).astype(np.float32)
    weight = v.sum(axis=(1, 3))
    out = np.zeros((h // 2, w // 2, 3), np.uint8)
    for c in range(3):
        s = (rgb[:, :, c].astype(np.float32).reshape(h // 2, 2, w // 2, 2) * v).sum(axis=(1, 3))
        with np.errstate(invalid="ignore", divide="ignore"):
            out[:, :, c] = np.where(weight > 0, np.round(s / np.maximum(weight, 1)), 0)
    return out, weight > 0


def encode_jpeg(rgb):
    from PIL import Image
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, "JPEG", quality=JPEG_КАЧЕСТВО, optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------- MBTiles

class MBTiles:
    """Писатель MBTiles 1.3. Принимает XYZ, кладёт TMS."""

    def __init__(self, path):
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS metadata (name TEXT, value TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS tiles (zoom_level INTEGER,"
                        " tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS tile_index ON tiles"
                        " (zoom_level, tile_column, tile_row)")
        self.count = 0

    def put(self, z, x, y, data):
        self.db.execute("INSERT OR REPLACE INTO tiles VALUES (?, ?, ?, ?)",
                        (z, x, tms_row(z, y), sqlite3.Binary(data)))
        self.count += 1
        if self.count % 1000 == 0:
            self.db.commit()

    def set_metadata(self, meta):
        self.db.execute("DELETE FROM metadata")
        self.db.executemany("INSERT INTO metadata (name, value) VALUES (?, ?)",
                            [(k, str(v)) for k, v in meta.items()])
        self.db.commit()

    def close(self, vacuum=True):
        self.db.commit()
        if vacuum:
            self.db.execute("VACUUM")
        self.db.close()


def make_metadata(title, bbox, minzoom, maxzoom, quarters_used, years):
    west, south, east, north = bbox
    return {
        "name": "ТАЙГА — спутник: " + title,
        "format": "jpg",
        "type": "baselayer",
        "version": "1.3",
        "minzoom": minzoom,
        "maxzoom": maxzoom,
        "bounds": "%.6f,%.6f,%.6f,%.6f" % (west, south, east, north),
        "center": "%.6f,%.6f,%d" % ((west + east) / 2, (south + north) / 2, min(maxzoom, 10)),
        "attribution": ATTRIBUTION.format(years=", ".join(str(y) for y in sorted(years))),
        "description": ("Copernicus Sentinel-2 Global Mosaics (S2MSI_L3__MCQ, 10 м), квартал "
                        + ", затем ".join(quarters_used)
                        + ". Единая цветовая формула ТАЙГИ: чёрное %s, белое %s, гамма %.1f."
                        % (ЧЁРНОЕ, БЕЛОЕ, ГАММА)),
    }


# ---------------------------------------------------------------- каталог

class Item:
    """Один предмет каталога: где лежат каналы и как их читать."""

    def __init__(self, item_id, bbox, hrefs, nodata, scale, offset, mask):
        self.id = item_id
        self.bbox = bbox
        self.hrefs = hrefs        # {'r','g','b','mask'} → путь /vsis3/…
        self.nodata = nodata
        self.scale = scale
        self.offset = offset
        self.mask = mask          # 'observations' или 'scl'


def s3_to_vsis3(href):
    """s3://eodata/… и https://<bucket>.s3.<region>.amazonaws.com/… → /vsis3/…"""
    if href.startswith("s3://"):
        return "/vsis3/" + href[5:]
    p = urllib.parse.urlparse(href)
    if p.netloc.endswith(".amazonaws.com") and ".s3." in p.netloc:
        return "/vsis3/" + p.netloc.split(".s3.")[0] + p.path
    raise ValueError("не понимаю адрес " + href)


def _scale_offset(asset, default_scale, default_offset):
    """Масштаб каталога главнее нашего умолчания — но только если он есть."""
    rb = (asset.get("raster:bands") or [{}])[0]
    scale = asset.get("raster:scale", rb.get("scale", default_scale))
    offset = asset.get("raster:offset", rb.get("offset", default_offset))
    return float(scale), float(offset)


def cdse_item(f):
    a = f["assets"]
    scale, offset = _scale_offset(a["B04"], 1e-4, 0.0)
    return Item(f["id"], f["bbox"],
                {"r": s3_to_vsis3(a["B04"]["href"]), "g": s3_to_vsis3(a["B03"]["href"]),
                 "b": s3_to_vsis3(a["B02"]["href"]),
                 "mask": s3_to_vsis3(a["observations"]["href"])},
                int(a["B04"].get("nodata", -32768)), scale, offset, "observations")


def http_json(url, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": USER_AGENT, "Content-Type": "application/json"})
    pause = 3.0
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as e:  # обрыв, 5xx — каталог иногда задумывается
            if attempt == 5:
                raise SystemExit("каталог не отвечает: %s (%s)" % (url, e))
            print("  каталог: %s, повтор через %.0f с" % (e, pause), flush=True)
            time.sleep(pause)
            pause *= 2


def stac_search(bbox, year, q):
    """Все предметы квартала над рамкой. Листаем страницы по ссылке next.

    datetime у мозаики — первый день квартала, ищем ровно по нему.
    Сверено 22.10.2026: запрос с интервалом «весь квартал» даёт то же,
    а дата без времени («2026-07-01/2026-09-30») — ошибка 400.
    """
    tag = "_%d_Q%d_" % (year, q)
    url = STAC_URL + "/search?" + urllib.parse.urlencode({
        "collections": COLLECTION,
        "bbox": ",".join("%.6f" % v for v in bbox),
        "datetime": quarter_start(year, q),
        "limit": 200,
    })
    items, seen = [], set()
    while url:
        page = http_json(url)
        for f in page.get("features", []):
            if tag in f["id"] and f["id"] not in seen:   # чужой квартал — мимо
                seen.add(f["id"])
                items.append(cdse_item(f))
        url = next((l["href"] for l in page.get("links", []) if l.get("rel") == "next"), None)
    return items


def newest_summer(bbox, today=None):
    """Самое свежее лето (Q3), у которого есть предметы над рамкой."""
    today = today or datetime.date.today()
    for year in range(today.year, 2019, -1):
        if year == today.year and today.month < 10:
            continue                         # лето ещё не кончилось
        if stac_search(bbox, year, 3):
            return year, 3
    raise SystemExit("в каталоге нет ни одного летнего квартала над этой рамкой")


def element84_item(scene):
    """Проба: одна открытая сцена L2A из Element84 (s3://sentinel-cogs)."""
    f = http_json("https://earth-search.aws.element84.com/v1/collections/"
                  "sentinel-2-l2a/items/" + scene)
    a = f["assets"]
    # РАЗБИРАЕМЫЙ ДЕФЕКТ (пойман пробой 22.10.2026). У L2A с базовой
    # линией ≥ 04.00 в числах сидит +1000, и Element84 пишет в
    # raster:bands offset −0.1. Но в свойствах той же сцены стоит
    # earthsearch:boa_offset_applied = true: сдвиг они УЖЕ сняли сами.
    # Применить его второй раз — медиана леса уходит в −0.08, снимок
    # чёрный. Поэтому при этом флаге смещение — ноль.
    scale, offset = _scale_offset(a["red"], 1e-4, -0.1)
    if f["properties"].get("earthsearch:boa_offset_applied"):
        offset = 0.0
    return Item(f["id"], f["bbox"],
                {"r": s3_to_vsis3(a["red"]["href"]), "g": s3_to_vsis3(a["green"]["href"]),
                 "b": s3_to_vsis3(a["blue"]["href"]), "mask": s3_to_vsis3(a["scl"]["href"])},
                0, scale, offset, "scl")


# SCL, которые мозаика CDSE считает негодными (по той же документации):
# 1 брак, 3 тень облака, 7 неясно, 8–9 облака, 10 перистые. 0 — пусто.
SCL_НЕГОДНЫЕ = (0, 1, 3, 7, 8, 9, 10)


# ---------------------------------------------------------------- чтение

def gdal_options(source):
    common = {
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
        "GDAL_HTTP_MAX_RETRY": 8,
        "GDAL_HTTP_RETRY_DELAY": 4,
        "GDAL_HTTP_TIMEOUT": 120,
        "VSI_CACHE": "TRUE",
        "VSI_CACHE_SIZE": 512 * 1024 * 1024,
        "GDAL_CACHEMAX": 1024,
        "GDAL_HTTP_USERAGENT": USER_AGENT,
    }
    if source == "cdse":
        common.update({
            "AWS_S3_ENDPOINT": S3_ENDPOINT,
            "AWS_VIRTUAL_HOSTING": "FALSE",
            "AWS_HTTPS": "YES",
            # Документация CDSE для aws cli пишет регион «default».
            "AWS_REGION": "default",
            "AWS_ACCESS_KEY_ID": os.environ["CDSE_S3_ACCESS_KEY"],
            "AWS_SECRET_ACCESS_KEY": os.environ["CDSE_S3_SECRET_KEY"],
        })
    else:
        common.update({"AWS_NO_SIGN_REQUEST": "YES", "AWS_REGION": "us-west-2"})
    return common


def read_quarter(items, west, south, east, north, width, height):
    """Сшить предметы в окно EPSG:3857. → ([R,G,B] отражение, годность)."""
    import rasterio
    from rasterio.transform import from_bounds
    from rasterio.warp import reproject, Resampling

    transform = from_bounds(west, south, east, north, width, height)
    out, valid = None, np.zeros((height, width), bool)
    for it in items:
        bands = []
        for key in ("r", "g", "b"):
            with rasterio.open(it.hrefs[key]) as src:
                dst = np.full((height, width), it.nodata, dtype=src.dtypes[0])
                reproject(rasterio.band(src, 1), dst, src_nodata=it.nodata,
                          dst_transform=transform, dst_crs="EPSG:3857",
                          dst_nodata=it.nodata, resampling=Resampling.bilinear)
                bands.append(dst)
        with rasterio.open(it.hrefs["mask"]) as src:
            m = np.zeros((height, width), dtype=src.dtypes[0])
            reproject(rasterio.band(src, 1), m, src_nodata=0, dst_transform=transform,
                      dst_crs="EPSG:3857", dst_nodata=0, resampling=Resampling.nearest)
        if it.mask == "observations":
            ok = m > 0
        else:
            ok = ~np.isin(m, SCL_НЕГОДНЫЕ)
        for b in bands:
            ok &= b != it.nodata
        refl = [dn_to_reflectance(b, it.nodata, it.scale, it.offset) for b in bands]
        if out is None:
            out = [np.full((height, width), np.nan, np.float32) for _ in range(3)]
        # Предметы соседних UTM-квадратов перекрываются краями; кто
        # первый лёг — тот и остаётся, второй только латает пустое.
        fill_gaps(out, valid, refl, ok)
    if out is None:
        out = [np.full((height, width), np.nan, np.float32) for _ in range(3)]
    return out, valid


def read_with_retry(items, *window, attempts=6):
    """read_quarter с повторами.

    РАЗБИРАЕМЫЙ ДЕФЕКТ (пойман пробой 22.10.2026): посреди чтения
    сцены соединение оборвалось — «TIFFReadEncodedTile() failed»,
    и вся сборка упала. Сам GDAL повторяет только ответы 5xx/429,
    а оборванный посреди блок не повторяет. За шесть часов на
    сотне кусков такое случится почти наверняка, поэтому кусок
    перечитывается целиком с нарастающей паузой.
    """
    pause = 10.0
    for attempt in range(attempts):
        try:
            return read_quarter(items, *window)
        except Exception as e:   # rasterio.errors.* и обрывы curl
            text = str(e)
            if "403" in text or "Access Denied" in text or "AccessDenied" in text:
                raise SystemExit("S3 отвечает «нет доступа» (403): ключи CDSE неверные или "
                                 "истекли. Создайте новые и обновите секреты. (%s)" % text[:200])
            if attempt == attempts - 1:
                raise SystemExit("кусок так и не прочитался: %s" % text[:300])
            print("  чтение сорвалось (%s), повтор через %.0f с" % (text[:120], pause), flush=True)
            time.sleep(pause)
            pause *= 2


# ---------------------------------------------------------------- сборка

def region_bbox(regions_file, key):
    with open(regions_file, encoding="utf-8") as f:
        regions = json.load(f)["regions"]
    for region in regions:
        if region["key"] == key:
            if not region.get("bbox"):
                raise SystemExit("у области %s нет рамки — спутник по ней не собрать" % key)
            west, south, east, north = (float(v) for v in region["bbox"].split(","))
            return (west, south, east, north), region["title"]
    raise SystemExit("нет области с ключом " + key)


def require_credentials():
    missing = [n for n in ("CDSE_S3_ACCESS_KEY", "CDSE_S3_SECRET_KEY")
               if not os.environ.get(n, "").strip()]
    if missing:
        raise SystemExit(
            "НЕТ КЛЮЧЕЙ COPERNICUS: " + ", ".join(missing) + ".\n"
            "Снимки CDSE отдаются только с ключами S3. Что сделать:\n"
            " 1) войти на https://eodata-s3keysmanager.dataspace.copernicus.eu/\n"
            "    своей учётной записью Copernicus и нажать «Add Credentials»;\n"
            " 2) в GitHub: Settings → Secrets and variables → Actions →\n"
            "    New repository secret — CDSE_S3_ACCESS_KEY (Access Key)\n"
            "    и CDSE_S3_SECRET_KEY (Secret Key);\n"
            " 3) заново сохранить zakaz-sputnik.txt.")


def build(args):
    if args.bbox:
        bbox = tuple(float(v) for v in args.bbox.split(","))
        title = args.title or args.key
    else:
        bbox, title = region_bbox(args.regions, args.key)
    if args.source == "cdse" and not args.count_only:
        require_credentials()          # падать сразу, а не через час
    minz, maxz = args.minzoom, args.maxzoom
    chunk_z = max(minz, maxz - 4)          # кусок — плитка 9-го при maxzoom 13
    per = 1 << (maxz - chunk_z)            # плиток maxzoom на сторону куска

    # --- что читать
    if args.source == "cdse":
        wanted = parse_quarter(args.quarter) or newest_summer(bbox)
        order = quarter_order(*wanted)
        cache = {}

        def items_for(qq):
            if qq not in cache:
                cache[qq] = stac_search(bbox, *qq)
                print("  квартал %s: %d предметов" % (quarter_name(*qq), len(cache[qq])),
                      flush=True)
            return cache[qq]
        main_items = items_for(order[0])
        if not main_items:
            raise SystemExit("в каталоге нет квартала %s над областью" % quarter_name(*order[0]))
    else:
        # Сцены через запятую: первая — главная, остальные латают её
        # облака так же, как запасные кварталы латают мозаику.
        probe = {s: [element84_item(s)] for s in args.scene.split(",") if s}
        order = [(s, 0) for s in probe]

        def items_for(qq):
            return probe[qq[0]]
        main_items = probe[order[0][0]]

    print("%s: плиток %d (уровни %d–%d), кусков %d, кварталы по порядку: %s"
          % (title, count_tiles(bbox, minz, maxz), minz, maxz,
             count_tiles(bbox, chunk_z, chunk_z),
             ", ".join(quarter_name(*q) if q[1] else q[0] for q in order)), flush=True)
    if args.source == "cdse":
        # Один предмет — три канала по ~155 МБ (file:size в каталоге,
        # сверено на 36VUM 2026-Q3). Читается только часть над областью.
        print("  главный квартал: %d предметов; их B02–B04 целиком ≈ %.0f ГБ, "
              "читается только часть над областью" % (len(main_items), len(main_items) * 0.48),
              flush=True)
    if args.count_only:
        return

    import rasterio
    out_path = args.out or "TAIGA-%s-satellite.mbtiles" % args.key
    part = out_path + ".part"
    if os.path.exists(part):
        os.remove(part)
    mb = MBTiles(part)
    used, years = [], set()
    lowres = {}                        # плитки chunk_z для сборки 8–6
    rx0, ry0, rx1, ry1 = tile_range(bbox, maxz)
    cx0, cy0, cx1, cy1 = tile_range(bbox, chunk_z)
    chunks = [(cx, cy) for cy in range(cy0, cy1 + 1) for cx in range(cx0, cx1 + 1)]
    started = time.time()
    checked_scale = False
    with rasterio.Env(**gdal_options(args.source)):
        for n, (cx, cy) in enumerate(chunks, 1):
            # Окно куска, урезанное по рамке области до целых плиток.
            tx0, ty0 = max(cx * per, rx0), max(cy * per, ry0)
            tx1, ty1 = min(cx * per + per - 1, rx1), min(cy * per + per - 1, ry1)
            w3857 = tile_bounds_3857(maxz, tx0, ty1)[0]
            s3857 = tile_bounds_3857(maxz, tx0, ty1)[1]
            e3857 = tile_bounds_3857(maxz, tx1, ty0)[2]
            n3857 = tile_bounds_3857(maxz, tx1, ty0)[3]
            ww, hh = (tx1 - tx0 + 1) * TILE, (ty1 - ty0 + 1) * TILE
            win_ll = (tile_bounds_lonlat(maxz, tx0, ty1)[0], tile_bounds_lonlat(maxz, tx0, ty1)[1],
                      tile_bounds_lonlat(maxz, tx1, ty0)[2], tile_bounds_lonlat(maxz, tx1, ty0)[3])

            rgb_refl = [np.full((hh, ww), np.nan, np.float32) for _ in range(3)]
            valid = np.zeros((hh, ww), bool)
            for qq in order:
                if valid.all():
                    break
                its = [it for it in items_for(qq) if bbox_intersects(it.bbox, win_ll)]
                if not its:
                    continue
                part_refl, part_ok = read_with_retry(its, w3857, s3857, e3857, n3857, ww, hh)
                got = fill_gaps(rgb_refl, valid, part_refl, part_ok)
                if got:
                    name = quarter_name(*qq) if qq[1] else qq[0]
                    if name not in used:
                        used.append(name)
                    years.add(qq[0] if qq[1] else int(its[0].id.split("_")[2][:4]))

            if not checked_scale and valid.sum() > 10000:
                # Самопроверка масштаба: у суши летом медиана красного
                # 0.02–0.10. Если вышло 0.1–0.2 — в данных есть смещение,
                # которого нет в документации (было бы видно как белёсый
                # снимок); если около нуля или минус — смещение лишнее.
                med = float(np.nanmedian(rgb_refl[0][valid]))
                print("  самопроверка: медиана отражения в красном %.3f %s" % (
                    med, "— нормально" if 0.005 <= med <= 0.12 else
                    "— ПОДОЗРИТЕЛЬНО, проверьте масштаб/смещение!"), flush=True)
                if med < 0.0 or med > 0.3:
                    # Явная чушь: дальше будут часы работы на чёрный
                    # или белый снимок. Лучше остановиться сразу.
                    raise SystemExit("масштаб чисел не тот (медиана %.3f) — сборка остановлена"
                                     % med)
                checked_scale = True

            # Полный кусок (4096×4096), в который вставлено прочитанное окно:
            # так уменьшение до 9-го ровно ложится на сетку плиток.
            full = np.zeros((per * TILE, per * TILE, 3), np.uint8)
            fvalid = np.zeros((per * TILE, per * TILE), bool)
            oy, ox = (ty0 - cy * per) * TILE, (tx0 - cx * per) * TILE
            full[oy:oy + hh, ox:ox + ww] = to_rgb8(*rgb_refl)
            fvalid[oy:oy + hh, ox:ox + ww] = valid
            full[~fvalid] = 0

            z, img, ok = maxz, full, fvalid
            while True:
                k = img.shape[0] // TILE
                x0b, y0b = cx * k, cy * k
                rr = tile_range(bbox, z)
                for j in range(k):
                    for i in range(k):
                        x, y = x0b + i, y0b + j
                        if not (rr[0] <= x <= rr[2] and rr[1] <= y <= rr[3]):
                            continue
                        m = ok[j * TILE:(j + 1) * TILE, i * TILE:(i + 1) * TILE]
                        if not m.any():
                            continue      # пусто — плитку не пишем
                        t = img[j * TILE:(j + 1) * TILE, i * TILE:(i + 1) * TILE]
                        mb.put(z, x, y, encode_jpeg(np.ascontiguousarray(t)))
                if z == chunk_z:
                    lowres[(cx, cy)] = (img, ok)
                    break
                img, ok = downsample2(img, ok)
                z -= 1

            el = time.time() - started
            print("  кусок %d из %d (%d/%d): плиток всего %d, %.0f мин, осталось ≈ %.0f мин"
                  % (n, len(chunks), cx, cy, mb.count, el / 60,
                     el / n * (len(chunks) - n) / 60), flush=True)

    # Уровни ниже куска — из уже готовых плиток куска, без нового чтения.
    level = lowres
    for z in range(chunk_z - 1, minz - 1, -1):
        parents = {}
        for (x, y) in level:
            parents.setdefault((x // 2, y // 2), None)
        nxt = {}
        rr = tile_range(bbox, z)
        for (px, py) in parents:
            big = np.zeros((2 * TILE, 2 * TILE, 3), np.uint8)
            bok = np.zeros((2 * TILE, 2 * TILE), bool)
            for dy in (0, 1):
                for dx in (0, 1):
                    child = level.get((2 * px + dx, 2 * py + dy))
                    if child is not None:
                        big[dy * TILE:(dy + 1) * TILE, dx * TILE:(dx + 1) * TILE] = child[0]
                        bok[dy * TILE:(dy + 1) * TILE, dx * TILE:(dx + 1) * TILE] = child[1]
            img, ok = downsample2(big, bok)
            nxt[(px, py)] = (img, ok)
            if ok.any() and rr[0] <= px <= rr[2] and rr[1] <= py <= rr[3]:
                mb.put(z, px, py, encode_jpeg(img))
        level = nxt

    if not used:
        mb.close(vacuum=False)
        os.remove(part)
        raise SystemExit("ни одной точки снимка не прочитано — файл не записан")
    mb.set_metadata(make_metadata(title, bbox, minz, maxz, used, years))
    total = mb.count
    mb.close()
    os.replace(part, out_path)
    print("готово: %s, плиток %d, %.1f МБ, кварталы: %s, %.0f мин"
          % (out_path, total, os.path.getsize(out_path) / 1048576, ", ".join(used),
             (time.time() - started) / 60), flush=True)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--key", required=True, help="ключ области из regions.json, например LENOBL")
    p.add_argument("--regions", default="regions.json")
    p.add_argument("--quarter", default="", help="квартал, например 2026-Q3; пусто — свежее лето")
    p.add_argument("--minzoom", type=int, default=6)
    p.add_argument("--maxzoom", type=int, default=13)
    p.add_argument("--out", default="", help="имя файла; по умолчанию TAIGA-<KEY>-satellite.mbtiles")
    p.add_argument("--count-only", action="store_true",
                   help="только посчитать плитки и предметы (ключи не нужны)")
    p.add_argument("--source", choices=("cdse", "element84-proba"), default="cdse")
    p.add_argument("--scene", default="", help="для пробы: id сцены Element84 L2A")
    p.add_argument("--bbox", default="", help="для пробы: запад,юг,восток,север вместо области")
    p.add_argument("--title", default="")
    args = p.parse_args(argv)
    if args.maxzoom - args.minzoom < 0 or args.maxzoom > 15:
        raise SystemExit("уровни странные")
    build(args)


if __name__ == "__main__":
    sys.exit(main())
