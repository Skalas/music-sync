-- Marca como Favorita/Love en Apple Music cada cancion de un archivo de texto.
-- Uso: osascript mark_loved.applescript /ruta/al/archivo.txt
-- Cada linea tiene el formato "Nombre - Artista". El artista es el texto despues
-- del ultimo " - " (asi los nombres con guiones se preservan).

on run argv
	if (count of argv) is 0 then return "ERROR: falta la ruta del archivo"
	set filePath to item 1 of argv

	set fileText to read (POSIX file filePath) as «class utf8»
	set theLines to paragraphs of fileText

	set marked to 0
	set missed to 0

	tell application "Music"
		repeat with aLine in theLines
			set lineStr to (aLine as text)
			if lineStr is not "" then
				-- Separar "Nombre - Artista" por el ultimo " - "
				set AppleScript's text item delimiters to " - "
				set parts to text items of lineStr
				if (count of parts) >= 2 then
					set theArtist to (item -1 of parts)
					set nameParts to items 1 thru -2 of parts
					set AppleScript's text item delimiters to " - "
					set theName to (nameParts as text)
				else
					set theName to lineStr
					set theArtist to ""
				end if
				set AppleScript's text item delimiters to ""

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
					set t to item 1 of hits
					try
						set favorited of t to true
					on error
						try
							set loved of t to true
						end try
					end try
					set marked to marked + 1
				else
					set missed to missed + 1
				end if
			end if
		end repeat
	end tell

	return "marcadas: " & marked & " | no encontradas: " & missed
end run
