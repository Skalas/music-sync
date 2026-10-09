-- Agrega a una playlist de usuario canciones que YA existen en la biblioteca.
-- Uso: osascript add_to_playlist.applescript /ruta/al/archivo.tsv NOMBRE [PERSISTENT_ID]
--
-- Entrada: una linea por cancion, campos separados por TAB:
--   nombre TAB artista_principal
-- Se busca por nombre exacto + artista que CONTENGA el artista principal; sin
-- artista no se agrega nada (evita agregar una cancion equivocada).
--
-- La playlist se busca SOLO por PERSISTENT_ID. Nunca por nombre: un nombre
-- podria coincidir con una playlist del sistema, una inteligente o una carpeta,
-- que el llamador excluye a proposito. Si viene PERSISTENT_ID y no se encuentra,
-- el script falla (nunca crea un duplicado). Solo sin PERSISTENT_ID se crea una
-- playlist de usuario nueva, al encontrar la primera cancion, para no dejar
-- playlists vacias.
--
-- Salida: una linea por cada linea de entrada, en el MISMO orden:
--   ADDED   TAB nombre TAB artista  -> se agrego a la playlist
--   PRESENT TAB nombre TAB artista  -> ya estaba en la playlist (no se duplica)
--   MISSING TAB nombre TAB artista  -> no esta en la biblioteca
--   ERROR   TAB nombre TAB artista  -> estaba, pero no se pudo agregar
-- y al final, si la playlist existe:
--   PLAYLIST TAB persistent_id
--
-- El llamador (apple_provider.py) alinea salida con entrada POR INDICE.

on run argv
	if (count of argv) < 2 then error "uso: archivo.tsv NOMBRE [PERSISTENT_ID]"
	set filePath to item 1 of argv
	set plName to item 2 of argv
	set plPid to ""
	if (count of argv) >= 3 then set plPid to item 3 of argv

	set TABCH to tab
	set LFCH to linefeed

	set fileText to read (POSIX file filePath) as «class utf8»
	set inputLines to paragraphs of fileText

	set out to ""

	tell application "Music"
		set pl to missing value
		if plPid is not "" then
			try
				set pl to (first user playlist whose persistent ID is plPid)
			end try
			-- Con un id dado, no encontrarla (Music ocupado, timeout, borrada a mitad
			-- de corrida) es un error: crear otra duplicaria la playlist.
			if pl is missing value then error "playlist " & plPid & " no encontrada"
		end if

		repeat with aLine in inputLines
			set lineStr to (aLine as text)
			if lineStr is not "" then
				set AppleScript's text item delimiters to TABCH
				set parts to text items of lineStr
				set AppleScript's text item delimiters to ""

				set theName to item 1 of parts
				if (count of parts) >= 2 then
					set theArtist to item 2 of parts
				else
					set theArtist to ""
				end if

				set hits to {}
				if theArtist is not "" then
					try
						set hits to (every track of library playlist 1 whose name is theName and artist contains theArtist)
					on error
						set hits to {}
					end try
				end if

				if (count of hits) is 0 then
					set theStatus to "MISSING"
				else
					set theTrack to item 1 of hits
					try
						if pl is missing value then
							set pl to (make new user playlist with properties {name:plName})
						end if
						set theTrackPid to persistent ID of theTrack
						if exists (some track of pl whose persistent ID is theTrackPid) then
							set theStatus to "PRESENT"
						else
							duplicate theTrack to pl
							set theStatus to "ADDED"
						end if
					on error
						set theStatus to "ERROR"
					end try
				end if

				set out to out & theStatus & TABCH & theName & TABCH & theArtist & LFCH
			end if
		end repeat

		if pl is not missing value then
			set out to out & "PLAYLIST" & TABCH & ((persistent ID of pl) as text) & LFCH
		end if
	end tell

	return out
end run
