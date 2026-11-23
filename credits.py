"""Project credits and third-party attribution text."""

from version import __version__

TMDB_URL = "https://www.themoviedb.org/"
TMDB_ATTRIBUTION_URL = "https://www.themoviedb.org/about/logos-attribution"
TMDB_ATTRIBUTION_NOTICE = (
    "This product uses the TMDB API but is not endorsed or certified by TMDB."
)

BOT_TAGLINE = "Film, TV & Documentary Guide"

ABOUT_CAPTION = f"""<b>Kokabiel — {BOT_TAGLINE}</b>
<code>v{__version__}</code>

Command-driven discovery for films, TV series, and documentaries,
with personal progress, group watchlists, ratings, and suggestions.

<b>Privacy</b>
Kokabiel responds only to commands and its own buttons.
It does not inspect ordinary group conversation.

<b>Data and images</b>
Kokabiel uses TMDB as its primary catalog and links to IMDb only
when TMDB supplies an IMDb title ID. Streaming-provider availability
returned through TMDB is sourced from JustWatch.

<i>{TMDB_ATTRIBUTION_NOTICE}</i>"""
