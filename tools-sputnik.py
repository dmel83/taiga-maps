#!/usr/bin/env python3
"""Собрать спутниковый снимок области в один файл MBTiles.

ДОБАВЛЕНО 04.10.2026 — проба спутника на Ленинградской области.
Разбор источника, лицензии и объёмов — в документах проекта
«спутниковые карты: что можно законно» и «спутник: проба на Ленобласти».

ИСТОЧНИК. Мозаика EOX Sentinel-2 cloudless за 2017 год, слой
s2cloudless-2017_3857 их WMTS. Лицензия CC BY 4.0 (сверено в описании
слоя 04.10.2026): брать, раздавать и показывать можно при подписи
источника. Годы 2018–2025 — CC BY-NC-SA, их здесь нет и быть не должно.

ВЕЖЛИВОСТЬ. Сервис EOX бесплатный и на массовую выгрузку не рассчитан.
Поэтому: запуск только после ответа EOX на письмо (cloudless@eox.at),
не быстрее --rate запросов в секунду, честное имя программы в запросе,
повторы с нарастающей паузой, а не долбёжка. Уже скачанные плитки при
повторном запуске не качаются второй раз.

ЧТО НА ВЫХОДЕ. Файл MBTiles 1.3: таблица metadata и таблица tiles,
ряды по схеме TMS (снизу вверх). Плитки JPEG как есть, без пережатия.
Приложение читает его osmdroid'ом (region/SatPack, ui/SatelliteLayer).

Только стандартная библиотека Python — на машине сборки ставить нечего.
"""

import argparse
import json
import math
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

LAYER = "s2cloudless-2017_3857"
URL = ("https://tiles.maps.eox.at/wmts/1.0.0/" + LAYER
       + "/default/g/{z}/{y}/{x}.jpg")
ATTRIBUTION = ("EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH "
               "(Contains modified Copernicus Sentinel data 2017)")
USER_AGENT = ("TAIGA-offline-maps/1.0 (+https://github.com/dmel83/taiga-maps; "
              "one-time download, see README)")
RETRIES = 6
TIMEOUT_S = 30
JPEG_START = b"\xff\xd8"


def tile_x(lon, zoom):
    return int(math.floor((lon + 180.0) / 360.0 * (1 << zoom)))


def tile_y(lat, zoom):
    rad = math.radians(lat)
    return int(math.floor((1.0 - math.log(math.tan(rad) + 1.0 / math.cos(rad)) / math.pi)
                          / 2.0 * (1 << zoom)))


def tiles_in(bbox, zoom):
    """Все плитки уровня, задевающие рамку (запад, юг, восток, север)."""
    west, south, east, north = bbox
    x0, x1 = tile_x(west, zoom), tile_x(east, zoom)
    y0, y1 = tile_y(north, zoom), tile_y(south, zoom)
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            yield x, y


def region_bbox(regions_file, key):
    with open(regions_file, encoding="utf-8") as f:
        regions = json.load(f)["regions"]
    for region in regions:
        if region["key"] == key:
            west, south, east, north = (float(v) for v in region["bbox"].split(","))
            return west, south, east, north, region["title"]
    raise SystemExit("нет области с ключом " + key)


def open_db(path, title, bbox, minzoom, maxzoom):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS metadata (name TEXT, value TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS tiles (zoom_level INTEGER, tile_column INTEGER,"
               " tile_row INTEGER, tile_data BLOB)")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS tile_index ON tiles"
               " (zoom_level, tile_column, tile_row)")
    db.execute("DELETE FROM metadata")
    west, south, east, north = bbox
    meta = {
        "name": "ТАЙГА — спутник: " + title,
        "format": "jpg",
        "type": "baselayer",
        "version": "1.3",
        "minzoom": str(minzoom),
        "maxzoom": str(maxzoom),
        "bounds": "%.6f,%.6f,%.6f,%.6f" % (west, south, east, north),
        "attribution": ATTRIBUTION,
        "description": "Sentinel-2 cloudless 2017 by EOX, CC BY 4.0",
    }
    db.executemany("INSERT INTO metadata (name, value) VALUES (?, ?)", meta.items())
    db.commit()
    return db


def have(db, z, x, y):
    row = (1 << z) - 1 - y
    return db.execute("SELECT 1 FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                      (z, x, row)).fetchone() is not None


def fetch(z, x, y):
    """Одна плитка. Повторы с паузой 2, 4, 8… секунд; None — плитки нет (404)."""
    url = URL.format(z=z, x=x, y=y)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    pause = 2.0
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as answer:
                data = answer.read()
            if not data.startswith(JPEG_START):
                raise ValueError("пришёл не JPEG")
            return data
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            reason = "HTTP %d" % e.code
        except Exception as e:  # обрыв, тайм-аут, мусор вместо картинки
            reason = str(e)
        print("  плитка %d/%d/%d: %s, повтор через %.0f с" % (z, x, y, reason, pause),
              flush=True)
        time.sleep(pause)
        pause *= 2
    raise SystemExit("плитка %d/%d/%d так и не скачалась — останавливаюсь" % (z, x, y))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--key", required=True, help="ключ области из regions.json, например LENOBL")
    parser.add_argument("--regions", default="regions.json")
    parser.add_argument("--minzoom", type=int, default=6)
    parser.add_argument("--maxzoom", type=int, default=13)
    parser.add_argument("--rate", type=float, default=3.0, help="запросов в секунду, не больше")
    parser.add_argument("--count-only", action="store_true",
                        help="только посчитать плитки, ничего не качать")
    args = parser.parse_args()

    west, south, east, north, title = region_bbox(args.regions, args.key)
    bbox = (west, south, east, north)
    plan = [(z, x, y) for z in range(args.minzoom, args.maxzoom + 1)
            for x, y in tiles_in(bbox, z)]
    print("%s: %d плиток, уровни %d–%d" % (title, len(plan), args.minzoom, args.maxzoom),
          flush=True)
    if args.count_only:
        return

    out = "TAIGA-%s-satellite.mbtiles" % args.key
    db = open_db(out, title, bbox, args.minzoom, args.maxzoom)
    gap = 1.0 / args.rate
    got = skipped = empty = 0
    started = time.time()
    for n, (z, x, y) in enumerate(plan, 1):
        if have(db, z, x, y):
            skipped += 1
            continue
        moment = time.time()
        data = fetch(z, x, y)
        if data is None:
            empty += 1
        else:
            db.execute("INSERT OR REPLACE INTO tiles VALUES (?, ?, ?, ?)",
                       (z, x, (1 << z) - 1 - y, sqlite3.Binary(data)))
            got += 1
        if n % 500 == 0:
            db.commit()
            print("  %d из %d, %.0f мин" % (n, len(plan), (time.time() - started) / 60),
                  flush=True)
        wait = gap - (time.time() - moment)
        if wait > 0:
            time.sleep(wait)
    db.commit()
    db.execute("VACUUM")
    db.close()
    print("готово: скачано %d, было раньше %d, нет на сервере %d; файл %s, %.1f МБ"
          % (got, skipped, empty, out, os.path.getsize(out) / 1048576), flush=True)


if __name__ == "__main__":
    sys.exit(main())
