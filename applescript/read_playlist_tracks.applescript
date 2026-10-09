-- Exporta las canciones de UNA playlist de usuario de Apple Music.
-- Uso: osascript read_playlist_tracks.applescript PERSISTENT_ID
--
-- Salida: una linea por cancion, campos separados por TAB:
--   nombre TAB artista
-- (mismo formato que read_loved.applescript; el parser en apple_provider.py
--  acepta los campos extra como opcionales).

on run argv
	if (count of argv) is 0 then error "falta el persistent ID de la playlist"
	set thePid to item 1 of argv

	set TABCH to tab
	set LFCH to linefeed

	tell application "Music"
		set pl to (first user playlist whose persistent ID is thePid)
		-- Lectura en bloque: una sola llamada Apple Event por propiedad.
		set theNames to name of every track of pl
		set theArtists to artist of every track of pl
	end tell

	set out to ""
	repeat with i from 1 to (count of theNames)
		set out to out & (item i of theNames as text) & TABCH & (item i of theArtists as text) & LFCH
	end repeat
	return out
end run
