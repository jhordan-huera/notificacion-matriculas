import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import requests
from bs4 import BeautifulSoup

import monitor
from monitor import Carrera, Mensaje

FIXTURES = Path(__file__).parent / "fixtures"
URL = monitor.URL_MATRICULA
JUEVES = datetime(2026, 9, 24, 10, 0, tzinfo=monitor.ECUADOR)  # semana 2026-W39
SOFTWARE = Carrera("FICA", "Software (Rediseño)", "Presencial", "A")


def fixture(nombre: str) -> str:
    return (FIXTURES / nombre).read_text(encoding="utf-8")


def sopa(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def pagina_reporte(*carreras: Carrera) -> str:
    """Respuesta AJAX mínima del reporte, con la misma estructura que devuelve APEX."""
    filas = "".join(
        f'<tr><td headers="C1">{c.facultad}</td><td headers="C2">{c.carrera}</td>'
        f'<td headers="C3">{c.modalidad}</td><td headers="C4">{c.estado}</td></tr>'
        for c in carreras
    )
    return (
        '<div id="R16424608761106467_content" class="a-IRR-content"><table class="a-IRR-table">'
        '<tr><th id="C1">Facultad</th><th id="C2">Carrera</th><th id="C3">Modalidad</th>'
        f'<th id="C4">Estado</th></tr>{filas}</table></div>'
    )


CARRERAS_ACTUALES = monitor.leer_tabla(sopa(fixture("login.html")))


class RespuestaFalsa:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        pass


class SesionFalsa:
    def __init__(self, html_get: str, htmls_post: list[str]):
        self.headers = {}
        self._html_get = html_get
        self._htmls_post = list(htmls_post)
        self.posts = []

    def get(self, url, timeout):
        return RespuestaFalsa(self._html_get)

    def post(self, url, data, timeout):
        self.posts.append((url, data))
        return RespuestaFalsa(self._htmls_post.pop(0))


class LeerTablaTest(unittest.TestCase):
    def test_lee_las_carreras_de_la_pagina_real(self):
        self.assertEqual(len(CARRERAS_ACTUALES), 20)
        self.assertEqual(
            CARRERAS_ACTUALES[0],
            Carrera("FACAE", "Administración de Empresas (Rediseño)", "Presencial", "A"),
        )
        self.assertEqual({c.facultad for c in CARRERAS_ACTUALES}, {"FACAE", "FICAYA"})

    def test_pagina_sin_filas_devuelve_lista_vacia(self):
        self.assertEqual(monitor.leer_tabla(sopa(fixture("pagina_sin_filas.html"))), [])

    def test_estructura_desconocida_es_error(self):
        with self.assertRaises(monitor.ErrorPagina):
            monitor.leer_tabla(sopa("<html><body>Sitio en mantenimiento</body></html>"))


class ObtenerCarrerasTest(unittest.TestCase):
    def test_una_sola_pagina_no_pide_mas(self):
        sesion = SesionFalsa(fixture("login.html"), [])
        self.assertEqual(monitor.obtener_carreras(URL, sesion), CARRERAS_ACTUALES)
        self.assertEqual(sesion.posts, [])

    def test_recorre_las_paginas_siguientes(self):
        # Con 20 filas por página, la primera queda llena y hay que pedir la siguiente.
        login = fixture("login.html").replace(
            'id="R16424608761106467_row_select" value="50"',
            'id="R16424608761106467_row_select" value="20"',
        )
        sesion = SesionFalsa(login, [pagina_reporte(SOFTWARE)])

        carreras = monitor.obtener_carreras(URL, sesion)

        self.assertEqual(carreras, CARRERAS_ACTUALES + [SOFTWARE])
        [(url_ajax, datos)] = sesion.posts
        self.assertEqual(url_ajax, "https://cloud3.utn.edu.ec/ords/wwv_flow.ajax")
        self.assertEqual(datos["p_widget_action_mod"], "pgR_min_row=21max_rows=20rows_fetched=20")
        self.assertEqual(datos["p_instance"], "7413996052965")
        self.assertEqual(datos["x01"], "16424676081106468")
        self.assertEqual(datos["x02"], "16777529469264832")
        self.assertTrue(datos["p_request"].startswith("PLUGIN=UkVHSU9OIFRZUEV-"))


class EvaluarTest(unittest.TestCase):
    def evaluar(self, estado, carreras, ahora=JUEVES, facultad="FICA", carrera="Software"):
        return monitor.evaluar(estado, carreras, facultad, carrera, ahora, URL)

    def estado_inicial(self):
        estado, _ = self.evaluar({}, CARRERAS_ACTUALES)
        return estado

    def test_primera_ejecucion_confirma_que_esta_activo(self):
        estado, mensajes = self.evaluar({}, CARRERAS_ACTUALES)
        [mensaje] = mensajes
        self.assertIn("activado", mensaje.titulo)
        self.assertIn("Habilitadas ahora (20):\n\nFACAE (13)\n• Administración de Empresas", mensaje.texto)
        self.assertIn("FICAYA (7)", mensaje.texto)
        self.assertEqual(estado["objetivo"], [])
        self.assertEqual(len(estado["habilitadas"]), 20)
        self.assertEqual(estado["semana_resumen"], "2026-W39")

    def test_sin_cambios_no_avisa(self):
        _, mensajes = self.evaluar(self.estado_inicial(), CARRERAS_ACTUALES)
        self.assertEqual(mensajes, [])

    def test_ficaya_no_se_confunde_con_fica(self):
        engañosa = Carrera("FICAYA", "Software Agroindustrial", "Presencial", "A")
        _, mensajes = self.evaluar(self.estado_inicial(), CARRERAS_ACTUALES + [engañosa])
        [mensaje] = mensajes  # solo el aviso normal de cambios, no la alerta urgente
        self.assertEqual(mensaje.titulo, "📋 Nuevas carreras habilitadas: FICAYA")
        self.assertEqual(mensaje.prioridad, 3)

    def test_aviso_urgente_cuando_se_habilita(self):
        estado, mensajes = self.evaluar(self.estado_inicial(), CARRERAS_ACTUALES + [SOFTWARE])
        [mensaje] = mensajes  # sin un aviso de cambios duplicado
        self.assertEqual(mensaje.prioridad, 5)
        self.assertIn("Software (FICA) ya está habilitada", mensaje.titulo)
        self.assertIn("• Software (Rediseño) · Presencial", mensaje.texto)

        _, mensajes = self.evaluar(estado, CARRERAS_ACTUALES + [SOFTWARE])
        self.assertEqual(mensajes, [], "no debe repetir el aviso")

    def test_ignora_mayusculas_tildes_y_espacios(self):
        carrera = Carrera(" fica ", "INGENIERÍA DE  SOFTWARE", "En Línea", "A")
        _, mensajes = self.evaluar(
            self.estado_inicial(), CARRERAS_ACTUALES + [carrera], carrera="Ingenieria de software"
        )
        self.assertEqual([m.prioridad for m in mensajes], [5])

    def test_avisa_con_prioridad_alta_otras_carreras_de_la_facultad(self):
        electricidad = Carrera("FICA", "Electricidad", "Presencial", "A")
        _, mensajes = self.evaluar(self.estado_inicial(), CARRERAS_ACTUALES + [electricidad])
        [mensaje] = mensajes
        self.assertEqual(mensaje.titulo, "📋 Nuevas carreras habilitadas: FICA")
        self.assertEqual(mensaje.prioridad, 4)
        self.assertTrue(mensaje.texto.startswith("Nuevas:\nFICA (1)\n• Electricidad · Presencial"))
        self.assertIn("Habilitadas ahora (21):", mensaje.texto)

    def test_avisa_cambios_de_otras_facultades_agrupados(self):
        fecyt = [
            Carrera("FECYT", "Educación Básica", "Presencial", "A"),
            Carrera("FECYT", "Diseño Grafico (Rediseño)", "Presencial", "A"),
        ]
        estado, mensajes = self.evaluar(self.estado_inicial(), CARRERAS_ACTUALES + fecyt)
        [mensaje] = mensajes
        self.assertEqual(mensaje.titulo, "📋 Nuevas carreras habilitadas: FECYT")
        self.assertEqual(mensaje.prioridad, 3)
        self.assertIn("Nuevas:\nFECYT (2)\n• Diseño Grafico (Rediseño) · Presencial\n• Educación Básica", mensaje.texto)
        self.assertIn("Habilitadas ahora (22):\n\nFACAE (13)", mensaje.texto)

        _, mensajes = self.evaluar(estado, CARRERAS_ACTUALES)
        [mensaje] = mensajes
        self.assertEqual(mensaje.titulo, "📋 Cambios en carreras habilitadas: FECYT")
        self.assertIn("Ya no aparecen:\nFECYT (2)", mensaje.texto)

    def test_estado_de_version_anterior_envia_la_lista_actual(self):
        anterior = {"objetivo": [], "facultad": [], "semana_resumen": "2026-W39"}
        estado, mensajes = self.evaluar(anterior, CARRERAS_ACTUALES)
        [mensaje] = mensajes
        self.assertEqual(mensaje.titulo, "📋 Carreras habilitadas")
        self.assertTrue(mensaje.texto.startswith("Habilitadas ahora (20):\n\nFACAE (13)"))
        self.assertNotIn("facultad", estado)

        _, mensajes = self.evaluar(estado, CARRERAS_ACTUALES)
        self.assertEqual(mensajes, [])

    def test_lista_que_no_cabe_se_resume_por_facultad(self):
        muchas = [Carrera(f"FAC{i % 5}", f"Carrera con un nombre bastante largo número {i}", "Presencial", "A") for i in range(120)]
        _, [mensaje] = self.evaluar({}, muchas)
        self.assertLessEqual(len(mensaje.texto.encode()), monitor.LIMITE_NTFY)
        self.assertIn("Habilitadas ahora: 120 (FAC0: 24, FAC1: 24", mensaje.texto)

    def test_avisa_cuando_deja_de_aparecer(self):
        abierta, _ = self.evaluar(self.estado_inicial(), CARRERAS_ACTUALES + [SOFTWARE])
        _, mensajes = self.evaluar(abierta, CARRERAS_ACTUALES)
        [mensaje] = mensajes
        self.assertIn("ya no aparece", mensaje.titulo)

    def test_resumen_semanal_desde_las_nueve(self):
        estado = self.estado_inicial()  # semana 39
        lunes_temprano = datetime(2026, 9, 28, 8, 45, tzinfo=monitor.ECUADOR)
        lunes = datetime(2026, 9, 28, 9, 0, tzinfo=monitor.ECUADOR)

        _, mensajes = self.evaluar(estado, CARRERAS_ACTUALES, ahora=lunes_temprano)
        self.assertEqual(mensajes, [])

        estado, mensajes = self.evaluar(estado, CARRERAS_ACTUALES, ahora=lunes)
        [mensaje] = mensajes
        self.assertIn("Sigo vigilando", mensaje.titulo)
        self.assertIn("Habilitadas ahora (20):", mensaje.texto)
        self.assertEqual(estado["semana_resumen"], "2026-W40")


class ListarTest(unittest.TestCase):
    def test_agrupa_por_facultad(self):
        texto = monitor.listar_por_facultad(CARRERAS_ACTUALES + [SOFTWARE])
        grupos = texto.split("\n\n")
        self.assertEqual([g.splitlines()[0] for g in grupos], ["FACAE (13)", "FICA (1)", "FICAYA (7)"])
        self.assertIn("• Software (Rediseño) · Presencial", grupos[1])

    def test_muestra_el_estado_solo_si_no_es_el_habitual(self):
        self.assertEqual(SOFTWARE.describir(), "• Software (Rediseño) · Presencial")
        self.assertEqual(
            Carrera("FICA", "Software", "Presencial", "I").describir(), "• Software · Presencial · estado I"
        )


class FallosTest(unittest.TestCase):
    def test_avisa_una_vez_tras_varios_fallos_y_luego_la_recuperacion(self):
        estado, _ = monitor.evaluar({}, CARRERAS_ACTUALES, "FICA", "Software", JUEVES, URL)
        avisos = []
        for _ in range(monitor.UMBRAL_FALLOS + 2):
            estado, mensajes = monitor.registrar_fallo(estado, "Timeout", URL)
            avisos.append(len(mensajes))
        self.assertEqual(avisos, [0, 0, 1, 0, 0])
        self.assertEqual(estado["fallos_consecutivos"], monitor.UMBRAL_FALLOS)

        estado, mensajes = monitor.evaluar(estado, CARRERAS_ACTUALES, "FICA", "Software", JUEVES, URL)
        self.assertEqual([m.titulo for m in mensajes], ["✅ El monitor volvió a funcionar"])
        self.assertEqual(estado["fallos_consecutivos"], 0)
        self.assertFalse(estado["alerta_error_enviada"])


class NotificacionesTest(unittest.TestCase):
    def test_ntfy_envia_titulo_prioridad_y_enlace(self):
        with mock.patch.object(monitor.requests, "post") as post:
            post.return_value.ok = True
            monitor.enviar_ntfy(Mensaje("Título", "Texto", 5), "https://ntfy.sh", "mi-topic", URL)
        post.assert_called_once()
        self.assertEqual(post.call_args.args, ("https://ntfy.sh/",))
        self.assertEqual(
            post.call_args.kwargs["json"],
            {"topic": "mi-topic", "title": "Título", "message": "Texto", "priority": 5, "click": URL},
        )

    def test_ntfy_recorta_mensajes_demasiado_largos(self):
        with mock.patch.object(monitor.requests, "post") as post:
            post.return_value.ok = True
            monitor.enviar_ntfy(Mensaje("T", "ñ" * 5000), "https://ntfy.sh", "mi-topic", URL)
        enviado = post.call_args.kwargs["json"]["message"]
        self.assertLessEqual(len(enviado.encode()), monitor.LIMITE_NTFY)
        self.assertTrue(enviado.endswith("\n…"))

    def test_error_de_conexion_se_reporta_como_error_de_notificacion(self):
        with mock.patch.object(monitor.requests, "post", side_effect=requests.ConnectionError("sin red")):
            with self.assertRaises(monitor.ErrorNotificacion):
                monitor.enviar_ntfy(Mensaje("T", "X"), "https://ntfy.sh", "mi-topic", URL)


class MainTest(unittest.TestCase):
    def setUp(self):
        self.directorio = tempfile.TemporaryDirectory()
        self.addCleanup(self.directorio.cleanup)
        self.ruta_estado = Path(self.directorio.name) / "estado.json"
        entorno = {"NTFY_TOPIC": "topic-de-prueba", "ARCHIVO_ESTADO": str(self.ruta_estado)}
        for variable in ("NTFY_SERVIDOR", "FACULTAD", "CARRERA", "URL_MATRICULA"):
            entorno[variable] = ""
        parche = mock.patch.dict(os.environ, entorno)
        parche.start()
        self.addCleanup(parche.stop)
        parche = mock.patch.object(monitor, "obtener_carreras", return_value=CARRERAS_ACTUALES)
        parche.start()
        self.addCleanup(parche.stop)

    def test_sin_topic_termina_con_error(self):
        with mock.patch.dict(os.environ, {"NTFY_TOPIC": ""}):
            self.assertEqual(monitor.main([]), 2)

    def test_guarda_el_estado_solo_si_la_notificacion_llego(self):
        with mock.patch.object(monitor.requests, "post") as post:
            post.return_value.ok = False
            post.return_value.status_code = 500
            post.return_value.text = "error"
            self.assertEqual(monitor.main([]), 1)
            self.assertFalse(self.ruta_estado.exists())

            post.return_value.ok = True
            self.assertEqual(monitor.main([]), 0)
            self.assertTrue(self.ruta_estado.exists())

    def test_listar_no_necesita_topic_ni_envia_nada(self):
        with mock.patch.dict(os.environ, {"NTFY_TOPIC": ""}), mock.patch.object(monitor.requests, "post") as post:
            self.assertEqual(monitor.main(["--listar"]), 0)
        post.assert_not_called()
        self.assertFalse(self.ruta_estado.exists())

    def test_dry_run_no_envia_ni_guarda(self):
        with mock.patch.object(monitor.requests, "post") as post:
            self.assertEqual(monitor.main(["--dry-run"]), 0)
        post.assert_not_called()
        self.assertFalse(self.ruta_estado.exists())


if __name__ == "__main__":
    unittest.main()
