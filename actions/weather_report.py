"""Weather report action — opens a Google weather search for a given city.

Exposes ``weather_action``, which JARVIS calls when the user asks about the
weather. It validates the requested city, opens the browser on a Google
weather query, and optionally records the lookup in session memory.
"""
import webbrowser
from urllib.parse import quote_plus


def weather_action(
    parameters: dict,
    player=None,
    session_memory=None,
) -> str:
    """Open a browser weather search for the requested city.

    Args:
        parameters: Tool arguments; ``city`` (required) and optional ``time``
            (e.g. "today", "tomorrow"; defaults to "today").
        player: Optional UI handle used for on-screen logging.
        session_memory: Optional store; when present the query and reply are
            saved as the last search.

    Returns:
        A short natural-language status string (also logged), suitable for
        JARVIS to speak back to the user.
    """
    city     = parameters.get("city")
    when     = parameters.get("time", "today")  

    if not city or not isinstance(city, str) or not city.strip():
        msg = "Sir, the city is missing for the weather report."
        _log(msg, player)
        return msg

    city = city.strip()
    when = (when or "today").strip()

    search_query  = f"weather in {city} {when}"
    url           = f"https://www.google.com/search?q={quote_plus(search_query)}"

    try:
        opened = webbrowser.open(url)
        if not opened:
            raise RuntimeError("webbrowser.open returned False")
    except Exception as e:
        msg = f"Sir, I couldn't open the browser for the weather report: {e}"
        _log(msg, player)
        return msg

    msg = f"Showing the weather for {city}, {when}, sir."
    _log(msg, player)

    if session_memory:
        try:
            session_memory.set_last_search(query=search_query, response=msg)
        except Exception:
            pass

    return msg


def _log(message: str, player=None) -> None:
    print(f"[Weather] {message}")
    if player:
        try:
            player.write_log(f"JARVIS: {message}")
        except Exception:
            pass