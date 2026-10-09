-- Lista las playlists de usuario de Apple Music.
-- Uso: osascript read_playlists.applescript
--
-- Salida: una linea por playlist, campos separados por TAB:
--   persistent_id TAB nombre TAB numero_de_canciones TAB editable
--
-- editable = 1 solo para playlists de usuario "normales" (no inteligentes y
-- con `special kind` igual a `none`). Las inteligentes, carpetas y especiales
-- (Genius, Purchased...) salen con editable = 0: nunca se leen ni se escriben,
-- pero el llamador (apple_provider.py) necesita saber que el nombre existe
-- para no crear una segunda playlist con el mismo nombre. El llamador filtra
-- ademas por nombre las automaticas que algunas versiones de macOS no marcan
-- como smart.

set TABCH to tab
set LFCH to linefeed

tell application "Music"
	set out to ""
	repeat with p in (every user playlist)
		set editable to "0"
		try
			if ((smart of p) is false) and ((special kind of p) is none) then set editable to "1"
		end try
		set thePid to (persistent ID of p) as text
		set theName to (name of p) as text
		set theCount to "0"
		try
			set theCount to (count of tracks of p) as text
		end try
		set out to out & thePid & TABCH & theName & TABCH & theCount & TABCH & editable & LFCH
	end repeat
	return out
end tell
