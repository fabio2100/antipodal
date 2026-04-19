import json
import pathlib
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

STEP = 1  # grados entre puntos de la grilla

# Colores: (es_tierra_punto, es_tierra_antipoda)
COLOR_AGUA_AGUA    = "#ADD8E6"  # blanco
COLOR_AGUA_TIERRA  = "#F7BE02"  # celeste claro
COLOR_TIERRA_AGUA  = "#006E00"  # marrón
COLOR_TIERRA_TIERRA = "#000000"  # negro


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


def generar_geojson_grilla(tierra_prep) -> dict:
    half = STEP / 2
    lats = list(range(-90, 91, STEP))
    lons = list(range(-180, 181, STEP))

    print(f"Calculando {len(lats) * len(lons)} puntos de la grilla...")
    is_land = {}
    for lat, lon in product(lats, lons):
        is_land[(lat, lon)] = tierra_prep.contains(Point(lon, lat))

    print("Asignando colores según antípodas...")
    features = []
    for lat, lon in product(lats, lons):
        a_lat, a_lon = antipodo(lat, lon)
        # Los antípodas de puntos en la grilla de 5° siempre caen en la grilla
        a_lat_r = round(a_lat / STEP) * STEP
        a_lon_r = round(a_lon / STEP) * STEP
        # Normalizar longitud al rango [-180, 180]
        if a_lon_r > 180:
            a_lon_r -= 360
        elif a_lon_r < -180:
            a_lon_r += 360

        tierra = is_land[(lat, lon)]
        anti_tierra = is_land.get(
            (a_lat_r, a_lon_r),
            tierra_prep.contains(Point(a_lon_r, a_lat_r)),
        )

        if not tierra and not anti_tierra:
            color = COLOR_AGUA_AGUA
        elif not tierra and anti_tierra:
            color = COLOR_AGUA_TIERRA
        elif tierra and not anti_tierra:
            color = COLOR_TIERRA_AGUA
        else:
            color = COLOR_TIERRA_TIERRA

        # Rectángulo centrado en (lat, lon), clipeado a los límites del mapa
        lat0 = max(-90.0, lat - half)
        lat1 = min(90.0, lat + half)
        lon0 = max(-180.0, lon - half)
        lon1 = min(180.0, lon + half)

        # GeoJSON usa [lon, lat]
        coords = [
            [lon0, lat0], [lon1, lat0], [lon1, lat1],
            [lon0, lat1], [lon0, lat0],
        ]
        features.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [coords]},
            "properties": {"color": color},
        })

    total = len(features)
    conteo = {
        COLOR_AGUA_AGUA:     0,
        COLOR_AGUA_TIERRA:   0,
        COLOR_TIERRA_AGUA:   0,
        COLOR_TIERRA_TIERRA: 0,
    }
    for f in features:
        conteo[f["properties"]["color"]] += 1

    nombres = {
        COLOR_AGUA_AGUA:     "Agua   / Agua   (blanco)",
        COLOR_AGUA_TIERRA:   "Agua   / Tierra (celeste)",
        COLOR_TIERRA_AGUA:   "Tierra / Agua   (marrón) ",
        COLOR_TIERRA_TIERRA: "Tierra / Tierra (negro)  ",
    }
    print(f"\n{'─'*45}")
    print(f"  Distribución de celdas ({total} total, paso {STEP}°)")
    print(f"{'─'*45}")
    for color, nombre in nombres.items():
        n = conteo[color]
        pct = n / total * 100
        print(f"  {nombre}: {n:>6}  ({pct:5.2f}%)")
    print(f"{'─'*45}\n")

    return {"type": "FeatureCollection", "features": features}


def crear_mapa(
    lat_centro: float = 20.0,
    lon_centro: float = 0.0,
    zoom_inicial: int = 3,
    archivo_salida: str = "mapa.html",
) -> folium.Map:
    land_geojson = obtener_land_geojson()
    tierra_prep = construir_geometria_tierra(land_geojson)
    grilla_geojson = generar_geojson_grilla(tierra_prep)

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
    folium.GeoJson(
        grilla_geojson,
        style_function=lambda f: {
            "fillColor": f["properties"]["color"],
            "color": f["properties"]["color"],
            "fillOpacity": 1.0,
            "weight": 0,
        },
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
        <span style="background:#8B4513;border:1px solid #999;display:inline-block;width:14px;height:14px;vertical-align:middle;"></span>&nbsp; Tierra &nbsp;/&nbsp; Agua<br>
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

