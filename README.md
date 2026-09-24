# Notificación de matrículas UTN

Te avisa en el celular cuando se habilita la carrera de **Software (FICA)** en la
[Matrícula En Línea de la UTN](https://cloud3.utn.edu.ec/ords/r/producci_n/jhsc42xld3flutexoqzq025366/y4eipevfjtvooejhnoyt026136).

## Cómo funciona

La pantalla de login de la matrícula muestra públicamente la tabla **"Carreras Habilitadas
Actualmente"**. `monitor.py` la lee completa cada 5 minutos desde GitHub Actions, sin usar
tu usuario ni contraseña. Luego la compara con el último estado guardado en `estado.json` y te
avisa por [ntfy](https://ntfy.sh) cuando:

| Aviso | Prioridad |
|---|---|
| 🎉 Software (FICA) aparece en la lista | urgente |
| Aparece o desaparece otra carrera de FICA (señal de que la apertura está cerca) | alta |
| Software deja de aparecer en la lista | alta |
| ⚠️ La página falla 3 veces seguidas (caída o cambio de estructura) | alta |
| 🔎 Resumen semanal, los lunes desde las 9:00 (hora de Ecuador) | normal |

Cada aviso se envía una sola vez. Si la notificación no llega a ntfy, el estado no se guarda y
se reintenta en la siguiente ejecución.

## Configuración

### 1. Instala ntfy en el celular

1. Instala la app **ntfy** (Play Store o App Store). No necesitas crear cuenta.
2. Toca **+** y suscríbete a un canal con un nombre difícil de adivinar, por ejemplo
   `matriculas-fica-` seguido de letras al azar. Quien conozca el nombre puede leer los avisos,
   así que no uses uno obvio.

### 2. Sube el proyecto a GitHub

```bash
git init && git add . && git commit -m "Monitor de matrículas"
gh repo create notificacion-matriculas --public --source . --push
```

Se recomienda un repositorio **público**, porque ahí los minutos de GitHub Actions son
ilimitados. En uno privado tienes 2000 minutos gratis al mes y cada ejecución cuenta como un
minuto: cambia el cron de [monitor.yml](.github/workflows/monitor.yml) a `*/30 * * * *`.
Los secretos no se ven aunque el repositorio sea público.

### 3. Guarda el nombre del canal como secreto

```bash
gh secret set NTFY_TOPIC   # pega el nombre del canal cuando lo pida
```

O en GitHub: **Settings → Secrets and variables → Actions → New repository secret**.

### 4. Pruébalo

```bash
gh workflow run monitor.yml
```

O en GitHub: **Actions → Monitor de matrículas → Run workflow**. En un par de minutos debe
llegarte "✅ Monitor de matrículas activado". Desde ahí corre solo cada 5 minutos.

## Probar en tu computadora

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/python monitor.py --listar      # carreras habilitadas ahora, agrupadas por facultad
.venv/bin/python monitor.py --dry-run     # muestra qué avisaría, sin enviar ni guardar nada
.venv/bin/python -m unittest discover -s tests -t .

# Enviar de verdad sin tocar el estado.json del repositorio:
NTFY_TOPIC=tu-canal ARCHIVO_ESTADO=/tmp/estado-prueba.json .venv/bin/python monitor.py
```

## Personalizar

En **Settings → Secrets and variables → Actions → Variables** puedes definir:

| Variable | Por defecto | Cómo se compara |
|---|---|---|
| `FACULTAD` | `FICA` | Nombre exacto ("FICAYA" no cuenta como "FICA") |
| `CARRERA` | `Software` | Que esté contenido en el nombre, sin importar mayúsculas ni tildes |

Para volver a recibir el mensaje de bienvenida, borra `estado.json` del repositorio.

## Limitaciones

- GitHub puede retrasar o saltarse ejecuciones programadas en horas pico, así que el aviso suele
  llegar entre 5 y 15 minutos después de que se habilite la carrera, y a veces más.
- Si la UTN cambia la página o bloquea conexiones desde los servidores de GitHub (están fuera de
  Ecuador), te llegará el aviso ⚠️. En ese caso, el script puede correr con cron en tu propia
  computadora.
- En repositorios públicos, GitHub pausa los cron tras 60 días sin actividad. El resumen semanal
  hace un commit en `estado.json` cada semana, lo que lo evita.
