"""Movie knowledge command — TMDB-powered movie lookup with interactive drill-downs.

A voice command produces a rich inbox item with synopsis + cast + director +
similar films. Each cast / director / similar element is tappable in the mobile
app; the tap fires a `@callback` method on this command which produces a
follow-up inbox item one level deeper (actor's filmography, director's other
films, or a fresh crawl of the similar movie).

The interactive-element flow:
  mobile tap -> CC POST /api/v0/callbacks -> CC MQTT -> node `handle_callback`
  -> `cmd.get_callbacks()[name](data, request_info)` -> follow-up inbox item.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, List

import httpx

from jarvis_command_sdk import (
    CommandExample,
    CommandResponse,
    FastPathPattern,
    IJarvisCommand,
    JarvisPackage,
    JarvisParameter,
    JarvisSecret,
    JarvisStorage,
    PreRouteResult,
    RequestInformation,
    UserSettings,
    callback,
)


# User-controlled settings (mobile UI surface).
_settings = UserSettings("movie_knowledge")


def _push_enabled() -> bool:
    """Read the per-user push-notification toggle for this command.

    Defaults to False — the inbox item always lands; the push (= phone
    buzz) is opt-in via the mobile app's settings UI.
    """
    return _settings.is_enabled("push_notifications", default=False)


# ── Pre-route regexes ───────────────────────────────────────────────────────
# Deterministic phrasings that skip the LLM. Each one demands the word "movie"
# be present so we don't grab unrelated "look up X" / "tell me about X"
# utterances that belong to other commands.

# `[\s,:.]+` is the separator between the leading phrase and the captured
# title. Whisper occasionally inserts a comma or colon ("Look up the movie,
# The Matrix.") and that punctuation has to match too — using `\s+` here
# silently failed the most natural phrasing.

_LOOK_UP_MOVIE_RE = re.compile(
    r"\b(?:look\s+up|pull\s+up|tell\s+me\s+about|info(?:rmation)?\s+(?:on|about)"
    r"|what\s+is|what'?s)"
    r"[\s,:.]+(?:the[\s,:.]+)?movie[\s,:.]+(?:called[\s,:.]+)?(.+?)\s*[?.!]*$",
    re.IGNORECASE,
)

_WHOS_IN_RE = re.compile(
    r"\bwho(?:'?s|\s+is|\s+stars)[\s,:.]+in[\s,:.]+(?:the[\s,:.]+movie[\s,:.]+)?(.+?)\s*[?.!]*$",
    re.IGNORECASE,
)

# Whisper consistently mangles "IMDB crawl" into "I am DB crawl" / "I.M.D.B."
# / etc. We accept the plain form, the letter-by-letter form (with optional
# dots / spaces between letters), and the "i am d b" misread, with or without
# the trailing "crawl" word.
_IMDB_CRAWL_RE = re.compile(
    r"\b(?:"
    r"imdb"
    r"|i\s*\.?\s*m\s*\.?\s*d\s*\.?\s*b\.?"
    r"|i\s+am\s+d\s*b"
    r")(?:[\s,:.]+crawl)?[\s,:.]+(.+?)\s*[?.!]*$",
    re.IGNORECASE,
)

try:
    from jarvis_log_client import JarvisLogger
except ImportError:
    import logging

    class JarvisLogger:
        def __init__(self, **kw):
            self._log = logging.getLogger(kw.get("service", __name__))

        def info(self, msg, **kw):
            self._log.info(msg)

        def warning(self, msg, **kw):
            self._log.warning(msg)

        def error(self, msg, **kw):
            self._log.error(msg)


logger = JarvisLogger(service="cmd.movie_knowledge")
_storage = JarvisStorage("movie_knowledge")


# ── TMDB constants ─────────────────────────────────────────────────────────

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMG_BASE = "https://image.tmdb.org/t/p/w185"
REQUEST_TIMEOUT = 10.0

# How many cast / similar films to embed as interactive elements in each
# inbox item. Keep these moderate — Expo push payload is 4KB but, more
# importantly, a wall of 30 chips is unusable.
MAX_CAST = 12
MAX_SIMILAR = 6
MAX_FILMOGRAPHY = 12


# ── Small helpers (no external state) ───────────────────────────────────────


def _tmdb_get(api_key: str, path: str, **params: Any) -> dict | None:
    """GET a TMDB endpoint; return parsed JSON or None on any failure."""
    params["api_key"] = api_key
    try:
        resp = httpx.get(f"{TMDB_BASE}{path}", params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("TMDB request failed path=%s error=%s" % (path, e))
        return None


def _search_movie(api_key: str, query: str) -> dict | None:
    """Top TMDB search result for a movie title (or None if no match)."""
    data = _tmdb_get(api_key, "/search/movie", query=query, include_adult="false")
    if not data:
        return None
    results = data.get("results") or []
    return results[0] if results else None


def _movie_detail(api_key: str, movie_id: int) -> dict | None:
    """Full movie detail with credits + similar in a single TMDB call."""
    return _tmdb_get(
        api_key, f"/movie/{movie_id}",
        append_to_response="credits,similar",
    )


def _person_detail(api_key: str, person_id: int) -> dict | None:
    """Person bio + filmography in a single TMDB call."""
    return _tmdb_get(
        api_key, f"/person/{person_id}",
        append_to_response="movie_credits",
    )


def _director_of(movie_data: dict) -> dict | None:
    """First credited director from a TMDB movie's crew array."""
    crew = (movie_data.get("credits") or {}).get("crew") or []
    for c in crew:
        if c.get("job") == "Director":
            return c
    return None


def _year(date_str: str | None) -> str | None:
    if not date_str or len(date_str) < 4:
        return None
    return date_str[:4]


# ── Notification posting ────────────────────────────────────────────────────
#
# Nodes don't hold app credentials, so we don't talk to jarvis-notifications
# directly. Instead the command POSTs to a command-center endpoint that's
# node-auth'd (X-API-Key) — CC resolves household_id from the validated
# node and creates the inbox item server-side. CC also injects node_id
# into metadata so any interactive-element callback routes back here.
#
# CC URL: discovered via the standard jarvis-config-client library (already
# installed on every node).
# Node creds: read from the node's config.json (CONFIG_PATH env var, set
# by the node's systemd unit).


_NODE_CREDS_CACHE: tuple[str, str] | None = None


def _get_node_creds() -> tuple[str, str] | None:
    """Read node_id + api_key from the node's config.json. Cached after first read."""
    global _NODE_CREDS_CACHE
    if _NODE_CREDS_CACHE is not None:
        return _NODE_CREDS_CACHE
    path = os.environ.get("CONFIG_PATH")
    if not path:
        return None
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        logger.warning("Could not read CONFIG_PATH=%s: %s" % (path, e))
        return None
    node_id = (data.get("node_id") or "").strip()
    api_key = (data.get("api_key") or "").strip()
    if not (node_id and api_key):
        return None
    _NODE_CREDS_CACHE = (node_id, api_key)
    return _NODE_CREDS_CACHE


def _get_cc_url() -> str | None:
    """Resolve command-center URL via jarvis-config-client; env var fallback."""
    try:
        from jarvis_config_client import get_command_center_url
        url = get_command_center_url()
        if url:
            return url
    except ImportError:
        pass
    except Exception as e:
        logger.warning("config-client lookup failed: %s" % e)
    return os.environ.get("JARVIS_COMMAND_CENTER_URL") or None


def _post_node_inbox_item(payload: dict) -> str | None:
    """POST a rich inbox item to CC's /api/v0/node/inbox-item endpoint.

    Node-authed with X-API-Key. The caller fills in everything except
    ``create_push_notification`` (read here from the user's setting) and
    ``target_type`` (defaults to "user" when the caller supplies a
    user_id — voice flow has a known speaker, callback flow has the
    tapping user, so the push should buzz only their phone, not the
    whole household). The caller can override either by passing it in.

    Returns the inbox item id on success or None if anything fails.
    Failure is logged; the command keeps going so the user still gets a
    spoken response.
    """
    cc_url = _get_cc_url()
    if not cc_url:
        logger.warning("Cannot post inbox item — command-center URL not resolved")
        return None
    creds = _get_node_creds()
    if not creds:
        logger.warning("Cannot post inbox item — node credentials not available")
        return None
    node_id, api_key = creds
    inferred_target = "user" if payload.get("user_id") else "household"
    payload = {
        "create_push_notification": _push_enabled(),
        "target_type": inferred_target,
        **payload,
    }
    try:
        resp = httpx.post(
            f"{cc_url.rstrip('/')}/api/v0/node/inbox-item",
            json=payload,
            headers={"X-API-Key": f"{node_id}:{api_key}"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return (resp.json() or {}).get("id")
    except (httpx.HTTPError, ValueError, KeyError) as e:
        logger.error("Inbox post failed: %s" % e)
        return None


# ── Inbox-item builders ─────────────────────────────────────────────────────


# Every drill-down chip in this command pushes a new screen onto the
# mobile navigation stack rather than producing a separate inbox item.
# Cheap TMDB call + natural back-stack > async inbox spam.
_STACK = "stack"


def _cast_elements(movie: dict) -> list[dict]:
    cast = (movie.get("credits") or {}).get("cast") or []
    return [
        {
            "id": f"actor-{c['id']}",
            "label": c.get("name") or "Unknown",
            "sublabel": (f"as {c['character']}" if c.get("character") else None),
            "kind": "actor",
            "command": "movie_knowledge",
            "callback": "expand_actor",
            "data": {"actor_id": c["id"]},
            "navigation_type": _STACK,
        }
        for c in cast[:MAX_CAST]
        if c.get("id") and c.get("name")
    ]


def _director_element(movie: dict) -> dict | None:
    d = _director_of(movie)
    if not d or not d.get("id"):
        return None
    return {
        "id": f"director-{d['id']}",
        "label": d.get("name") or "Unknown",
        "kind": "director",
        "command": "movie_knowledge",
        "callback": "expand_director",
        "data": {"director_id": d["id"]},
        "navigation_type": _STACK,
    }


def _similar_elements(movie: dict) -> list[dict]:
    similar = (movie.get("similar") or {}).get("results") or []
    out: list[dict] = []
    for s in similar[:MAX_SIMILAR]:
        if not s.get("id") or not s.get("title"):
            continue
        year = _year(s.get("release_date"))
        out.append({
            "id": f"movie-{s['id']}",
            "label": s["title"],
            "sublabel": year,
            "kind": "movie",
            "command": "movie_knowledge",
            "callback": "expand_movie",
            "data": {"movie_id": s["id"]},
            "navigation_type": _STACK,
        })
    return out


def _movie_body(movie: dict) -> str:
    overview = movie.get("overview") or "_No synopsis available._"
    director = _director_of(movie)
    director_line = f"**Director:** {director['name']}\n" if director and director.get("name") else ""
    runtime = movie.get("runtime")
    runtime_line = f"**Runtime:** {runtime} min\n" if runtime else ""
    rating = movie.get("vote_average")
    rating_line = f"**TMDB rating:** {rating:.1f}/10\n" if isinstance(rating, (int, float)) else ""
    genres = ", ".join(g["name"] for g in (movie.get("genres") or []) if g.get("name"))
    genres_line = f"**Genres:** {genres}\n" if genres else ""
    return f"{overview}\n\n{director_line}{genres_line}{runtime_line}{rating_line}".strip()


def _filmography_elements(films: list[dict]) -> list[dict]:
    """Build interactive movie chips for a person's filmography slice."""
    out: list[dict] = []
    for f in films[:MAX_FILMOGRAPHY]:
        if not f.get("id") or not f.get("title"):
            continue
        year = _year(f.get("release_date"))
        out.append({
            "id": f"movie-{f['id']}",
            "label": f["title"],
            "sublabel": year,
            "kind": "movie",
            "command": "movie_knowledge",
            "callback": "expand_movie",
            "data": {"movie_id": f["id"]},
            "navigation_type": _STACK,
        })
    return out


# ── Result builders (callback context_data) ────────────────────────────────
#
# Callbacks no longer POST inbox items themselves — they return the
# renderable content via CommandResponse.context_data["inbox"]. CC reads
# the original mobile tap's navigation_type and decides whether to:
#   - "new_notification": fan out to a server-side inbox item via
#     post_inbox_item_sync (the existing async surface)
#   - "stack" / "popover": just store the result; mobile polls and
#     renders inline.


def _movie_inbox_block(movie: dict) -> dict:
    """The {title, summary, body, category, metadata} block for a movie crawl."""
    title = movie.get("title") or "Movie"
    year = _year(movie.get("release_date"))
    display_title = f"{title} ({year})" if year else title

    elements: list[dict] = []
    d = _director_element(movie)
    if d:
        elements.append(d)
    elements.extend(_cast_elements(movie))
    elements.extend(_similar_elements(movie))

    return {
        "title": display_title,
        "summary": (movie.get("tagline") or movie.get("overview") or "")[:160],
        "body": _movie_body(movie),
        "category": "movie_knowledge",
        "metadata": {
            "interactive_elements": elements,
            "tmdb_movie_id": movie.get("id"),
            "content_format": "markdown",
        },
    }


def _person_inbox_block(person: dict, *, kind: str) -> dict:
    """The {title, summary, body, category, metadata} block for a person crawl."""
    name = person.get("name") or "Unknown"
    bio = (person.get("biography") or "_No bio available._")
    credits = (person.get("movie_credits") or {})
    if kind == "director":
        films = [c for c in (credits.get("crew") or []) if c.get("job") == "Director"]
    else:
        films = credits.get("cast") or []
    films.sort(key=lambda f: f.get("release_date") or "", reverse=True)

    return {
        "title": f"{name} — Films" if kind == "actor" else f"{name} — Directed",
        "summary": (person.get("known_for_department") or "").strip()[:160],
        "body": bio[:1200],
        "category": "movie_knowledge",
        "metadata": {
            "interactive_elements": _filmography_elements(films),
            "tmdb_person_id": person.get("id"),
            "person_kind": kind,
            "content_format": "markdown",
        },
    }


def _emit_movie_inbox(movie: dict, *, user_id: int | None) -> str | None:
    """Build + POST the *initial* inbox item produced by a voice crawl.

    Used by run() only — callbacks return content via context_data instead.
    No household_id / node_id in the payload — CC resolves them from the
    authenticated node and injects node_id into metadata server-side.
    """
    block = _movie_inbox_block(movie)
    return _post_node_inbox_item({"user_id": user_id, **block})


# ── Command class ──────────────────────────────────────────────────────────


class MovieKnowledgeCommand(IJarvisCommand):

    @property
    def command_name(self) -> str:
        return "movie_knowledge"

    @property
    def description(self) -> str:
        return (
            "Look up a movie by title and produce a rich inbox card with "
            "synopsis, cast, director, and similar films — each tappable to "
            "drill one level deeper into an actor's filmography or related "
            "films. Use when the user asks to look up, get info on, learn "
            "about, or explore a movie. Distinct from a ratings lookup."
        )

    @property
    def keywords(self) -> List[str]:
        return [
            "movie", "movies", "film", "imdb",
            "look up", "info", "info on", "about the movie", "tell me about",
            "cast", "actor", "actors", "director", "synopsis", "starring",
            "who is in", "who stars in",
        ]

    @property
    def parameters(self) -> List[JarvisParameter]:
        return [
            JarvisParameter(
                name="movie_title",
                param_type="string",
                required=True,
                description="The movie title to look up.",
            ),
        ]

    @property
    def required_secrets(self) -> List[JarvisSecret]:
        return [
            JarvisSecret(
                key="TMDB_API_KEY",
                description="TMDB v3 API key",
                scope="integration",
                value_type="string",
                is_sensitive=True,
                required=True,
            ),
            # User-facing toggle (rendered as a switch in mobile settings).
            JarvisSecret(
                key="MOVIE_KNOWLEDGE_PUSH_NOTIFICATIONS",
                description="Send a push notification when a movie crawl card appears in the inbox.",
                scope="integration",
                value_type="bool",
                is_sensitive=False,
                required=False,
                friendly_name="Push notifications",
            ),
        ]

    @property
    def required_packages(self) -> List[JarvisPackage]:
        return [JarvisPackage(name="httpx")]

    def generate_prompt_examples(self) -> List[CommandExample]:
        return [
            CommandExample(
                voice_command="look up the movie The Matrix",
                expected_parameters={"movie_title": "The Matrix"},
                is_primary=True,
            ),
            CommandExample(
                voice_command="tell me about the movie Inception",
                expected_parameters={"movie_title": "Inception"},
            ),
            CommandExample(
                voice_command="who's in the movie Forrest Gump",
                expected_parameters={"movie_title": "Forrest Gump"},
            ),
            CommandExample(
                voice_command="info on the movie Dune",
                expected_parameters={"movie_title": "Dune"},
            ),
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        titles = [
            "The Matrix", "Inception", "Forrest Gump", "Pulp Fiction",
            "The Godfather", "Dune", "Oppenheimer", "Parasite",
            "Everything Everywhere All At Once", "Spirited Away",
            "The Shawshank Redemption", "Goodfellas",
        ]
        out: list[CommandExample] = []
        for t in titles:
            out.append(CommandExample(
                voice_command=f"look up the movie {t}",
                expected_parameters={"movie_title": t},
            ))
            out.append(CommandExample(
                voice_command=f"tell me about the movie {t}",
                expected_parameters={"movie_title": t},
            ))
            out.append(CommandExample(
                voice_command=f"who's in {t}",
                expected_parameters={"movie_title": t},
            ))
        return out

    # ── Fast-path patterns: skip the LLM for deterministic phrasings ───────

    @property
    def fast_path_patterns(self) -> List[FastPathPattern]:
        return [
            FastPathPattern(
                id="movie_knowledge.look_up",
                description="Bypass LLM for 'look up / tell me about the movie X'",
                example="look up the movie The Matrix",
                regex=_LOOK_UP_MOVIE_RE.pattern,
                handler="_fp_look_up_movie",
            ),
            FastPathPattern(
                id="movie_knowledge.whos_in",
                description="Bypass LLM for 'who's in X' / 'who stars in X'",
                example="who's in Forrest Gump",
                regex=_WHOS_IN_RE.pattern,
                handler="_fp_whos_in",
            ),
            FastPathPattern(
                id="movie_knowledge.imdb_crawl",
                description=(
                    "Bypass LLM for 'IMDB crawl X' (Whisper often mishears "
                    "this as 'I am DB crawl' — we accept both)"
                ),
                example="IMDB crawl The Matrix",
                regex=_IMDB_CRAWL_RE.pattern,
                handler="_fp_imdb_crawl",
            ),
        ]

    def _fp_look_up_movie(
        self, match: "re.Match[str]", voice_command: str,
    ) -> PreRouteResult | None:
        title = (match.group(1) or "").strip()
        if not title:
            return None
        return PreRouteResult(arguments={"movie_title": title})

    def _fp_whos_in(
        self, match: "re.Match[str]", voice_command: str,
    ) -> PreRouteResult | None:
        title = (match.group(1) or "").strip()
        if not title:
            return None
        return PreRouteResult(arguments={"movie_title": title})

    def _fp_imdb_crawl(
        self, match: "re.Match[str]", voice_command: str,
    ) -> PreRouteResult | None:
        title = (match.group(1) or "").strip()
        if not title:
            return None
        return PreRouteResult(arguments={"movie_title": title})

    def run(self, request_info: RequestInformation, **kwargs) -> CommandResponse:
        title = (kwargs.get("movie_title") or "").strip()
        if not title:
            return CommandResponse.error_response("Which movie should I look up?")

        api_key = _storage.get_secret("TMDB_API_KEY", scope="integration")
        if not api_key:
            return CommandResponse.error_response(
                "TMDB API key not configured. Add it in mobile settings.",
            )

        hit = _search_movie(api_key, title)
        if not hit or not hit.get("id"):
            return CommandResponse.success_response(
                {"message": f"I couldn't find a movie called '{title}' on TMDB."},
                wait_for_input=False,
            )

        movie = _movie_detail(api_key, hit["id"])
        if not movie:
            return CommandResponse.error_response(
                f"Couldn't load details for '{title}'.",
            )

        inbox_id = _emit_movie_inbox(movie, user_id=request_info.user_id)
        display_title = movie.get("title") or title
        if inbox_id:
            return CommandResponse.final_response({
                "message": f"Added {display_title} to your inbox — tap an actor to see more.",
                "inbox_item_id": inbox_id,
                "tmdb_movie_id": movie.get("id"),
            })
        # Inbox post failed — still answer the user with a brief reply.
        return CommandResponse.final_response({
            "message": (
                f"{display_title}: {(movie.get('overview') or '')[:240]}".strip()
            ),
            "tmdb_movie_id": movie.get("id"),
        })

    # ── Interactive callbacks ──────────────────────────────────────────────

    # Callbacks return the renderable content in ``context_data["inbox"]``.
    # CC consumes that block: for navigation_type=new_notification it
    # fans out to an inbox item server-side; for stack/popover it just
    # stores the result and the mobile screen polls and renders inline.

    @callback("expand_actor")
    def expand_actor(
        self, data: dict, request_info: RequestInformation,
    ) -> CommandResponse:
        actor_id = data.get("actor_id")
        if not actor_id:
            return CommandResponse.error_response("expand_actor: missing actor_id")
        api_key = _storage.get_secret("TMDB_API_KEY", scope="integration")
        if not api_key:
            return CommandResponse.error_response("TMDB API key not configured.")
        person = _person_detail(api_key, int(actor_id))
        if not person:
            return CommandResponse.error_response("Couldn't load actor.")
        return CommandResponse.final_response({
            "inbox": _person_inbox_block(person, kind="actor"),
            "tmdb_person_id": person.get("id"),
        })

    @callback("expand_director")
    def expand_director(
        self, data: dict, request_info: RequestInformation,
    ) -> CommandResponse:
        director_id = data.get("director_id")
        if not director_id:
            return CommandResponse.error_response("expand_director: missing director_id")
        api_key = _storage.get_secret("TMDB_API_KEY", scope="integration")
        if not api_key:
            return CommandResponse.error_response("TMDB API key not configured.")
        person = _person_detail(api_key, int(director_id))
        if not person:
            return CommandResponse.error_response("Couldn't load director.")
        return CommandResponse.final_response({
            "inbox": _person_inbox_block(person, kind="director"),
            "tmdb_person_id": person.get("id"),
        })

    @callback("expand_movie")
    def expand_movie(
        self, data: dict, request_info: RequestInformation,
    ) -> CommandResponse:
        """Recursive crawl from a movie id — same content shape as run()'s
        initial card, but returned as context_data so CC decides how to
        surface it based on the original tap's navigation_type."""
        movie_id = data.get("movie_id")
        if not movie_id:
            return CommandResponse.error_response("expand_movie: missing movie_id")
        api_key = _storage.get_secret("TMDB_API_KEY", scope="integration")
        if not api_key:
            return CommandResponse.error_response("TMDB API key not configured.")
        movie = _movie_detail(api_key, int(movie_id))
        if not movie:
            return CommandResponse.error_response("Couldn't load movie.")
        return CommandResponse.final_response({
            "inbox": _movie_inbox_block(movie),
            "tmdb_movie_id": movie.get("id"),
        })
