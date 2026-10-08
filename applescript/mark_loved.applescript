-- Marca como Favorita/Love las canciones que YA existen en la biblioteca.
-- Uso: osascript mark_loved.applescript /ruta/al/archivo.tsv
--
-- Entrada: una linea por cancion, campos separados por TAB:
--   nombre TAB artista
-- (TAB en vez de " - " porque los titulos con guiones rompen el parseo:
--  "Song - Live" se partia en nombre "Song" y artista "Live".)
--
-- Salida: una linea por cada linea de entrada, en el MISMO orden:
--   OK      TAB nombre TAB artista  -> estaba en la biblioteca y quedo marcada
--   MISSING TAB nombre TAB artista  -> no esta en la biblioteca (hay que importarla)
--   ERROR   TAB nombre TAB artista  -> estaba, pero no se pudo marcar
--
-- El llamador (apple_provider.py) alinea salida con entrada POR INDICE, asi que
-- la correspondencia 1:1 de lineas es parte del contrato de este script.

on run argv
	if (count of argv) is 0 then error "falta la ruta del archivo"
	set filePath to item 1 of argv

	-- Se resuelven fuera del bloque `tell` para que no compitan con los
	-- terminos del diccionario de Music.
	set TABCH to tab
	set LFCH to linefeed

	set fileText to read (POSIX file filePath) as «class utf8»
	set inputLines to paragraphs of fileText

	set out to ""

	tell application "Music"
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
				try
					if theArtist is not "" then
						set hits to (every track of library playlist 1 whose name is theName and artist is theArtist)
					end if
					if (count of hits) is 0 then
						set hits to (every track of library playlist 1 whose name is theName)
					end if
				on error
					set hits to {}
				end try

				if (count of hits) > 0 then
					set theStatus to "OK"
					try
						set favorited of (item 1 of hits) to true
					on error
						-- `loved` es el nombre de la propiedad en macOS anteriores.
						try
							set loved of (item 1 of hits) to true
						on error
							set theStatus to "ERROR"
						end try
					end try
				else
					set theStatus to "MISSING"
				end if

				set out to out & theStatus & TABCH & theName & TABCH & theArtist & LFCH
			end if
		end repeat
	end tell

	return out
end run
