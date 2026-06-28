# music-sync — Conciliador N-direcciones Spotify ⇄ Apple Music ⇄ Tidal

Mantiene en sincronía tus canciones **"Me Gusta" (Liked Songs / Favoritas)** entre **Spotify**,
**Apple Music** y **Tidal**, de forma **local y soberana**: sin apps de terceros, sin servidores
externos, sin APIs de pago.

**Regla de unión:** si una canción está marcada en **cualquiera** de las plataformas conectadas,
debe quedar marcada en **todas**. El estado vive en una **base de datos SQLite** (`library.db`) que es
la **fuente de verdad** y reemplaza al antiguo `state.json` (migrado automáticamente en la primera
corrida). La conciliación es **incremental**: solo procesa lo que falta en cada plataforma.

```
SPOTIFY (Liked)   ──user-library-read──┐
APPLE MUSIC (Fav) ──osascript──────────┤
TIDAL (Favorites) ──OAuth2 API─────────┘
        │
        ▼
   library.db (SQLite, fuente de verdad)  ──►  unión N-direcciones  ──►  diff por plataforma
        │                                                                      │
        ├─ exportable a CSV (--export)                                         │
        │                                                                      ▼
        └─ faltan en Apple   → Atajo + Love        (--apply-apple)
           faltan en Spotify → API agrega like     (--apply-spotify)
           faltan en Tidal   → API add favorite    (--apply-tidal)
```

> **Seguridad:** escribir en **cualquier** plataforma es **opt-in**. Por defecto la herramienta solo
> lee, actualiza `library.db` y genera archivos de revisión; las altas reales requieren el flag
> `--apply-<plataforma>` correspondiente.
>
> **Tidal es best-effort:** la cobertura de la API oficial para favoritos personales es incierta. Si
> Tidal falla (auth/scope/endpoint), la herramienta lo registra una vez, **omite** la dirección de Tidal
> y la conciliación Spotify⇄Apple continúa sin interrupción.

---

## 1. Crear la app en Spotify Developer (desde cero)

1. Entra a **https://developer.spotify.com/dashboard** e inicia sesión con tu cuenta de Spotify.
2. Pulsa **Create app**.
3. Rellena:
   - **App name:** `music-sync local` (cualquiera).
   - **App description:** lo que quieras.
   - **Redirect URI:** escribe exactamente **`http://127.0.0.1:8080`** y pulsa **Add**.
     > ⚠️ **No uses `http://localhost`** — Spotify lo deshabilitó en 2025. Tiene que ser la IP `127.0.0.1`.
   - **Which API/SDKs are you planning to use?** marca **Web API**.
4. Acepta los términos y pulsa **Save**.
5. Entra a la app → **Settings**. Copia el **Client ID** y, con **View client secret**, el **Client Secret**.
6. Crea tu `.env` a partir de la plantilla y pega las credenciales:
   ```bash
   cp .env.example .env
   # edita .env con tu Client ID y Client Secret
   ```

**Scopes que usa la herramienta:**
- `user-library-read` — siempre (leer tus Liked Songs).
- `user-library-modify` — solo cuando corres `--apply-spotify` (agregar likes).

---

## 1b. Crear la app en Tidal Developer (opcional, para incluir Tidal)

Tidal no tiene un hook tipo AppleScript, así que se usa la **API oficial** (`developer.tidal.com`)
con **OAuth2 + PKCE**.

1. Entra a **https://developer.tidal.com/dashboard** e inicia sesión.
2. Crea una app. En **Redirect URI** usa exactamente **`http://127.0.0.1:8080`** (igual que Spotify).
3. Copia el **Client ID** a tu `.env` (el flujo PKCE de cliente público **no usa** Client Secret):
   ```bash
   TIDAL_CLIENT_ID=...
   TIDAL_REDIRECT_URI=http://127.0.0.1:8080
   ```
4. La primera corrida con Tidal abre el navegador para autorizar; el token se cachea en
   `.tidal-cache` (permisos `0600`, ignorado por git).

> Si no configuras Tidal, corre con `--no-tidal` y la herramienta funciona igual que antes
> (solo Spotify⇄Apple). Si lo configuras pero la API falla, Tidal se omite con un aviso.

---

## 2. Instalación

```bash
cd ~/github/skalas/music-sync
uv venv .venv && source .venv/bin/activate
uv sync                 # instala deps de runtime + dev (pytest, ruff, mypy) desde uv.lock
```

---

## 3. Crear el Atajo `SyncToAppleMusic` en Atajos.app

El Atajo hace **una sola cosa**: tomar una lista de canciones, buscarlas en la tienda de Apple Music y
añadirlas a tu biblioteca. (El marcar "Favorita/Love" lo hace después un AppleScript, porque Atajos no
lo hace de forma confiable.)

**Pasos (créalo visualmente una vez):**

1. Abre **Atajos.app** → **+** (nuevo atajo). Renómbralo a **`SyncToAppleMusic`** (exacto, respeta mayúsculas).
2. Arriba, abre **ⓘ (Detalles del atajo)** y, en el tipo de entrada, asegúrate de que **acepta
   Texto/Archivos**. (La CLI le pasa el contenido del `.txt` como *Entrada del atajo*.)
3. Añade la acción **"Obtener texto de la entrada del atajo"** (busca "texto de la entrada").
4. Añade **"Dividir texto"**. Configúrala:
   - Texto = la salida del paso anterior.
   - Separador = **Líneas nuevas**.
5. Añade **"Repetir con cada elemento"** (Repeat with Each) y arrastra la lista del paso 4 como entrada.
   **Dentro del bucle:**
   1. Acción de búsqueda en la tienda (busca **"iTunes"** o **"Apple Music"** en el buscador de acciones;
      suele llamarse **"Buscar en la tienda de iTunes"** / *Search the iTunes Store*).
      - Consulta = **Elemento repetido**.
      - **Límite de resultados = 1** (toma el match más exacto).
   2. Acción **"Añadir a la biblioteca"** (*Add to Library*), tomando como entrada el resultado anterior (Canción).
   3. *(Opcional)* Acción **"Esperar"** = **0.5 s** para no saturar el demonio de Music.app.
6. Guarda (⌘S).

> **macOS 26:** los nombres exactos pueden variar; usa el buscador de acciones escribiendo "iTunes",
> "tienda" o "biblioteca" para localizarlas.
>
> **Permisos:** la primera ejecución pedirá permiso para que el Atajo controle Música. Acéptalo.

**Prueba el Atajo aislado** antes de integrarlo:
```bash
printf "Bohemian Rhapsody - Queen\nImagine - John Lennon\n" > /tmp/test.txt
shortcuts run "SyncToAppleMusic" -i /tmp/test.txt
```
Revisa que esas canciones aparezcan en tu biblioteca de Música.

---

## 4. Uso

```bash
# Conciliación segura (por defecto):
#  - lee Spotify + Apple Music + Tidal y actualiza library.db
#  - genera archivos de revisión SIN escribir en ningún servicio remoto
uv run sync_music.py

# Aplica realmente las altas, por plataforma (combinables):
uv run sync_music.py --apply-apple --apply-spotify --apply-tidal

# Ver / exportar tu biblioteca unificada a CSV (no toca la red):
uv run sync_music.py --offline --export biblioteca.csv
```

La **primera corrida** abre el navegador para autorizar (Spotify y, si lo configuraste, Tidal) y pide
permisos de Automatización (Música). Las siguientes son silenciosas e incrementales.

**Flags útiles:**

| Flag | Efecto |
|---|---|
| `--apply-spotify` | Aplica realmente los likes nuevos en Spotify (requiere scope de escritura). |
| `--apply-apple` | Aplica realmente las altas en Apple Music (Atajo + Love). |
| `--apply-tidal` | Aplica realmente las altas en Tidal Favorites (requiere scope de escritura). |
| `--export PATH.csv` | Exporta toda la biblioteca a CSV (una fila por canción, una columna por plataforma) y termina. |
| `--db PATH` | Ruta de la base SQLite (por defecto `library.db`). |
| `--offline` | No usa la red; opera solo sobre `library.db` (para exportar o inspeccionar el diff). |
| `--dry-run` | Solo reporta el diff; no escribe en ningún lado. |
| `--full` | Reconcilia todo desde cero (ignora lo ya marcado como sincronizado). |
| `--no-apple` | No procesa la dirección hacia Apple Music. |
| `--no-spotify` | No procesa la dirección hacia Spotify. |
| `--no-tidal` | Excluye Tidal por completo de la unión. |

**Base de datos (`library.db`, fuente de verdad):**
- Tabla `tracks` (una fila por canción normalizada) y `presence` (qué plataformas la tienen marcada y
  cuándo se sincronizó). Sustituye a `state.json`, que se **migra automáticamente** una sola vez.
- Inspecciónala con cualquier cliente SQLite, o expórtala con `--export`.

**Archivos generados (ignorados por git):**
- `library.db` — base SQLite, fuente de verdad (presencia por plataforma + estado de sync).
- `canciones_to_apple.txt` — entrada del Atajo / lista de revisión para Apple.
- `to_spotify_review.txt` — candidatos a agregar en Spotify, para revisar.
- `unmatched.log` — canciones sin match cross-service.
- `.tidal-cache` — token OAuth de Tidal (permisos `0600`).

---

## Limitaciones conocidas

- **Match cross-service** por nombre+artista normalizado: heurístico, no infalible (versiones en vivo,
  remasters, nombres ambiguos). Por eso toda escritura remota pasa por revisión previa (opt-in).
- **Tidal** usa la API oficial, cuya cobertura de favoritos personales es incierta y puede cambiar; la
  herramienta degrada con gracia (omite Tidal) si la API no responde como se espera.
- **Orden:** Apple Music no expone una lista de "Me Gusta" ordenada; las canciones se **procesan** en
  orden por fecha de like (`added_at`) pero su posición final en la biblioteca la decide Music.app.
- La propiedad de favorita cambió de `loved` a `favorited` entre versiones de macOS; el AppleScript
  maneja ambas con fallback.
