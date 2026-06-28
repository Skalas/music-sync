# music-sync — Conciliador bidireccional Spotify ⇄ Apple Music

Mantiene en sincronía tus canciones **"Me Gusta" (Liked Songs)** entre **Spotify** y **Apple Music**,
de forma **local y soberana**: sin apps de terceros, sin servidores externos, sin APIs de pago.

**Regla de unión:** si una canción está marcada en cualquiera de las dos plataformas, queda marcada en
ambas. La conciliación es **incremental** (recuerda lo ya hecho en `state.json`).

```
SPOTIFY (Liked)  ──user-library-read──┐
                                       ├──► match por nombre+artista ──► diff
APPLE MUSIC (Favoritas) ──osascript────┘
        │
        ├─ faltan en Apple   → Atajo (buscar en tienda + añadir) → osascript marca Love
        └─ faltan en Spotify → revisión → (--apply-spotify) → API agrega like
```

> **Seguridad:** escribir en Spotify es **opt-in**. Por defecto solo se genera un archivo de revisión
> (`to_spotify_review.txt`); las altas reales requieren `--apply-spotify`.

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

## 2. Instalación

```bash
cd ~/github/skalas/music-sync
uv venv .venv && source .venv/bin/activate
uv pip install spotipy python-dotenv tqdm unidecode
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
#  - empuja a Apple Music lo que falta (Atajo + Love)
#  - genera to_spotify_review.txt SIN escribir en Spotify
uv run sync_music.py

# Tras revisar to_spotify_review.txt, aplica las altas en Spotify:
uv run sync_music.py --apply-spotify
```

La **primera corrida** abre el navegador para autorizar (Spotify) y pide permisos de Automatización
(Música). Las siguientes son silenciosas e incrementales.

**Flags útiles:**

| Flag | Efecto |
|---|---|
| `--apply-spotify` | Aplica realmente los likes nuevos en Spotify (requiere scope de escritura). |
| `--dry-run` | Solo reporta el diff; no escribe en ningún lado. |
| `--full` | Ignora `state.json` y reconcilia todo desde cero. |
| `--no-apple` | No empuja hacia Apple Music. |
| `--no-spotify` | No procesa la dirección hacia Spotify. |

**Archivos generados (ignorados por git):**
- `state.json` — claves ya conciliadas en cada lado (incremental).
- `canciones_to_apple.txt` — entrada del Atajo (solo lo nuevo).
- `to_spotify_review.txt` — candidatos a agregar en Spotify, para revisar.
- `unmatched.log` — canciones de Apple sin match en Spotify.

---

## Limitaciones conocidas

- **Match cross-service** por nombre+artista normalizado: heurístico, no infalible (versiones en vivo,
  remasters, nombres ambiguos). Por eso la escritura en Spotify pasa por revisión previa.
- **Orden:** Apple Music no expone una lista de "Me Gusta" ordenada; las canciones se **procesan** en
  orden por fecha de like (`added_at`) pero su posición final en la biblioteca la decide Music.app.
- La propiedad de favorita cambió de `loved` a `favorited` entre versiones de macOS; el AppleScript
  maneja ambas con fallback.
