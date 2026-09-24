#!/usr/bin/env python3
"""Avisa cuando se habilita una carrera en la Matrícula En Línea de la UTN.

La pantalla de login del sistema de matrículas muestra públicamente la tabla
"Carreras Habilitadas Actualmente" (un Interactive Report de Oracle APEX).
Este script la lee completa, la compara con el último estado guardado y
notifica los cambios al celular mediante ntfy (https://ntfy.sh). Cada cambio
queda registrado en historial.csv, junto al archivo de estado.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import astuple, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

URL_MATRICULA = (
    "https://cloud3.utn.edu.ec/ords/r/producci_n/"
    "jhsc42xld3flutexoqzq025366/y4eipevfjtvooejhnoyt026136"
)
ECUADOR = timezone(timedelta(hours=-5))  # Ecuador no usa horario de verano.
USER_AGENT = "Mozilla/5.0 (compatible; notificacion-matriculas/1.0)"
TIMEOUT = 30
UMBRAL_FALLOS = 3  # ejecuciones fallidas seguidas antes de avisar que algo anda mal
MAX_PAGINAS = 20
HORA_RESUMEN = 9  # el resumen semanal se envía a partir de esta hora (Ecuador)
LIMITE_NTFY = 4000  # bytes; ntfy no muestra como texto los mensajes de más de 4096
VENTANA_DUPLICADOS = "10m"  # un aviso idéntico enviado en este lapso no se repite
CAMPOS_HISTORIAL = ("detectado", "evento", "facultad", "carrera", "modalidad")


class ErrorPagina(Exception):
    """La página no respondió como se esperaba (caída o cambio de estructura)."""


class ErrorNotificacion(Exception):
    """No se pudo entregar un mensaje por algún canal."""


@dataclass(frozen=True, order=True)
class Carrera:
    facultad: str
    carrera: str
    modalidad: str = ""
    estado: str = ""

    def describir(self) -> str:
        # Las carreras habilitadas tienen estado "A"; solo se muestra si es otro.
        partes = [self.carrera, self.modalidad, self.estado not in ("", "A") and f"estado {self.estado}"]
        return "• " + " · ".join(p for p in partes if p)


@dataclass
class Mensaje:
    titulo: str
    texto: str
    prioridad: int = 3  # escala de ntfy: 3 normal, 4 alta, 5 urgente


def normalizar(texto: str) -> str:
    """Minúsculas, sin tildes y con espacios simples, para comparar nombres."""
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c)
    )
    return " ".join(sin_tildes.casefold().split())


# --- Lectura de la página -------------------------------------------------


def leer_tabla(soup: BeautifulSoup) -> list[Carrera]:
    """Extrae las filas del Interactive Report, ubicando las columnas por su encabezado."""
    tabla = soup.find("table", class_="a-IRR-table")
    if tabla is None:
        # Sin tabla, APEX muestra un mensaje cuando no hay filas para mostrar.
        if soup.find(class_=["a-IRR-noDataMsg", "a-IRR-message"]):
            return []
        raise ErrorPagina("La página cambió: no se encontró la tabla de carreras")

    columnas = {th.get("id"): normalizar(th.get_text()) for th in tabla.find_all("th")}
    if not {"facultad", "carrera"} <= set(columnas.values()):
        raise ErrorPagina(f"La tabla cambió de columnas: {sorted(columnas.values())}")

    carreras = []
    for fila in tabla.find_all("tr"):
        # BeautifulSoup entrega el atributo "headers" como lista de ids.
        celdas = {
            columnas.get(" ".join(td.get("headers") or [])): td.get_text(" ", strip=True)
            for td in fila.find_all("td")
        }
        if celdas:
            carreras.append(
                Carrera(
                    facultad=celdas.get("facultad", ""),
                    carrera=celdas.get("carrera", ""),
                    modalidad=celdas.get("modalidad", ""),
                    estado=celdas.get("estado", ""),
                )
            )
    return carreras


def _valor(soup: BeautifulSoup, id_: str) -> str:
    nodo = soup.find(id=id_)
    if nodo is None or not nodo.get("value"):
        raise ErrorPagina(f"La página cambió: falta el campo {id_}")
    return nodo["value"]


def _config_reporte(soup: BeautifulSoup) -> dict:
    """Lee la configuración con la que APEX inicializa el reporte desde JavaScript."""
    for script in soup.find_all("script"):
        codigo = script.string or ""
        inicio = codigo.find(".interactiveReport(")
        if inicio == -1:
            continue
        try:
            config, _ = json.JSONDecoder().raw_decode(codigo, codigo.index("{", inicio))
        except ValueError as error:
            raise ErrorPagina(f"No se pudo leer la configuración del reporte: {error}") from None
        if not {"regionId", "ajaxIdentifier"} <= config.keys():
            raise ErrorPagina("La configuración del reporte cambió")
        return config
    raise ErrorPagina("La página cambió: no se encontró el reporte de carreras")


def obtener_carreras(url: str, sesion: requests.Session | None = None) -> list[Carrera]:
    """Descarga la lista completa de carreras habilitadas, recorriendo todas sus páginas."""
    sesion = sesion or requests.Session()
    sesion.headers["User-Agent"] = USER_AGENT

    respuesta = sesion.get(url, timeout=TIMEOUT)
    respuesta.raise_for_status()
    soup = BeautifulSoup(respuesta.text, "html.parser")
    carreras = leer_tabla(soup)

    config = _config_reporte(soup)
    region = config["regionId"]
    filas_por_pagina = int(_valor(soup, f"{region}_row_select"))
    base = soup.find("base")
    url_ajax = urljoin(urljoin(url, base["href"] if base else "/ords/"), "wwv_flow.ajax")
    datos = {
        "p_flow_id": _valor(soup, "pFlowId"),
        "p_flow_step_id": _valor(soup, "pFlowStepId"),
        "p_instance": _valor(soup, "pInstance"),
        "p_request": f"PLUGIN={config['ajaxIdentifier']}",
        "p_widget_name": "worksheet",
        "p_widget_mod": "ACTION",
        "p_widget_action": "PAGE",
        "x01": _valor(soup, f"{region}_worksheet_id"),
        "x02": _valor(soup, f"{region}_report_id"),
    }

    # Una página llena puede significar que hay más: se pide la siguiente
    # igual que lo hace el botón "siguiente" del reporte.
    pagina = carreras
    for _ in range(MAX_PAGINAS):
        if len(pagina) < filas_por_pagina:
            return carreras
        rango = f"pgR_min_row={len(carreras) + 1}max_rows={filas_por_pagina}rows_fetched={filas_por_pagina}"
        respuesta = sesion.post(url_ajax, data={**datos, "p_widget_action_mod": rango}, timeout=TIMEOUT)
        respuesta.raise_for_status()
        soup_pagina = BeautifulSoup(respuesta.text, "html.parser")
        if soup_pagina.find(id=f"{region}_content") is None:
            raise ErrorPagina("Respuesta inesperada al pedir la siguiente página del reporte")
        pagina = leer_tabla(soup_pagina)
        carreras += pagina
    raise ErrorPagina(f"El reporte tiene más de {MAX_PAGINAS} páginas")


def con_reintentos(funcion: Callable[[], list[Carrera]], intentos: int = 3, espera: int = 20) -> list[Carrera]:
    for intento in range(1, intentos + 1):
        try:
            return funcion()
        except (requests.RequestException, ErrorPagina) as error:
            if intento == intentos:
                raise
            print(f"Intento {intento} falló ({error}); reintentando en {espera} s", file=sys.stderr)
            time.sleep(espera)
    raise AssertionError("inalcanzable")


# --- Decisión de qué notificar --------------------------------------------


def _a_filas(carreras: Iterable[Carrera]) -> list[list[str]]:
    return [list(astuple(c)) for c in sorted(carreras)]


def _a_carreras(filas: list[list[str]]) -> set[Carrera]:
    return {Carrera(*fila) for fila in filas}


def _resumen_facultades(carreras: list[Carrera]) -> str:
    conteo = Counter(c.facultad for c in carreras)
    return ", ".join(f"{facultad}: {n}" for facultad, n in sorted(conteo.items())) or "ninguna"


def _lista(carreras: Iterable[Carrera]) -> str:
    return "\n".join(c.describir() for c in sorted(carreras))


def listar_por_facultad(carreras: Iterable[Carrera]) -> str:
    grupos: dict[str, list[Carrera]] = {}
    for carrera in sorted(carreras):
        grupos.setdefault(carrera.facultad, []).append(carrera)
    return "\n\n".join(f"{facultad} ({len(lista)})\n{_lista(lista)}" for facultad, lista in grupos.items())


def _con_habilitadas(texto: str, carreras: list[Carrera]) -> str:
    """Agrega la lista completa por facultad, o solo el conteo si no cabe en la notificación."""
    if not carreras:
        detalle = "Por ahora no hay carreras habilitadas."
    else:
        detalle = f"Habilitadas ahora ({len(carreras)}):\n\n{listar_por_facultad(carreras)}"
        if len(f"{texto}\n\n{detalle}".encode()) > LIMITE_NTFY:
            detalle = f"Habilitadas ahora: {len(carreras)} ({_resumen_facultades(carreras)})"
    return f"{texto}\n\n{detalle}" if texto else detalle


def _aviso_apertura(nombre: str, carreras: set[Carrera], url: str) -> Mensaje:
    return Mensaje(
        f"🎉 ¡{nombre} ya está habilitada!",
        f"Ya aparece en las carreras habilitadas para matrícula:\n{_lista(carreras)}\n\nIngresa aquí: {url}",
        prioridad=5,
    )


def _aviso_cambios(nuevas: set[Carrera], retiradas: set[Carrera], carreras: list[Carrera], facultad: str) -> Mensaje:
    facultades = sorted({c.facultad for c in nuevas | retiradas})
    partes = []
    if nuevas:
        partes.append(f"Nuevas:\n{listar_por_facultad(nuevas)}")
    if retiradas:
        partes.append(f"Ya no aparecen:\n{listar_por_facultad(retiradas)}")
    titulo = "📋 Cambios en carreras habilitadas" if retiradas else "📋 Nuevas carreras habilitadas"
    # Un cambio en la facultad vigilada suele anunciar la apertura: prioridad alta.
    prioridad = 4 if any(normalizar(f) == normalizar(facultad) for f in facultades) else 3
    return Mensaje(f"{titulo}: {', '.join(facultades)}", _con_habilitadas("\n\n".join(partes), carreras), prioridad)


def evaluar(
    estado: dict,
    carreras: list[Carrera],
    facultad: str,
    carrera: str,
    ahora: datetime,
    url: str,
) -> tuple[dict, list[Mensaje]]:
    """Compara la lista actual con el estado anterior y decide qué notificar."""
    actuales = set(carreras)
    objetivo = {
        c
        for c in actuales
        if normalizar(c.facultad) == normalizar(facultad) and normalizar(carrera) in normalizar(c.carrera)
    }
    nombre = f"{carrera} ({facultad})"
    semana = ahora.strftime("%G-W%V")
    semana_resumen = estado.get("semana_resumen", semana)
    mensajes = []

    if estado.get("alerta_error_enviada"):
        mensajes.append(Mensaje("✅ El monitor volvió a funcionar", "La página de matrículas responde de nuevo."))

    if "objetivo" not in estado:  # primera ejecución
        if objetivo:
            mensajes.append(_aviso_apertura(nombre, objetivo, url))
        else:
            mensajes.append(
                Mensaje(
                    "✅ Monitor de matrículas activado",
                    _con_habilitadas(f"{nombre} aún no está habilitada. Te avisaré apenas aparezca.", carreras),
                )
            )
    else:
        antes_objetivo = _a_carreras(estado["objetivo"])
        abrio = bool(objetivo) and not antes_objetivo
        cerro = bool(antes_objetivo) and not objetivo
        if abrio:
            mensajes.append(_aviso_apertura(nombre, objetivo, url))
        if cerro:
            mensajes.append(
                Mensaje(
                    f"{nombre} ya no aparece",
                    f"{nombre} salió de la lista de carreras habilitadas; puede que la matrícula se haya cerrado.\n{url}",
                    prioridad=4,
                )
            )

        if "habilitadas" in estado:
            antes = _a_carreras(estado["habilitadas"])
            # Lo que ya anunció el aviso de apertura o de cierre no se repite.
            nuevas = actuales - antes - (objetivo if abrio else set())
            retiradas = antes - actuales - (antes_objetivo if cerro else set())
            if nuevas or retiradas:
                mensajes.append(_aviso_cambios(nuevas, retiradas, carreras, facultad))
        else:
            # El estado viene de una versión anterior que no guardaba la lista completa.
            mensajes.append(Mensaje("📋 Carreras habilitadas", _con_habilitadas("", carreras)))

        # Resumen semanal: confirma que el monitor sigue vivo (y su commit evita
        # que GitHub pause el cron de un repositorio público por inactividad).
        if semana != semana_resumen and ahora.hour >= HORA_RESUMEN:
            estado_objetivo = "ya está habilitada ✅" if objetivo else "aún no está habilitada"
            mensajes.append(
                Mensaje("🔎 Sigo vigilando las matrículas", _con_habilitadas(f"{nombre} {estado_objetivo}.", carreras))
            )
            semana_resumen = semana

    nuevo_estado = {
        "objetivo": _a_filas(objetivo),
        "habilitadas": _a_filas(actuales),
        "semana_resumen": semana_resumen,
        "fallos_consecutivos": 0,
        "alerta_error_enviada": False,
    }
    return nuevo_estado, mensajes


def registrar_fallo(estado: dict, error: str, url: str) -> tuple[dict, list[Mensaje]]:
    """Cuenta fallos seguidos y avisa una sola vez cuando superan el umbral."""
    # El contador se detiene en el umbral para no reescribir el estado en cada ejecución.
    fallos = min(estado.get("fallos_consecutivos", 0) + 1, UMBRAL_FALLOS)
    nuevo_estado = {**estado, "fallos_consecutivos": fallos}
    mensajes = []
    if fallos >= UMBRAL_FALLOS and not estado.get("alerta_error_enviada"):
        mensajes.append(
            Mensaje(
                "⚠️ El monitor de matrículas no puede leer la página",
                f"Falló {fallos} veces seguidas. Último error: {error}\n\n"
                f"Mientras tanto, revisa la página a mano: {url}",
                prioridad=4,
            )
        )
        nuevo_estado["alerta_error_enviada"] = True
    return nuevo_estado, mensajes


# --- Notificaciones -------------------------------------------------------


def _texto_ntfy(mensaje: Mensaje) -> str:
    if len(mensaje.texto.encode()) <= LIMITE_NTFY:
        return mensaje.texto
    return mensaje.texto.encode()[: LIMITE_NTFY - 10].decode(errors="ignore") + "\n…"


def enviar_ntfy(mensaje: Mensaje, servidor: str, topic: str, enlace: str) -> None:
    try:
        respuesta = requests.post(
            servidor.rstrip("/") + "/",
            json={
                "topic": topic,
                "title": mensaje.titulo,
                "message": _texto_ntfy(mensaje),
                "priority": mensaje.prioridad,
                "click": enlace,
            },
            timeout=TIMEOUT,
        )
    except requests.RequestException as error:
        raise ErrorNotificacion(f"ntfy: {type(error).__name__}") from None
    if not respuesta.ok:
        raise ErrorNotificacion(f"ntfy respondió {respuesta.status_code}: {respuesta.text[:200]}")


def publicados_recientes(servidor: str, topic: str) -> set[tuple[str, str]]:
    """Avisos que llegaron al canal hace poco, quizá enviados por otro monitor (GitHub o la Mac)."""
    try:
        respuesta = requests.get(
            f"{servidor.rstrip('/')}/{topic}/json",
            params={"poll": "1", "since": VENTANA_DUPLICADOS},
            timeout=TIMEOUT,
        )
        respuesta.raise_for_status()
    except requests.RequestException as error:
        # Ante la duda se envía: un aviso repetido es mejor que uno perdido.
        print(f"No se pudieron consultar los avisos recientes ({type(error).__name__}); se enviará igual", file=sys.stderr)
        return set()
    recientes = set()
    for linea in respuesta.text.splitlines():
        try:
            evento = json.loads(linea)
        except ValueError:
            continue
        if isinstance(evento, dict) and evento.get("event") == "message":
            recientes.add((evento.get("title", ""), evento.get("message", "")))
    return recientes


def notificar(mensajes: list[Mensaje], servidor: str, topic: str, enlace: str) -> bool:
    """Envía los mensajes en orden, salvo los que otro monitor acaba de enviar.

    Devuelve False si alguno no se pudo entregar.
    """
    recientes = publicados_recientes(servidor, topic) if mensajes else set()
    for mensaje in mensajes:
        if (mensaje.titulo, _texto_ntfy(mensaje)) in recientes:
            print(f"Omitido, otro monitor ya lo envió: {mensaje.titulo}")
            continue
        try:
            enviar_ntfy(mensaje, servidor, topic, enlace)
        except ErrorNotificacion as error:
            print(f"No se pudo enviar la notificación: {error}", file=sys.stderr)
            return False
    return True


# --- Estado y punto de entrada --------------------------------------------


def cargar_estado(ruta: Path) -> dict:
    return json.loads(ruta.read_text(encoding="utf-8")) if ruta.exists() else {}


def guardar_estado(ruta: Path, estado: dict) -> None:
    ruta.write_text(json.dumps(estado, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def cambios_historial(estado: dict, nuevo_estado: dict, ahora: datetime) -> list[dict[str, str]]:
    """Filas para el historial: qué carreras aparecieron o desaparecieron desde la revisión anterior."""
    if "habilitadas" not in nuevo_estado:  # todavía no hubo una lectura exitosa
        return []
    actuales = _a_carreras(nuevo_estado["habilitadas"])
    if "habilitadas" in estado:
        antes = _a_carreras(estado["habilitadas"])
        cambios = [("habilitada", c) for c in actuales - antes] + [("retirada", c) for c in antes - actuales]
    else:
        cambios = [("ya habilitada", c) for c in actuales]
    detectado = ahora.strftime("%Y-%m-%d %H:%M")
    return [
        {"detectado": detectado, "evento": evento, "facultad": c.facultad, "carrera": c.carrera, "modalidad": c.modalidad}
        for evento, c in sorted(cambios, key=lambda cambio: (cambio[1], cambio[0]))
    ]


def guardar_historial(ruta: Path, filas: list[dict[str, str]]) -> None:
    if not filas:
        return
    nuevo = not ruta.exists() or ruta.stat().st_size == 0
    with ruta.open("a", encoding="utf-8", newline="") as archivo:
        escritor = csv.DictWriter(archivo, fieldnames=CAMPOS_HISTORIAL)
        if nuevo:
            escritor.writeheader()
        escritor.writerows(filas)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="muestra lo que notificaría, sin enviar nada ni guardar el estado",
    )
    parser.add_argument(
        "--listar",
        action="store_true",
        help="muestra las carreras habilitadas agrupadas por facultad y termina",
    )
    args = parser.parse_args(argv)

    url = os.environ.get("URL_MATRICULA") or URL_MATRICULA
    if args.listar:
        try:
            carreras = obtener_carreras(url)
        except (requests.RequestException, ErrorPagina) as error:
            print(f"No se pudo leer la página: {error}", file=sys.stderr)
            return 1
        print(f"{len(carreras)} carreras habilitadas\n\n{listar_por_facultad(carreras) or 'Ninguna por ahora.'}")
        return 0

    facultad = os.environ.get("FACULTAD") or "FICA"
    carrera = os.environ.get("CARRERA") or "Software"
    ruta_estado = Path(os.environ.get("ARCHIVO_ESTADO") or "estado.json")

    topic = os.environ.get("NTFY_TOPIC")
    servidor = os.environ.get("NTFY_SERVIDOR") or "https://ntfy.sh"
    if not topic and not args.dry_run:
        print("Falta NTFY_TOPIC: el canal de ntfy al que se envían los avisos (o usa --dry-run).", file=sys.stderr)
        return 2

    estado = cargar_estado(ruta_estado)
    ahora = datetime.now(ECUADOR)
    try:
        carreras = con_reintentos(lambda: obtener_carreras(url))
    except (requests.RequestException, ErrorPagina) as error:
        print(f"No se pudo leer la página: {error}", file=sys.stderr)
        nuevo_estado, mensajes = registrar_fallo(estado, str(error), url)
    else:
        print(f"{len(carreras)} carreras habilitadas ({_resumen_facultades(carreras)})")
        nuevo_estado, mensajes = evaluar(estado, carreras, facultad, carrera, ahora, url)

    for mensaje in mensajes:
        print(f"\n[{'simulado' if args.dry_run else 'enviando'}] {mensaje.titulo}\n{mensaje.texto}")
    if not mensajes:
        print("Sin novedades.")
    if args.dry_run:
        return 0

    if not notificar(mensajes, servidor, topic, url):
        # No se guarda el estado, así el aviso se reintenta en la próxima ejecución.
        return 1
    if nuevo_estado != estado:
        guardar_historial(ruta_estado.with_name("historial.csv"), cambios_historial(estado, nuevo_estado, ahora))
        guardar_estado(ruta_estado, nuevo_estado)
    return 0


if __name__ == "__main__":
    sys.exit(main())
