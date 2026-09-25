# Notificación de matrículas UTN

[![Pruebas](https://github.com/jhordan-huera/notificacion-matriculas/actions/workflows/pruebas.yml/badge.svg)](https://github.com/jhordan-huera/notificacion-matriculas/actions/workflows/pruebas.yml)

Te avisa en el celular cuando se habilita la carrera de **Software (FICA)** en la
[Matrícula En Línea de la UTN](https://cloud3.utn.edu.ec/ords/r/producci_n/jhsc42xld3flutexoqzq025366/y4eipevfjtvooejhnoyt026136).

**Página de estado:** https://jhordan-huera.github.io/notificacion-matriculas/, para compartir
con quien no tenga ntfy.

## Cómo funciona

La pantalla de login de la matrícula muestra públicamente la tabla **"Carreras Habilitadas
Actualmente"**. `monitor.py` la lee completa, sin usar tu usuario ni contraseña, la compara con
el último estado guardado en `estado.json` y te avisa por [ntfy](https://ntfy.sh) cuando:

| Aviso | Prioridad |
|---|---|
| 🎉 Software (FICA) aparece en la lista | urgente |
| 📋 Una facultad habilita o retira carreras, con la lista completa por facultad | alta si es FICA, normal si es otra |
| Software deja de aparecer en la lista | alta |
| ⚠️ La página falla 3 veces seguidas (caída o cambio de estructura) | alta |
| 🔎 Resumen semanal, los lunes desde las 9:00 (hora de Ecuador) | normal |

- Cada aviso se envía una sola vez. Si la notificación no llega a ntfy, el estado no se guarda y
  se reintenta en la siguiente revisión.
- Cada cambio queda registrado en [`historial.csv`](historial.csv): cuándo se detectó cada carrera
  habilitada o retirada. Sirve para saber en qué orden abren las facultades el próximo semestre.

### Quién hace las revisiones

| Revisión | Frecuencia | Depende de |
|---|---|---|
| [cron-job.org](https://console.cron-job.org) lanza el workflow de GitHub por la API | cada minuto | nada: funciona con la Mac apagada |
| Servicio de macOS (`launchd`), opcional y hoy apagado | cada minuto | que la Mac esté encendida |
| Horario propio de GitHub (`schedule`) | cada 5 minutos | GitHub, que a menudo lo retrasa o se lo salta |

Antes de enviar un aviso, cada revisión consulta los avisos recientes del canal de ntfy. Si otra
revisión ya envió uno idéntico en los últimos 10 minutos, lo omite, así no llegan avisos dobles.

## Configuración

### 1. Instala ntfy en el celular

1. Instala la app **ntfy** (Play Store o App Store). No necesitas crear cuenta.
2. Toca **+** y suscríbete a un canal con un nombre difícil de adivinar, por ejemplo
   `matriculas-fica-` seguido de letras al azar. Quien conozca el nombre puede leer los avisos,
   así que no uses uno obvio.
3. Permite que ntfy suene en "No molestar" para no perderte la alerta urgente.

### 2. Sube el proyecto a GitHub y guarda el canal como secreto

```bash
gh repo create notificacion-matriculas --public --source . --push
gh secret set NTFY_TOPIC   # pega el nombre del canal cuando lo pida
```

Se recomienda un repositorio **público**: ahí los minutos de GitHub Actions son ilimitados y los
secretos siguen sin verse.

Pruébalo con `gh workflow run monitor.yml`. En un par de minutos debe llegarte
"✅ Monitor de matrículas activado".

### 3. Revisión cada minuto con cron-job.org

GitHub no garantiza su propio horario, así que el que manda es cron-job.org:

1. Crea un token en https://github.com/settings/personal-access-tokens/new con acceso solo a este
   repositorio y el permiso **Actions: Read and write**.
2. En cron-job.org, crea un cronjob con estos datos:
   - **URL:** `https://api.github.com/repos/jhordan-huera/notificacion-matriculas/actions/workflows/monitor.yml/dispatches`
   - **Frecuencia:** cada minuto.
   - **Método:** `POST`.
   - **Cuerpo:** `{"ref":"main"}`.
   - **Encabezados:**
     - `Authorization: Bearer <token>`
     - `Accept: application/vnd.github+json`
     - `X-GitHub-Api-Version: 2022-11-28`
     - `Content-Type: application/json`
3. La ejecución de prueba debe responder **204**.

El token vence: cuando GitHub te avise, crea uno nuevo y reemplázalo en cron-job.org.

### 4. Alarma si el monitor se detiene (healthchecks.io)

El aviso ⚠️ cubre las fallas de la página de la UTN, pero no el caso en que el monitor mismo deja
de correr (cron-job.org desactivado, token vencido, caída de GitHub). Para eso:

1. Crea una cuenta gratis en https://healthchecks.io y agrega un check con **Period: 1 minute** y
   **Grace: 10 minutes**.
2. Copia su **Ping URL** y guárdala como secreto:
   ```bash
   gh secret set HEALTHCHECK_URL   # pega la Ping URL cuando lo pida
   ```
3. En **Integrations**, deja activo el email y, si quieres, agrega **ntfy** con tu canal.

Cada revisión de GitHub envía una señal de vida al terminar, o `/fail` si falló. Si pasan
10 minutos sin señal, healthchecks.io te avisa.

### 5. Página de estado (GitHub Pages)

La página está en [`docs/index.html`](docs/index.html) y se publica desde **Settings → Pages**
(rama `main`, carpeta `/docs`). Lee `estado.json` e `historial.csv` directamente del repositorio,
y la hora de la última revisión desde la API de GitHub. Se actualiza sola cada 2 minutos.

### 6. Respaldo en la Mac (opcional)

Un servicio de `launchd` revisa cada minuto mientras la Mac esté encendida. Usa su propio estado
e historial, en `~/Library/Application Support/notificacion-matriculas/`.

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.notificacion-matriculas.plist  # iniciar
launchctl bootout gui/$(id -u)/local.notificacion-matriculas                                  # detener
tail -f ~/Library/Logs/notificacion-matriculas.log                                           # ver qué hace
```

## Desarrollo

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/python monitor.py --listar      # carreras habilitadas ahora, agrupadas por facultad
.venv/bin/python monitor.py --dry-run     # muestra qué avisaría, sin enviar ni guardar nada
.venv/bin/python -m unittest discover -s tests -t .

# Enviar de verdad sin tocar el estado del repositorio (el historial se guarda junto al estado):
NTFY_TOPIC=tu-canal ARCHIVO_ESTADO=/tmp/prueba/estado.json .venv/bin/python monitor.py
```

Las pruebas corren solas en GitHub con cada `push` ([pruebas.yml](.github/workflows/pruebas.yml)).
Como el monitor usa el código de `main` un minuto después de cada `push`, corre las pruebas antes
de subir cambios.

## Personalizar

En **Settings → Secrets and variables → Actions → Variables** puedes definir:

| Variable | Por defecto | Cómo se compara |
|---|---|---|
| `FACULTAD` | `FICA` | Nombre exacto ("FICAYA" no cuenta como "FICA") |
| `CARRERA` | `Software` | Que esté contenido en el nombre, sin importar mayúsculas ni tildes |

Si las cambias, cambia también `FACULTAD` y `CARRERA` al inicio del script de
[`docs/index.html`](docs/index.html). Para volver a recibir el mensaje de bienvenida, borra
`estado.json` del repositorio.

## Limitaciones

- Cada revisión de GitHub tarda entre 20 y 60 segundos en arrancar y terminar, así que el aviso
  llega uno o dos minutos después de que se habilite la carrera.
- Si la UTN cambia la página o bloquea las conexiones desde GitHub, te llegará el aviso ⚠️.
- Los datos de la página de estado pueden tardar unos minutos en actualizarse por la caché de
  GitHub. La fuente oficial es siempre la página de matrícula.
