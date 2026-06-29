-- Exporta las canciones marcadas como Favorita/Love en Apple Music.
-- Salida: una linea por cancion con campos separados por TAB:
--   name TAB artist TAB album TAB year TAB duration_sec TAB date_added
-- El delimitador TAB es improbable en metadatos de canciones.
-- El parser en apple_provider.py acepta tambien el formato legado "Nombre - Artista".

on zeroPad(n)
	set s to n as text
	if length of s < 2 then set s to "0" & s
	return s
end zeroPad

on formatDate(d)
	set y to year of d as text
	set mo to zeroPad(month of d as integer)
	set dy to zeroPad(day of d)
	return y & "-" & mo & "-" & dy
end formatDate

tell application "Music"
	set faves to {}
	try
		set faves to (every track of library playlist 1 whose favorited is true)
	on error
		try
			set faves to (every track of library playlist 1 whose loved is true)
		on error
			set faves to {}
		end try
	end try

	set out to ""
	repeat with t in faves
		set theName to (name of t) as text
		set theArtist to (artist of t) as text
		set theAlbum to ""
		try
			set theAlbum to (album of t) as text
		end try
		set theYear to ""
		try
			set theYear to (year of t) as text
		end try
		set theDuration to ""
		try
			set theDuration to (round (duration of t)) as text
		end try
		set theDateAdded to ""
		try
			set theDateAdded to my formatDate(date added of t)
		end try
		set out to out & theName & tab & theArtist & tab & theAlbum & tab & theYear & tab & theDuration & tab & theDateAdded & linefeed
	end repeat
	return out
end tell
