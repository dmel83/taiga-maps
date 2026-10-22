#!/usr/bin/env python3
"""Собрать спутниковый снимок области из Sentinel-2 в MBTiles.

ДОБАВЛЕНО 22.10.2026. Старый сборщик tools-sputnik.py (EOX 2017)
остаётся запасным, его не трогаем.

ИЗМЕНЕНО 22.10.2026 (вечер). РАЗБИРАЕМЫЙ ДЕФЕКТ: первая версия брала
готовые мозаики Copernicus Data Space (CDSE), а они отдаются только
с ключами личного кабинета. Из России кабинет CDSE не открывается даже
через VPN — завести ключи нельзя. Поэтому основной путь теперь без
всяких учётных записей: мы САМИ собираем безоблачное лето из открытых
снимков Sentinel-2 L2A в облаке Amazon (Element84). CDSE оставлен как
необязательный путь — если когда-нибудь ключи появятся.

ОСНОВНОЙ ИСТОЧНИК (по умолчанию, --source aws). Сверено 22.10.2026:
  каталог  https://earth-search.aws.element84.com/v1 , коллекция
           sentinel-2-c1-l2a («Sentinel-2 Collection 1 Level-2A»;
           если её вдруг нет — старая sentinel-2-l2a). Поиск — POST
           /search, следующая страница — ссылка next с методом POST
           и телом запроса (её и отправляем как есть). Расширение
           fields сервер молча игнорирует — приходят предметы целиком.
  файлы    https://e84-earth-search-sentinel-data.s3.us-west-2.
           amazonaws.com/sentinel-2-c1-l2a/36/V/UM/2026/7/<id>/B04.tif —
           открыто, без ключей (AWS_NO_SIGN_REQUEST). COG 10980×10980,
           блоки 1024, uint16, nodata 0; SCL — 20 м, 5490×5490.
           Один канал одного снимка ≈ 205 МБ (file:size в каталоге).
  масштаб  в raster:bands: scale 0.0001, offset −0.1. У Collection 1
           смещение в числах ЕСТЬ: медиана красного над лесом у Тосно
           1169 → (1169 − 1000)/10000 = 0.017 — как у леса и должно.
           А у старой sentinel-2-l2a стоит флаг
           earthsearch:boa_offset_applied = true — там смещение уже
           снято, и вычитать его второй раз нельзя (см. l2a_item).
  лицензия Copernicus Sentinel Data Legal Notice (ссылка license
           в описании коллекции): раздавать и изменять можно,
           подпись «Contains modified Copernicus Sentinel data <год>».

КАК СОБИРАЕТСЯ ЛЕТО (--source aws).
  1. Каталог: все снимки над рамкой области с 1 июня по 15 сентября
     последнего законченного лета, облачность < 80 %.
  2. Область идёт кусками (плитка 9-го приближения, 4096×4096 точек
     на 13-м). Для куска по каждому квадрату MGRS берутся ЛУЧШИЕ
     --scenes снимков (по доле ясного неба: облачность и пустые края
     полосы съёмки), из них читаются только окна над куском.
  3. Каждая точка: выкидываем облака, тени, снег и брак по SCL
     (SCL_НЕГОДНЫЕ), из оставшихся ясных наблюдений берём МЕДИАНУ
     отдельно по каналу. Медиана, а не среднее: недобитое облако или
     тень — это выброс, медиана его не замечает.
  4. Где за лето ясных наблюдений нет — то же по прошлому лету;
     где нет и там — просто лучший по облачности снимок как есть.
  5. Цвет — одна формула на всю Россию (stretch_channel), плитки
     13…9 из куска, 8…6 из 9-го в конце. JPEG 80, MBTiles 1.3, ряды
     TMS — ровно как ждёт приложение (region/SatPack, osmdroid):
     файл TAIGA-<КЛЮЧ>-satellite.mbtiles.

НЕОБЯЗАТЕЛЬНЫЙ ИСТОЧНИК (--source cdse). Готовые квартальные мозаики
CDSE Sentinel-2 Global Mosaics (S2MSI_L3__MCQ). Сверено 22.10.2026:
каталог https://stac.dataspace.copernicus.eu/v1, коллекция
sentinel-2-global-mosaics, datetime предмета — первый день квартала;
файлы s3://eodata/Global-Mosaics/Sentinel-2/S2MSI_L3__MCQ/ГГГГ/ММ/01/
<id>/B04.tif на https://eodata.dataspace.copernicus.eu — только
с ключами (без них 403). По документации CDSE (раздел «Sentinel-2
Level 3 Quarterly Mosaics») значения = отражение × 10000, смещения нет.
Пределы S3 CDSE: 4 соединения, 20 МБ/с на соединение.

ЗАПУСК.
  python3 tools-sputnik-sentinel.py --key LENOBL --count-only  # план
  python3 tools-sputnik-sentinel.py --key LENOBL               # сборка
  python3 tools-sputnik-sentinel.py --key TOSNO --title Тосно \
      --bbox 30.70,59.45,31.06,59.63                            # проба
  CDSE_S3_ACCESS_KEY=… CDSE_S3_SECRET_KEY=… python3 \
      tools-sputnik-sentinel.py --key LENOBL --source cdse [--quarter 2026-Q3]

Нужны numpy, Pillow и rasterio (в нём свой GDAL) — версии закреплены
в requirements-sputnik.txt. Чистые части (счёт плиток, медиана, маска,
цвет, латание, запись MBTiles) от rasterio не зависят и проверяются
тестами tests/test_sputnik_sentinel.py без сети.
"""

import argparse
import concurrent.futures
import datetime
import io
import json
import math
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import numpy as np

# ---------------------------------------------------------------- источники

EARTH_SEARCH = "https://earth-search.aws.element84.com/v1"
AWS_COLLECTIONS = ("sentinel-2-c1-l2a", "sentinel-2-l2a")   # по порядку предпочтения

CDSE_STAC = "https://stac.dataspace.copernicus.eu/v1"
CDSE_COLLECTION = "sentinel-2-global-mosaics"
CDSE_S3_ENDPOINT = "eodata.dataspace.copernicus.eu"

# Подпись по условиям Copernicus Sentinel Data Legal Notice. Год —
# год снимков; подставляется по фактически взятым.
ATTRIBUTION = "Contains modified Copernicus Sentinel data {years}"
USER_AGENT = "TAIGA-offline-maps/1.0 (+https://github.com/dmel83/taiga-maps)"

# ЛЕТО. С 1 июня по 15 сентября. Почему не весь третий квартал: во
# второй половине сентября на севере уже желтеет берёза и ложится
# первый снег в горах — медиана тянет в осень. Почему с июня, а не
# с июля: в Ленобласти и Карелии ясных дней мало, июнь добавляет
# наблюдений, а листва в июне уже полная.
ЛЕТО = ((6, 1), (9, 15))

# Сколько лучших снимков брать на квадрат MGRS. 5 — компромисс:
# медиана из 3–5 ясных наблюдений уже убирает облака, а каждая лишняя
# сцена — это ещё ~0.6 ГБ чтения на квадрат 110×110 км (см. план в
# --count-only). Если сборка не успевает в --budget-minutes, число
# уменьшается на ходу, но не ниже СЦЕН_МИНИМУМ.
СЦЕН_НА_КВАДРАТ = 5
СЦЕН_МИНИМУМ = 3
ПЕРЕБОР_БЛОКОВ = 3.0     # см. plan_aws
ОБЛАЧНОСТЬ_ДО = 80        # снимки облачнее этого в каталоге не берём

# SCL (Scene Classification L2A), которые выкидываем из медианы:
# 0 пусто, 1 брак, 3 тень облака, 7 неясно, 8–9 облака, 10 перистые
# (это ровно список, по которому CDSE строит свои мозаики), плюс
# 11 снег/лёд — летом снег у нас это почти всегда ошибка
# классификации на ярком облаке. Оставляем: 2 тёмное, 4 растительность,
# 5 голая земля, 6 вода.
SCL_НЕГОДНЫЕ = (0, 1, 3, 7, 8, 9, 10, 11)

# ЗАПАСНЫЕ КВАРТАЛЫ (только для --source cdse) — чем латать дыры
# главного квартала: то же лето год назад, потом весна того же года.
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
# Проверено на снимках у Тосно (лето 2026), см. описание PR.
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


def overlap_fraction(a, b):
    """Какая доля рамки b (в градусах) лежит внутри рамки a."""
    w = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    h = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    area = (b[2] - b[0]) * (b[3] - b[1])
    return w * h / area if area > 0 else 0.0


def count_tiles(bbox, minzoom, maxzoom):
    total = 0
    for z in range(minzoom, maxzoom + 1):
        x0, y0, x1, y1 = tile_range(bbox, z)
        total += (x1 - x0 + 1) * (y1 - y0 + 1)
    return total


# ---------------------------------------------------------------- время года

def last_summer(today=None):
    """Год последнего ЗАКОНЧЕННОГО лета (1 июня – 15 сентября)."""
    today = today or datetime.date.today()
    end_m, end_d = ЛЕТО[1]
    return today.year if (today.month, today.day) > (end_m, end_d) else today.year - 1


def summer_interval(year):
    (m0, d0), (m1, d1) = ЛЕТО
    return "%04d-%02d-%02dT00:00:00Z/%04d-%02d-%02dT23:59:59Z" % (year, m0, d0, year, m1, d1)


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


# ---------------------------------------------------------------- числа и маска

def dn_to_reflectance(dn, nodata, scale, offset):
    """Целые числа файла → отражение float32; nodata → NaN."""
    refl = dn.astype(np.float32) * np.float32(scale) + np.float32(offset)
    refl[dn == nodata] = np.nan
    return refl


def normalize_dn(dn, scale, offset):
    """Числа снимка → «отражение × 10000» в uint16; 0 — нет данных.

    Зачем: в одной медиане могут оказаться снимки с разным смещением
    (старая коллекция: часть со снятым, часть с неснятым). Приводим
    всех к одному виду ДО медианы. Отражение ≤ 0 (бывает над тёмной
    водой после поправки) → 1, чтобы не спутать с «нет данных».
    """
    v = dn.astype(np.float64) * (scale * 10000.0) + offset * 10000.0
    out = np.clip(np.round(v), 1, 65534).astype(np.uint16)
    out[dn == 0] = 0
    return out


def scl_clear(scl):
    """True там, где SCL говорит «ясно»."""
    return ~np.isin(scl, SCL_НЕГОДНЫЕ)


def masked_median(stack, clear):
    """Медиана по оси 0 только по ясным наблюдениям.

    stack — (n, H, W) uint16, clear — (n, H, W) bool.
    → (медиана float32, NaN где ясных нет; число ясных uint8).
    Без NaN и без nanmedian: негодные заменяем на 65535, сортируем,
    берём середину среди первых k — быстро и без лишней памяти.
    """
    k = clear.sum(axis=0)
    s = np.where(clear, stack, np.uint16(65535))
    s.sort(axis=0)
    lo = np.clip((k - 1) // 2, 0, None)[None]
    hi = np.clip(k // 2, 0, stack.shape[0] - 1)[None]
    a = np.take_along_axis(s, lo, axis=0)[0].astype(np.float32)
    b = np.take_along_axis(s, hi, axis=0)[0].astype(np.float32)
    med = (a + b) / 2.0
    med[k == 0] = np.nan
    return med, k.astype(np.uint8)


def composite(stacks, clear):
    """Безоблачное лето из стопки снимков одного окна.

    stacks — [R, G, B], каждый (n, H, W) uint16 «отражение × 10000»,
             0 — нет данных; снимки отсортированы от лучшего к худшему.
    clear  — (n, H, W) bool: ясно по SCL И есть данные во всех каналах.
    → (медиана [R,G,B] как отражение float32 c NaN, где ясно хоть раз;
       «лучший как есть» [R,G,B] отражение, где есть хоть какие данные).
    Медиана — отдельно по каналу (так делают и мозаики CDSE).
    """
    med = []
    for st in stacks:
        m, k = masked_median(st, clear)
        med.append(m / 10000.0)
    med_ok = k > 0
    has = (stacks[0] > 0) & (stacks[1] > 0) & (stacks[2] > 0)
    first = np.argmax(has, axis=0)[None]             # первый (лучший) с данными
    raw_ok = has.any(axis=0)
    raw = []
    for st in stacks:
        v = np.take_along_axis(st, first, axis=0)[0].astype(np.float32) / 10000.0
        v[~raw_ok] = np.nan
        raw.append(v)
    return med, med_ok, raw, raw_ok


def pick_scenes(items, window_ll, per_tile):
    """Лучшие снимки для окна: по per_tile на каждый квадрат MGRS.

    Лучший — у кого больше ясного неба: (1 − облачность) × (1 − пустой
    край полосы). Пустой край важен: снимок с краю полосы съёмки может
    быть «безоблачным», но покрывать треть квадрата.
    Один и тот же пролёт в одном квадрате берётся один раз.
    → список от лучшего к худшему.
    """
    groups = {}
    for it in items:
        if bbox_intersects(it.bbox, window_ll):
            groups.setdefault(it.grid, []).append(it)
    chosen = []
    for grid, its in groups.items():
        its.sort(key=lambda i: (-i.clear_share, i.id))
        seen = set()
        for it in its:
            if it.pass_key in seen:
                continue
            seen.add(it.pass_key)
            chosen.append(it)
            if len(seen) == per_tile:
                break
    chosen.sort(key=lambda i: (-i.clear_share, i.id))
    return chosen


# ---------------------------------------------------------------- цвет

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
    Уже заполненное НЕ перетирается: главный слой важнее запасных.
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


def make_metadata(title, bbox, minzoom, maxzoom, source_text, years):
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
        "description": (source_text
                        + " Единая цветовая формула ТАЙГИ: чёрное %s, белое %s, гамма %.1f."
                        % (ЧЁРНОЕ, БЕЛОЕ, ГАММА)),
    }


# ---------------------------------------------------------------- каталог

class Item:
    """Один предмет каталога: где лежат каналы и как их читать."""

    def __init__(self, item_id, bbox, hrefs, nodata, scale, offset, mask,
                 grid="", cloud=0.0, empty=0.0, when="", sizes=None):
        self.id = item_id
        self.bbox = bbox
        self.hrefs = hrefs        # {'r','g','b','mask'} → путь /vsis3/…
        self.nodata = nodata
        self.scale = scale
        self.offset = offset
        self.mask = mask          # 'observations' или 'scl'
        self.grid = grid          # квадрат MGRS
        self.cloud = cloud        # облачность, %
        self.empty = empty        # пустой край полосы, %
        self.when = when          # дата съёмки
        self.sizes = sizes or {}  # байт в файле канала
        self.clear_share = (1 - cloud / 100.0) * (1 - empty / 100.0)
        self.pass_key = when[:10]  # один день — один пролёт над квадратом

    @property
    def year(self):
        return int(self.when[:4])


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
                int(a["B04"].get("nodata", -32768)), scale, offset, "observations",
                when=f.get("properties", {}).get("datetime", ""),
                sizes={k: a[b].get("file:size", 0) for k, b in
                       (("r", "B04"), ("g", "B03"), ("b", "B02"), ("mask", "observations"))})


def l2a_item(f):
    """Предмет Element84 (sentinel-2-c1-l2a или sentinel-2-l2a)."""
    a, p = f["assets"], f["properties"]
    # РАЗБИРАЕМЫЙ ДЕФЕКТ (пойман пробой 22.10.2026). У L2A с базовой
    # линией ≥ 04.00 в числах сидит +1000, и Element84 пишет в
    # raster:bands offset −0.1. В Collection 1 так оно и есть. Но в
    # старой sentinel-2-l2a у тех же сцен стоит
    # earthsearch:boa_offset_applied = true: сдвиг они УЖЕ сняли сами.
    # Применить его второй раз — медиана леса уходит в −0.08, снимок
    # чёрный. Поэтому при этом флаге смещение — ноль.
    scale, offset = _scale_offset(a["red"], 1e-4, -0.1)
    if p.get("earthsearch:boa_offset_applied"):
        offset = 0.0
    return Item(f["id"], f["bbox"],
                {"r": s3_to_vsis3(a["red"]["href"]), "g": s3_to_vsis3(a["green"]["href"]),
                 "b": s3_to_vsis3(a["blue"]["href"]), "mask": s3_to_vsis3(a["scl"]["href"])},
                0, scale, offset, "scl",
                grid=p.get("grid:code", f["id"]),
                cloud=float(p.get("eo:cloud_cover", 100.0)),
                empty=float(p.get("s2:nodata_pixel_percentage", 0.0) or 0.0),
                when=p.get("datetime", ""),
                sizes={k: a[b].get("file:size", 0) for k, b in
                       (("r", "red"), ("g", "green"), ("b", "blue"), ("mask", "scl"))})


def http_json(url, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": USER_AGENT, "Content-Type": "application/json"})
    pause = 3.0
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                raise
            err = e
        except Exception as e:  # обрыв, тайм-аут — каталог иногда задумывается
            err = e
        if attempt == 5:
            raise SystemExit("каталог не отвечает: %s (%s)" % (url, err))
        print("  каталог: %s, повтор через %.0f с" % (err, pause), flush=True)
        time.sleep(pause)
        pause *= 2


def aws_search(bbox, year, collection):
    """Все летние снимки над рамкой из каталога Element84."""
    body = {"collections": [collection],
            "bbox": [round(v, 6) for v in bbox],
            "datetime": summer_interval(year),
            "query": {"eo:cloud_cover": {"lt": ОБЛАЧНОСТЬ_ДО}},
            "limit": 100}
    url, items, seen = EARTH_SEARCH + "/search", [], set()
    while url:
        # Сверено 22.10.2026: ссылка next у Element84 — POST с готовым
        # телом (там же метка страницы). Если когда-нибудь станет GET —
        # тела не будет, и мы просто идём по адресу.
        page = http_json(url, body)
        for f in page.get("features", []):
            if f["id"] not in seen:
                seen.add(f["id"])
                items.append(l2a_item(f))
        nxt = next((l for l in page.get("links", []) if l.get("rel") == "next"), None)
        if nxt is None or not page.get("features"):
            break
        url = nxt["href"]
        body = nxt.get("body") if nxt.get("method", "GET").upper() == "POST" else None
    return items


def cdse_search(bbox, year, q):
    """Все предметы квартала CDSE над рамкой. Листаем страницы по next.

    datetime у мозаики — первый день квартала, ищем ровно по нему.
    Сверено 22.10.2026: дата без времени («2026-07-01/2026-09-30») —
    ошибка 400.
    """
    tag = "_%d_Q%d_" % (year, q)
    url = CDSE_STAC + "/search?" + urllib.parse.urlencode({
        "collections": CDSE_COLLECTION,
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


def newest_cdse_summer(bbox, today=None):
    """Самое свежее лето (Q3) CDSE, у которого есть предметы над рамкой."""
    today = today or datetime.date.today()
    for year in range(today.year, 2019, -1):
        if year == today.year and today.month < 10:
            continue                         # лето ещё не кончилось
        if cdse_search(bbox, year, 3):
            return year, 3
    raise SystemExit("в каталоге CDSE нет ни одного летнего квартала над этой рамкой")


# ---------------------------------------------------------------- чтение

def gdal_options(source):
    common = {
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
        "GDAL_HTTP_MAX_RETRY": 8,
        "GDAL_HTTP_RETRY_DELAY": 4,
        "GDAL_HTTP_TIMEOUT": 120,
        "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
        "VSI_CACHE": "TRUE",
        "VSI_CACHE_SIZE": 256 * 1024 * 1024,
        "GDAL_CACHEMAX": 1024,
        "GDAL_HTTP_USERAGENT": USER_AGENT,
    }
    if source == "cdse":
        common.update({
            "AWS_S3_ENDPOINT": CDSE_S3_ENDPOINT,
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


def _warp(path, dst, nodata, transform, resampling):
    import rasterio
    from rasterio.warp import reproject
    with rasterio.open(path) as src:
        reproject(rasterio.band(src, 1), dst, src_nodata=nodata, dst_transform=transform,
                  dst_crs="EPSG:3857", dst_nodata=nodata, resampling=resampling)
    return dst


def read_scene(it, transform, width, height, options):
    """Один снимок L2A в окно EPSG:3857 → ([R,G,B] uint16 ×10000, ясно)."""
    import rasterio
    from rasterio.warp import Resampling
    with rasterio.Env(**options):          # у каждого потока своё окружение GDAL
        bands = []
        for key in ("r", "g", "b"):
            dn = _warp(it.hrefs[key], np.zeros((height, width), np.uint16), 0,
                       transform, Resampling.bilinear)
            bands.append(normalize_dn(dn, it.scale, it.offset))
        scl = _warp(it.hrefs["mask"], np.zeros((height, width), np.uint8), 0,
                    transform, Resampling.nearest)
    clear = scl_clear(scl) & (bands[0] > 0) & (bands[1] > 0) & (bands[2] > 0)
    return bands, clear


def read_aws_layer(scenes, window, options, threads):
    """Стопка снимков окна → composite()."""
    from rasterio.transform import from_bounds
    west, south, east, north, width, height = window
    transform = from_bounds(west, south, east, north, width, height)
    n = len(scenes)
    stacks = [np.zeros((n, height, width), np.uint16) for _ in range(3)]
    clear = np.zeros((n, height, width), bool)
    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as pool:
        # Повтор — на уровне одного снимка, а не всего куска: обрыв
        # одного файла не должен заставлять перечитывать гигабайты.
        futures = {pool.submit(with_retry, read_scene, it, transform, width, height,
                               options): i
                   for i, it in enumerate(scenes)}
        for fut in concurrent.futures.as_completed(futures):
            i = futures[fut]
            bands, ok = fut.result()
            for c in range(3):
                stacks[c][i] = bands[c]
            clear[i] = ok
    return composite(stacks, clear)


def read_cdse_quarter(items, window, options, threads=1):
    """Сшить мозаики CDSE в окно EPSG:3857. → ([R,G,B] отражение, годность)."""
    import rasterio
    from rasterio.transform import from_bounds
    from rasterio.warp import Resampling
    west, south, east, north, width, height = window
    transform = from_bounds(west, south, east, north, width, height)
    out = [np.full((height, width), np.nan, np.float32) for _ in range(3)]
    valid = np.zeros((height, width), bool)
    with rasterio.Env(**options):
        for it in items:
            bands = [_warp(it.hrefs[k], np.full((height, width), it.nodata, np.int16),
                           it.nodata, transform, Resampling.bilinear) for k in ("r", "g", "b")]
            m = _warp(it.hrefs["mask"], np.zeros((height, width), np.uint16), 0,
                      transform, Resampling.nearest)
            ok = m > 0
            for b in bands:
                ok &= b != it.nodata
            refl = [dn_to_reflectance(b, it.nodata, it.scale, it.offset) for b in bands]
            # Соседние UTM-квадраты перекрываются краями; кто первый
            # лёг — тот и остаётся, второй только латает пустое.
            fill_gaps(out, valid, refl, ok)
    return out, valid


def with_retry(fn, *a, attempts=6):
    """Вызов чтения с повторами.

    РАЗБИРАЕМЫЙ ДЕФЕКТ (пойман пробой 22.10.2026): посреди чтения
    сцены соединение оборвалось — «TIFFReadEncodedTile() failed»,
    и вся сборка упала. Сам GDAL повторяет только ответы 5xx/429,
    а оборванный посреди блок не повторяет. За часы работы на
    сотне кусков такое случится почти наверняка, поэтому кусок
    перечитывается целиком с нарастающей паузой.
    """
    pause = 10.0
    for attempt in range(attempts):
        try:
            return fn(*a)
        except Exception as e:   # rasterio.errors.* и обрывы curl
            text = str(e)
            if "403" in text or "Access Denied" in text or "AccessDenied" in text:
                raise SystemExit("S3 отвечает «нет доступа» (403). Для CDSE — ключи неверные "
                                 "или истекли; для Amazon — файл убрали из открытого "
                                 "доступа. (%s)" % text[:200])
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
            "Путь CDSE работает только с ключами S3 (кабинет "
            "https://eodata-s3keysmanager.dataspace.copernicus.eu/ ,\n"
            "секреты CDSE_S3_ACCESS_KEY и CDSE_S3_SECRET_KEY). Без ключей "
            "соберите обычным путём — без слова CDSE в заказе.")


class Chunks:
    """Разбиение области на куски и окна чтения."""

    def __init__(self, bbox, minz, maxz):
        self.bbox, self.minz, self.maxz = bbox, minz, maxz
        self.z = max(minz, maxz - 4)          # кусок — плитка 9-го при maxzoom 13
        self.per = 1 << (maxz - self.z)       # плиток maxzoom на сторону куска
        self.rr = tile_range(bbox, maxz)
        cx0, cy0, cx1, cy1 = tile_range(bbox, self.z)
        self.list = [(cx, cy) for cy in range(cy0, cy1 + 1) for cx in range(cx0, cx1 + 1)]

    def window(self, cx, cy):
        """Окно куска, урезанное по рамке до целых плиток.
        → (tx0, ty0, окно 3857 (w,s,e,n,ширина,высота), окно в градусах)"""
        rx0, ry0, rx1, ry1 = self.rr
        per, z = self.per, self.maxz
        tx0, ty0 = max(cx * per, rx0), max(cy * per, ry0)
        tx1, ty1 = min(cx * per + per - 1, rx1), min(cy * per + per - 1, ry1)
        w, s = tile_bounds_3857(z, tx0, ty1)[:2]
        e, n = tile_bounds_3857(z, tx1, ty0)[2:]
        ll = tile_bounds_lonlat(z, tx0, ty1)[:2] + tile_bounds_lonlat(z, tx1, ty0)[2:]
        return tx0, ty0, (w, s, e, n, (tx1 - tx0 + 1) * TILE, (ty1 - ty0 + 1) * TILE), ll


def gap_box(valid):
    """Рамка вокруг пустых точек: (ряд0, ряд1, кол0, кол1), концы не
    включены. None — дыр нет."""
    rows = np.flatnonzero(~valid.all(axis=1))
    if rows.size == 0:
        return None
    cols = np.flatnonzero(~valid.all(axis=0))
    return int(rows[0]), int(rows[-1]) + 1, int(cols[0]), int(cols[-1]) + 1


def sub_window(win, box):
    """Часть окна (w, s, e, n, ширина, высота) по рамке точек box."""
    w, s, e, n, width, height = win
    r0, r1, c0, c1 = box
    px, py = (e - w) / width, (n - s) / height
    return (w + c0 * px, n - r1 * py, w + c1 * px, n - r0 * py, c1 - c0, r1 - r0)


def merc_to_lonlat(x, y):
    lon = x / ПОЛОВИНА_МИРА * 180.0
    lat = math.degrees(2 * math.atan(math.exp(y / 6378137.0)) - math.pi / 2)
    return lon, lat


def window_lonlat(win):
    w, s = merc_to_lonlat(win[0], win[1])
    e, n = merc_to_lonlat(win[2], win[3])
    return w, s, e, n


def plan_aws(chunks, items, per_tile):
    """Сколько снимков и байт придётся прочитать (оценка сверху).

    Для каждого куска — выбранные снимки × доля квадрата под окном ×
    размер файлов, умноженные на ПЕРЕБОР_БЛОКОВ: читаются целые блоки
    1024×1024 (10 км), а кусок — 39 км, края блоков торчат за окно.
    Пробой куска 299/149 (22.10.2026): план без перебора — 0.73 ГБ,
    счётчик GDAL — около 2.1 ГБ (без повторов). Кусок лежит на стыке
    UTM-зон 35 и 36, окно в «чужой» зоне повёрнуто и захватывает
    больше блоков; плюс поля для билинейной выборки. Берём 3× с запасом.
    """
    total, scenes = 0.0, set()
    for cx, cy in chunks.list:
        _, _, _, ll = chunks.window(cx, cy)
        for it in pick_scenes(items, ll, per_tile):
            scenes.add(it.id)
            total += overlap_fraction(ll, it.bbox) * sum(it.sizes.values())
    return len(scenes), total * ПЕРЕБОР_БЛОКОВ


def build(args):
    if args.bbox:
        bbox = tuple(float(v) for v in args.bbox.split(","))
        title = args.title or args.key
    else:
        bbox, title = region_bbox(args.regions, args.key)
    if args.source == "cdse" and not args.count_only:
        require_credentials()          # падать сразу, а не через час
    minz, maxz = args.minzoom, args.maxzoom
    ch = Chunks(bbox, minz, maxz)
    print("%s: плиток %d (уровни %d–%d), кусков %d"
          % (title, count_tiles(bbox, minz, maxz), minz, maxz, len(ch.list)), flush=True)

    # --- каталог
    if args.source == "aws":
        year = args.year or last_summer()
        years_order = [year, year - 1]
        collection, cat = None, {}
        for coll in AWS_COLLECTIONS:
            try:
                found = aws_search(bbox, year, coll)
            except urllib.error.HTTPError as e:
                print("  коллекция %s недоступна (%s)" % (coll, e), flush=True)
                continue
            if found:
                collection, cat[year] = coll, found
                break
        if not collection:
            raise SystemExit("в каталоге Element84 нет летних снимков %d над областью" % year)
        print("  коллекция %s, лето %d (%s): снимков %d, квадратов MGRS %d"
              % (collection, year, summer_interval(year), len(cat[year]),
                 len({i.grid for i in cat[year]})), flush=True)

        def items_for(y):
            if y not in cat:
                cat[y] = aws_search(bbox, y, collection)
                print("  лето %d: снимков %d" % (y, len(cat[y])), flush=True)
            return cat[y]

        n_sc, nbytes = plan_aws(ch, cat[year], args.scenes)
        print("  план: по %d лучших снимков на квадрат, разных снимков %d, "
              "читать ≈ %.0f ГБ (лето %d; прошлое лето — только для дыр)"
              % (args.scenes, n_sc, nbytes / 1e9, year), flush=True)
        for speed in (50, 100, 200):
            print("    при %d МБ/с — %.0f мин чтения" % (speed, nbytes / 1e6 / speed / 60))
    else:
        wanted = parse_quarter(args.quarter) or newest_cdse_summer(bbox)
        order = quarter_order(*wanted)
        cache = {}

        def items_for_q(qq):
            if qq not in cache:
                cache[qq] = cdse_search(bbox, *qq)
                print("  квартал %s: %d предметов" % (quarter_name(*qq), len(cache[qq])),
                      flush=True)
            return cache[qq]
        main_items = items_for_q(order[0])
        if not main_items:
            raise SystemExit("в каталоге нет квартала %s над областью" % quarter_name(*order[0]))
        print("  кварталы по порядку: %s; главный — %d предметов"
              % (", ".join(quarter_name(*q) for q in order), len(main_items)), flush=True)
    if args.count_only:
        return

    import warnings
    import rasterio
    from rasterio.errors import NotGeoreferencedWarning
    # rasterio ворчит, что массив-приёмник «без привязки», хотя привязку
    # мы передаём явно (dst_transform). Проба у Тосно легла точно на
    # место — предупреждение пустое, в журнале оно только мешает.
    warnings.filterwarnings("ignore", category=NotGeoreferencedWarning)
    options = gdal_options(args.source)
    out_path = args.out or "TAIGA-%s-satellite.mbtiles" % args.key
    part = out_path + ".part"
    if os.path.exists(part):
        os.remove(part)
    mb = MBTiles(part)
    used, years = [], set()
    stats = {"медиана": 0, "медиана прошлого лета": 0, "один снимок": 0}
    lowres = {}                        # плитки куска для сборки 8–6
    per_tile = args.scenes
    started = time.time()
    checked_scale = False
    budget = args.budget_minutes * 60.0
    timing = {"чтение": 0.0, "плитки": 0.0}
    for n, (cx, cy) in enumerate(ch.list, 1):
        tx0, ty0, win, ll = ch.window(cx, cy)
        ww, hh = win[4], win[5]
        rgb_refl = [np.full((hh, ww), np.nan, np.float32) for _ in range(3)]
        valid = np.zeros((hh, ww), bool)

        if args.source == "aws":
            raws = []
            for i, y in enumerate(years_order):
                box = gap_box(valid)
                if box is None:
                    break
                # РАЗБИРАЕМЫЙ ДЕФЕКТ (пойман пробой куска 299/149
                # 22.10.2026): после медианы 2026 года дырявыми
                # остались 36 точек из 16 миллионов, а прошлое лето
                # читалось на весь кусок — лишние 0.6 ГБ ради горстки
                # точек. Теперь запасной слой читается только в рамке
                # вокруг дыр.
                r0, r1, c0, c1 = box
                sub = sub_window(win, box)
                scenes = pick_scenes(items_for(y), window_lonlat(sub), per_tile)
                if not scenes:
                    continue
                t0 = time.time()
                med, med_ok, raw, raw_ok = read_aws_layer(scenes, sub, options, args.threads)
                timing["чтение"] += time.time() - t0
                view = [a[r0:r1, c0:c1] for a in rgb_refl]
                got = fill_gaps(view, valid[r0:r1, c0:c1], med, med_ok)
                stats["медиана" if i == 0 else "медиана прошлого лета"] += got
                if got:
                    years.add(y)
                raws.append((y, box, raw, raw_ok))
            for y, (r0, r1, c0, c1), raw, raw_ok in raws:
                view = [a[r0:r1, c0:c1] for a in rgb_refl]
                got = fill_gaps(view, valid[r0:r1, c0:c1], raw, raw_ok)
                stats["один снимок"] += got
                if got:
                    years.add(y)
        else:
            for qq in order:
                if valid.all():
                    break
                its = [it for it in items_for_q(qq) if bbox_intersects(it.bbox, ll)]
                if not its:
                    continue
                part_refl, part_ok = with_retry(read_cdse_quarter, its, win, options)
                if fill_gaps(rgb_refl, valid, part_refl, part_ok):
                    if quarter_name(*qq) not in used:
                        used.append(quarter_name(*qq))
                    years.add(qq[0])

        if not checked_scale and valid.sum() > 10000:
            # Самопроверка масштаба: у суши летом медиана красного
            # 0.01–0.10. Если вышло 0.1–0.2 — в данных есть смещение,
            # которое мы не сняли (снимок белёсый); если около нуля или
            # минус — сняли лишнее (снимок чёрный).
            med = float(np.nanmedian(rgb_refl[0][valid]))
            print("  самопроверка: медиана отражения в красном %.3f %s" % (
                med, "— нормально" if 0.005 <= med <= 0.12 else
                "— ПОДОЗРИТЕЛЬНО, проверьте масштаб/смещение!"), flush=True)
            if med <= 0.0 or med > 0.3:
                # Явная чушь: дальше будут часы работы на чёрный
                # или белый снимок. Лучше остановиться сразу.
                raise SystemExit("масштаб чисел не тот (медиана %.3f) — сборка остановлена" % med)
            checked_scale = True

        t0 = time.time()
        # Полный кусок (4096×4096), в который вставлено прочитанное окно:
        # так уменьшение до куска ровно ложится на сетку плиток.
        per = ch.per
        full = np.zeros((per * TILE, per * TILE, 3), np.uint8)
        fvalid = np.zeros((per * TILE, per * TILE), bool)
        oy, ox = (ty0 - cy * per) * TILE, (tx0 - cx * per) * TILE
        full[oy:oy + hh, ox:ox + ww] = to_rgb8(*rgb_refl)
        fvalid[oy:oy + hh, ox:ox + ww] = valid
        full[~fvalid] = 0
        del rgb_refl

        z, img, ok = maxz, full, fvalid
        while True:
            k = img.shape[0] // TILE
            rr = tile_range(bbox, z)
            for j in range(k):
                for i in range(k):
                    x, y = cx * k + i, cy * k + j
                    if not (rr[0] <= x <= rr[2] and rr[1] <= y <= rr[3]):
                        continue
                    m = ok[j * TILE:(j + 1) * TILE, i * TILE:(i + 1) * TILE]
                    if not m.any():
                        continue      # пусто — плитку не пишем
                    t = img[j * TILE:(j + 1) * TILE, i * TILE:(i + 1) * TILE]
                    mb.put(z, x, y, encode_jpeg(np.ascontiguousarray(t)))
            if z == ch.z:
                lowres[(cx, cy)] = (img, ok)
                break
            img, ok = downsample2(img, ok)
            z -= 1

        timing["плитки"] += time.time() - t0
        el = time.time() - started
        eta = el / n * (len(ch.list) - n)
        print("  кусок %d из %d (%d/%d): снимков на квадрат %s, плиток %d, %.0f мин, "
              "осталось ≈ %.0f мин (всего чтение %.0f с, плитки %.0f с)"
              % (n, len(ch.list), cx, cy, per_tile if args.source == "aws" else "—",
                 mb.count, el / 60, eta / 60, timing["чтение"], timing["плитки"]), flush=True)
        # Не успеваем в срок задачи — берём меньше снимков на квадрат.
        # Лучше чуть облачнее, чем ничего: на 6-м часу GitHub снимает
        # задачу целиком, и файла не будет вовсе.
        if (args.source == "aws" and n >= 3 and el + eta > budget
                and per_tile > СЦЕН_МИНИМУМ):
            per_tile -= 1
            print("  НЕ УСПЕВАЕМ в %d мин — дальше по %d снимка на квадрат"
                  % (args.budget_minutes, per_tile), flush=True)

    # Уровни ниже куска — из уже готовых плиток куска, без нового чтения.
    level = lowres
    for z in range(ch.z - 1, minz - 1, -1):
        parents = {(x // 2, y // 2) for (x, y) in level}
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

    if not years:
        mb.close(vacuum=False)
        os.remove(part)
        raise SystemExit("ни одной точки снимка не прочитано — файл не записан")
    if args.source == "aws":
        source_text = ("Sentinel-2 L2A (Element84 %s, открытый доступ), медиана до %d ясных "
                       "снимков на квадрат MGRS, лето %s (%s); облака, тени и снег сняты по "
                       "SCL. Точек: %s." % (collection, args.scenes, year,
                                            summer_interval(year),
                                            ", ".join("%s %d" % kv for kv in stats.items())))
    else:
        source_text = ("Copernicus Sentinel-2 Global Mosaics (S2MSI_L3__MCQ, 10 м), квартал "
                       + ", затем ".join(used) + ".")
    mb.set_metadata(make_metadata(title, bbox, minz, maxz, source_text, years))
    total = mb.count
    mb.close()
    os.replace(part, out_path)
    print("готово: %s, плиток %d, %.1f МБ, годы %s, %.0f мин\n  %s"
          % (out_path, total, os.path.getsize(out_path) / 1048576, sorted(years),
             (time.time() - started) / 60, source_text), flush=True)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--key", required=True, help="ключ области из regions.json, например LENOBL")
    p.add_argument("--regions", default="regions.json")
    p.add_argument("--source", choices=("aws", "cdse"), default="aws",
                   help="aws — открытые снимки, без ключей (по умолчанию); cdse — мозаики CDSE")
    p.add_argument("--year", type=int, default=0, help="aws: год лета; 0 — последнее законченное")
    p.add_argument("--scenes", type=int, default=СЦЕН_НА_КВАДРАТ,
                   help="aws: лучших снимков на квадрат MGRS")
    p.add_argument("--threads", type=int, default=8, help="aws: снимков читать одновременно")
    p.add_argument("--budget-minutes", type=float, default=290,
                   help="aws: уложиться в столько минут (иначе меньше снимков)")
    p.add_argument("--quarter", default="", help="cdse: квартал, например 2026-Q3")
    p.add_argument("--minzoom", type=int, default=6)
    p.add_argument("--maxzoom", type=int, default=13)
    p.add_argument("--out", default="", help="имя файла; по умолчанию TAIGA-<KEY>-satellite.mbtiles")
    p.add_argument("--count-only", action="store_true",
                   help="только план: плитки, снимки, объём чтения (ключи не нужны)")
    p.add_argument("--bbox", default="", help="запад,юг,восток,север вместо области из regions.json")
    p.add_argument("--title", default="")
    args = p.parse_args(argv)
    if args.maxzoom < args.minzoom or args.maxzoom > 15 or args.scenes < 1:
        raise SystemExit("параметры странные")
    build(args)


if __name__ == "__main__":
    sys.exit(main())
