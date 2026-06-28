-- Exporta las canciones marcadas como Favorita/Love en la biblioteca de Apple Music.
-- Salida: una linea por cancion con el formato exacto "Nombre - Artista".
-- Maneja el cambio de nomenclatura de macOS: la propiedad fue "loved" y en
-- versiones recientes es "favorited". Se intenta favorited y se cae a loved.

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
		set theName to (name of t)
		set theArtist to (artist of t)
		set out to out & theName & " - " & theArtist & linefeed
	end repeat
	return out
end tell
