import base64
import json
import math
import pathlib
import struct
import zlib
from itertools import product

import folium
import requests
from branca.element import MacroElement
from folium.plugins import MousePosition
from jinja2 import Template
from shapely.geometry import Point, shape
from shapely.ops import unary_union
from shapely.prepared import prep

LAND_GEOJSON_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector"
    "/master/geojson/ne_110m_land.geojson"
)
LAND_GEOJSON_CACHE = pathlib.Path("ne_110m_land.geojson")

# Límite estándar de Web Mercator (~85.05°): lat > esto va a infinito
_MAX_LAT_MERC = 85.051129

# Colores: (es_tierra_punto, es_tierra_antipoda)
COLOR_AGUA_AGUA     = "#ADD8E6"  # celeste claro
COLOR_AGUA_TIERRA   = "#F7BE02"  # amarillo
COLOR_TIERRA_AGUA   = "#006E00"  # verde
COLOR_TIERRA_TIERRA = "#000000"  # negro

# Equivalentes RGB para pintar la imagen PNG
_COLOR_RGB = {
    COLOR_AGUA_AGUA:     (173, 216, 230),
    COLOR_AGUA_TIERRA:   (247, 190,   2),
    COLOR_TIERRA_AGUA:   (  0, 110,   0),
    COLOR_TIERRA_TIERRA: (  0,   0,   0),
}


def _construir_png_b64(filas_rgb: list) -> str:
    """Convierte filas de píxeles RGB a un PNG incrustable como data-URL base64."""
    alto = len(filas_rgb)
    ancho = len(filas_rgb[0]) if alto else 0

    def _chunk(tag: bytes, datos: bytes) -> bytes:
        cuerpo = tag + datos
        return struct.pack(">I", len(datos)) + cuerpo + struct.pack(">I", zlib.crc32(cuerpo) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", ancho, alto, 8, 2, 0, 0, 0)
    raw = bytearray()
    for fila in filas_rgb:
        raw.append(0)  # byte de filtro PNG (ninguno)
        for r, g, b in fila:
            raw += bytes([r, g, b])

    png = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(bytes(raw), level=6))
        + _chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + base64.b64encode(png).decode()


class AntipodalClickHandler(MacroElement):
    """Coloca un marcador rojo en la antípoda del punto clickeado."""

    def __init__(self):
        super().__init__()
        self._name = "AntipodalClickHandler"
        self._template = Template("""
            {% macro script(this, kwargs) %}
            (function () {
                var theMap = {{ this._parent.get_name() }};
                var antipodalMarker = null;
                var clickedMarker = null;

                theMap.on('click', function (e) {
                    var lat = e.latlng.lat;
                    var lon = e.latlng.lng;

                    // Normalizar longitud al rango [-180, 180]
                    lon = ((lon + 180) % 360 + 360) % 360 - 180;

                    // Calcular antípoda
                    var antiLat = -lat;
                    var antiLon = lon >= 0 ? lon - 180 : lon + 180;

                    // Eliminar marcadores previos
                    if (antipodalMarker) { theMap.removeLayer(antipodalMarker); }
                    if (clickedMarker)   { theMap.removeLayer(clickedMarker); }

                    // Marcador azul en el punto clickeado
                    clickedMarker = L.circleMarker([lat, lon], {
                        radius: 8,
                        color: '#0055ff',
                        fillColor: '#0055ff',
                        fillOpacity: 0.85,
                        weight: 2
                    }).bindTooltip('Punto: ' + lat.toFixed(4) + ', ' + lon.toFixed(4))
                      .addTo(theMap);

                    // Marcador rojo en la antípoda
                    antipodalMarker = L.circleMarker([antiLat, antiLon], {
                        radius: 8,
                        color: '#cc0000',
                        fillColor: '#ff0000',
                        fillOpacity: 0.85,
                        weight: 2
                    }).bindTooltip('Antípoda: ' + antiLat.toFixed(4) + ', ' + antiLon.toFixed(4))
                      .bindPopup(
                          '<b>Antípoda</b><br>Lat: ' + antiLat.toFixed(4) +
                          '<br>Lon: ' + antiLon.toFixed(4)
                      ).addTo(theMap)
                      .openPopup();
                });
            })();
            {% endmacro %}
        """)


def obtener_land_geojson() -> dict:
    if LAND_GEOJSON_CACHE.exists():
        return json.loads(LAND_GEOJSON_CACHE.read_text(encoding="utf-8"))
    print("Descargando datos de tierra (Natural Earth)...")
    respuesta = requests.get(LAND_GEOJSON_URL, timeout=30)
    respuesta.raise_for_status()
    LAND_GEOJSON_CACHE.write_text(respuesta.text, encoding="utf-8")
    print("Datos guardados en caché.")
    return respuesta.json()


def construir_geometria_tierra(geojson: dict):
    geometrias = [shape(f["geometry"]) for f in geojson["features"]]
    return prep(unary_union(geometrias))


def antipodo(lat: float, lon: float) -> tuple:
    anti_lat = -lat
    anti_lon = lon + 180 if lon <= 0 else lon - 180
    return anti_lat, anti_lon


def generar_imagen_grilla(tierra_prep) -> str:
    """Genera imagen PNG en proyección Mercator (720×720 px, 0.5°/px en longitud)."""
    W, H = 720, 720  # cuadrada en espacio Mercator → proporciones correctas en el mapa

    # 1. Precalcular is_land en grilla 0.5° (índices enteros para evitar floats como clave)
    lons_h = list(range(-360, 360))     # 720 celdas de longitud
    lats_h = list(range(179, -181, -1)) # 360 celdas de latitud (norte→sur)

    total_celdas = len(lats_h) * len(lons_h)
    print(f"Calculando {total_celdas} celdas de tierra/agua (0.5°)...")
    is_land = {}
    for lat_h, lon_h in product(lats_h, lons_h):
        is_land[(lat_h, lon_h)] = tierra_prep.contains(
            Point(lon_h * 0.5 + 0.25, lat_h * 0.5 + 0.25)
        )

    # 2. Generar imagen en espacio Mercator:
    #    cada fila = igual distancia de y-Mercator, así la imagen no se distorsiona
    #    al superponerla con ImageOverlay en Leaflet (que también usa Mercator).
    print(f"Generando imagen PNG Mercator {W}×{H} px...")
    y_max = math.log(math.tan(math.pi / 4 + math.radians(_MAX_LAT_MERC) / 2))

    conteo = {c: 0 for c in _COLOR_RGB}
    filas_rgb = []
    for row in range(H):
        # y-Mercator de y_max (norte) a -y_max (sur), lineal en píxeles
        y_merc = y_max * (1 - 2 * row / H)
        lat_real = math.degrees(2 * math.atan(math.exp(y_merc)) - math.pi / 2)
        lat_h = min(179, max(-180, math.floor(lat_real * 2)))
        a_lat_h = -lat_h - 1  # antípoda: -(lat_h*0.5+0.25) → celda a_lat_h

        fila = []
        for col in range(W):
            lon_h = col - 360  # col 0 → lon_h=-360 (centro -179.75°) … col 719 → 359 (179.75°)
            a_lon_h = lon_h - 360 if lon_h >= 0 else lon_h + 360

            tierra      = is_land.get((lat_h, lon_h), False)
            anti_tierra = is_land.get((a_lat_h, a_lon_h), False)

            if not tierra and not anti_tierra:
                color = COLOR_AGUA_AGUA
            elif not tierra and anti_tierra:
                color = COLOR_AGUA_TIERRA
            elif tierra and not anti_tierra:
                color = COLOR_TIERRA_AGUA
            else:
                color = COLOR_TIERRA_TIERRA

            conteo[color] += 1
            fila.append(_COLOR_RGB[color])
        filas_rgb.append(fila)

    total = W * H
    nombres = {
        COLOR_AGUA_AGUA:     "Agua   / Agua",
        COLOR_AGUA_TIERRA:   "Agua   / Tierra",
        COLOR_TIERRA_AGUA:   "Tierra / Agua",
        COLOR_TIERRA_TIERRA: "Tierra / Tierra",
    }
    print(f"\n{'─'*45}")
    print(f"  Distribución de píxeles ({total} total, Mercator {W}×{H})")
    print(f"{'─'*45}")
    for color, nombre in nombres.items():
        n = conteo[color]
        print(f"  {nombre}: {n:>6}  ({n/total*100:5.2f}%)")
    print(f"{'─'*45}\n")

    return _construir_png_b64(filas_rgb)


def crear_mapa(
    lat_centro: float = 20.0,
    lon_centro: float = 0.0,
    zoom_inicial: int = 3,
    archivo_salida: str = "mapa.html",
) -> folium.Map:
    land_geojson = obtener_land_geojson()
    tierra_prep = construir_geometria_tierra(land_geojson)
    imagen_b64 = generar_imagen_grilla(tierra_prep)

    mapa = folium.Map(
        location=[lat_centro, lon_centro],
        zoom_start=zoom_inicial,
        tiles=None,
    )

    # Fondo blanco (agua sin antípoda de tierra)
    mapa.get_root().html.add_child(
        folium.Element("<style>.leaflet-container { background: #ffffff !important; }</style>")
    )

    # Capa coloreada según combinación tierra/agua en punto y antípoda
    folium.raster_layers.ImageOverlay(
        image=imagen_b64,
        bounds=[[-_MAX_LAT_MERC, -180], [_MAX_LAT_MERC, 180]],
        opacity=1.0,
        zindex=1,
    ).add_to(mapa)

    # Leyenda
    leyenda_html = """
    <div style="position:fixed;bottom:40px;right:10px;z-index:1000;
                background:white;padding:10px 14px;border:1px solid #aaa;
                font-size:13px;font-family:sans-serif;border-radius:6px;
                box-shadow:2px 2px 6px rgba(0,0,0,.3);">
        <b>Punto &nbsp;/&nbsp; Antípoda</b><br><br>
        <span style="background:#FFFFFF;border:1px solid #999;display:inline-block;width:14px;height:14px;vertical-align:middle;"></span>&nbsp; Agua &nbsp;/&nbsp; Agua<br>
        <span style="background:#ADD8E6;border:1px solid #999;display:inline-block;width:14px;height:14px;vertical-align:middle;"></span>&nbsp; Agua &nbsp;/&nbsp; Tierra<br>
        <span style="background:#006E00;border:1px solid #999;display:inline-block;width:14px;height:14px;vertical-align:middle;"></span>&nbsp; Tierra &nbsp;/&nbsp; Agua<br>
        <span style="background:#000000;border:1px solid #999;display:inline-block;width:14px;height:14px;vertical-align:middle;"></span>&nbsp; Tierra &nbsp;/&nbsp; Tierra<br>
    </div>
    """
    mapa.get_root().html.add_child(folium.Element(leyenda_html))

    # Coordenadas en tiempo real
    MousePosition(
        position="bottomleft",
        separator=" | Lon: ",
        prefix="Lat: ",
        lat_formatter="function(num) {return L.Util.formatNum(num, 6);}",
        lng_formatter="function(num) {return L.Util.formatNum(num, 6);}",
    ).add_to(mapa)

    # Marcador rojo en la antípoda al hacer clic
    AntipodalClickHandler().add_to(mapa)

    mapa.save(archivo_salida)
    print(f"Mapa guardado en: {archivo_salida}")
    return mapa


def agregar_punto(
    mapa: folium.Map,
    lat: float,
    lon: float,
    nombre: str = "",
) -> folium.Map:
    folium.Marker(
        location=[lat, lon],
        popup=folium.Popup(f"<b>{nombre}</b><br>Lat: {lat}<br>Lon: {lon}", max_width=200),
        tooltip=nombre or f"({lat}, {lon})",
        icon=folium.Icon(color="red", icon="info-sign"),
    ).add_to(mapa)
    return mapa


def main():
    crear_mapa(
        lat_centro=20.0,
        lon_centro=0.0,
        zoom_inicial=3,
        archivo_salida="mapa.html",
    )
    print("Abre 'mapa.html' en tu navegador.")


if __name__ == "__main__":
    main()

