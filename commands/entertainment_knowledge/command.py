"""Entertainment knowledge command — TMDB-powered movie and TV lookup.

A voice command produces a rich inbox item with synopsis + cast + director
(or creators, for TV) + similar titles. Each cast / creator / similar element
is tappable in the mobile app; the tap fires an `@callback` method on this
command which produces a follow-up inbox item one level deeper (the person's
combined movie + TV filmography, or a fresh crawl of the related title).

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
_settings = UserSettings("entertainment_knowledge")


def _push_enabled() -> bool:
    """Read the per-user push-notification toggle for this command.

    Defaults to False — the inbox item always lands; the push (= phone
    buzz) is opt-in via the mobile app's settings UI.
    """
    return _settings.is_enabled("push_notifications", default=False)


# ── Pre-route regexes ───────────────────────────────────────────────────────
# Deterministic phrasings that skip the LLM. The captured (?P<media>...) group
# tells the handler whether the user said "movie" or a TV variant, so we can
# route to TMDB's /movie or /tv endpoints. `_LOOK_UP_RE` requires the media
# word (otherwise we'd grab unrelated "look up X" utterances); the other two
# treat it as optional and default to movie when absent.
#
# `[\s,:.]+` is the separator between phrase fragments. Whisper occasionally
# inserts a comma or colon ("Look up the movie, The Matrix.") and that
# punctuation has to match too — using `\s+` here silently failed the most
# natural phrasings.
#
# Alternation order matters: `tv\s+show` and `tv\s+series` must come before
# `show` / `series` so Python's leftmost-first matching prefers the longer
# qualified form.

_MEDIA_GROUP = r"(?P<media>tv\s+show|tv\s+series|movie|film|show|series)"

_LOOK_UP_RE = re.compile(
    r"\b(?:look\s+up|pull\s+up|tell\s+me\s+about|info(?:rmation)?\s+(?:on|about)"
    r"|what\s+is|what'?s)"
    rf"[\s,:.]+(?:the[\s,:.]+)?{_MEDIA_GROUP}[\s,:.]+(?:called[\s,:.]+)?"
    r"(?P<title>.+?)\s*[?.!]*$",
    re.IGNORECASE,
)

_WHOS_IN_RE = re.compile(
    r"\bwho(?:'?s|\s+is|\s+stars)[\s,:.]+in[\s,:.]+"
    rf"(?:the[\s,:.]+{_MEDIA_GROUP}[\s,:.]+)?"
    r"(?P<title>.+?)\s*[?.!]*$",
    re.IGNORECASE,
)

# Whisper consistently mangles "IMDB crawl" into "I am DB crawl" / "I.M.D.B."
# / etc. We accept the plain form, the letter-by-letter form (with optional
# dots / spaces between letters), and the "i am d b" misread, with or without
# the trailing "crawl" word. The media qualifier is optional — "IMDB crawl
# the show Breaking Bad" routes to TV.
_IMDB_CRAWL_RE = re.compile(
    r"\b(?:"
    r"imdb"
    r"|i\s*\.?\s*m\s*\.?\s*d\s*\.?\s*b\.?"
    r"|i\s+am\s+d\s*b"
    r")(?:[\s,:.]+crawl)?[\s,:.]+"
    rf"(?:the[\s,:.]+{_MEDIA_GROUP}[\s,:.]+)?"
    r"(?P<title>.+?)\s*[?.!]*$",
    re.IGNORECASE,
)


def _normalize_media_type(raw: str | None) -> str:
    """Map a captured media word (or None) to 'movie' or 'tv'."""
    if not raw:
        return "movie"
    word = re.sub(r"\s+", " ", raw.strip().lower())
    if word in ("tv show", "tv series", "show", "series"):
        return "tv"
    return "movie"


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


logger = JarvisLogger(service="cmd.entertainment_knowledge")
_storage = JarvisStorage("entertainment_knowledge")


# ── TMDB constants ─────────────────────────────────────────────────────────

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMG_BASE = "https://image.tmdb.org/t/p/w185"
REQUEST_TIMEOUT = 10.0

# How many cast / similar titles to embed as interactive elements in each
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


def _search_tv(api_key: str, query: str) -> dict | None:
    """Top TMDB search result for a TV show name (or None if no match)."""
    data = _tmdb_get(api_key, "/search/tv", query=query, include_adult="false")
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


def _tv_detail(api_key: str, tv_id: int) -> dict | None:
    """Full TV show detail with credits + similar in a single TMDB call.

    TV detail also carries `created_by`, `number_of_seasons`, and
    `number_of_episodes` which the body builder picks up.
    """
    return _tmdb_get(
        api_key, f"/tv/{tv_id}",
        append_to_response="credits,similar",
    )


def _person_detail(api_key: str, person_id: int) -> dict | None:
    """Person bio + combined movie + TV credits in a single TMDB call."""
    return _tmdb_get(
        api_key, f"/person/{person_id}",
        append_to_response="combined_credits",
    )


def _director_of(movie_data: dict) -> dict | None:
    """First credited director from a TMDB movie's crew array."""
    crew = (movie_data.get("credits") or {}).get("crew") or []
    for c in crew:
        if c.get("job") == "Director":
            return c
    return None


def _creators_of(tv_data: dict) -> list[dict]:
    """Credited creators from a TMDB TV show's `created_by` array."""
    return [
        c for c in (tv_data.get("created_by") or [])
        if c.get("id") and c.get("name")
    ]


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


def _title_chip(item: dict, media_type: str) -> dict | None:
    """Build an interactive chip for a movie or TV item (similar or filmography).

    `item` is a TMDB entity — movie/tv search result, similar result, or a
    combined_credits entry. We accept either `title`+`release_date` (movie
    shape) or `name`+`first_air_date` (TV shape).
    """
    item_id = item.get("id")
    if not item_id:
        return None
    label = item.get("title") or item.get("name")
    if not label:
        return None
    year = _year(item.get("release_date") or item.get("first_air_date"))
    return {
        "id": f"{media_type}-{item_id}",
        "label": label,
        "sublabel": year,
        "kind": media_type,
        "command": "entertainment_knowledge",
        "callback": "expand_title",
        "data": {"tmdb_id": item_id, "media_type": media_type},
        "navigation_type": _STACK,
    }


def _person_chip(
    person: dict, *, kind: str, sublabel: str | None = None,
) -> dict | None:
    """Build an interactive chip for a person (cast member, director, creator).

    `kind` is informational (actor / director / creator) — the callback
    fetches the full person record either way.
    """
    pid = person.get("id")
    name = person.get("name")
    if not (pid and name):
        return None
    return {
        "id": f"person-{pid}",
        "label": name,
        "sublabel": sublabel,
        "kind": kind,
        "command": "entertainment_knowledge",
        "callback": "expand_person",
        "data": {"person_id": pid, "kind": kind},
        "navigation_type": _STACK,
    }


def _cast_elements(data: dict) -> list[dict]:
    """Top-billed cast chips (same shape for movies and TV)."""
    cast = (data.get("credits") or {}).get("cast") or []
    out: list[dict] = []
    for c in cast[:MAX_CAST]:
        chip = _person_chip(
            c,
            kind="actor",
            sublabel=(f"as {c['character']}" if c.get("character") else None),
        )
        if chip:
            out.append(chip)
    return out


def _crew_elements(data: dict, media_type: str) -> list[dict]:
    """Director(s) for movies, creator(s) for TV. May be empty for either."""
    if media_type == "tv":
        out: list[dict] = []
        for c in _creators_of(data):
            chip = _person_chip(c, kind="creator")
            if chip:
                out.append(chip)
        return out
    d = _director_of(data)
    if not d:
        return []
    chip = _person_chip(d, kind="director")
    return [chip] if chip else []


def _similar_elements(data: dict, media_type: str) -> list[dict]:
    """Similar movies (for a movie) or similar shows (for a TV)."""
    similar = (data.get("similar") or {}).get("results") or []
    out: list[dict] = []
    for s in similar[:MAX_SIMILAR]:
        chip = _title_chip(s, media_type)
        if chip:
            out.append(chip)
    return out


def _filmography_chips(works: list[dict], default_media: str = "movie") -> list[dict]:
    """Build chips for a person's filmography (mixed movie + tv).

    Each entry from TMDB combined_credits carries `media_type`; we fall
    back to `default_media` if absent.
    """
    out: list[dict] = []
    for w in works[:MAX_FILMOGRAPHY]:
        media = w.get("media_type") or default_media
        chip = _title_chip(w, media)
        if chip:
            out.append(chip)
    return out


def _title_body(data: dict, media_type: str) -> str:
    """Markdown body for a movie or TV inbox card."""
    overview = data.get("overview") or "_No synopsis available._"
    if media_type == "tv":
        creator_names = ", ".join(
            c["name"] for c in _creators_of(data) if c.get("name")
        )
        crew_line = f"**Created by:** {creator_names}\n" if creator_names else ""
        seasons = data.get("number_of_seasons")
        episodes = data.get("number_of_episodes")
        if seasons:
            ep_part = f", {episodes} episodes" if episodes else ""
            runtime_line = f"**Seasons:** {seasons}{ep_part}\n"
        else:
            runtime_line = ""
    else:
        director = _director_of(data)
        crew_line = (
            f"**Director:** {director['name']}\n"
            if director and director.get("name") else ""
        )
        runtime = data.get("runtime")
        runtime_line = f"**Runtime:** {runtime} min\n" if runtime else ""

    rating = data.get("vote_average")
    rating_line = (
        f"**TMDB rating:** {rating:.1f}/10\n"
        if isinstance(rating, (int, float)) else ""
    )
    genres = ", ".join(g["name"] for g in (data.get("genres") or []) if g.get("name"))
    genres_line = f"**Genres:** {genres}\n" if genres else ""
    return f"{overview}\n\n{crew_line}{genres_line}{runtime_line}{rating_line}".strip()


# ── Result builders (callback context_data) ────────────────────────────────
#
# Callbacks return the renderable content in ``context_data["inbox"]``. CC
# reads the original mobile tap's navigation_type and decides whether to:
#   - "new_notification": fan out to a server-side inbox item via
#     post_inbox_item_sync (the existing async surface)
#   - "stack" / "popover": just store the result; mobile polls and
#     renders inline.


def _title_inbox_block(data: dict, media_type: str) -> dict:
    """The {title, summary, body, category, metadata} block for a title crawl."""
    raw_title = (
        data.get("title")
        or data.get("name")
        or ("Show" if media_type == "tv" else "Movie")
    )
    year = _year(data.get("release_date") or data.get("first_air_date"))
    display_title = f"{raw_title} ({year})" if year else raw_title

    elements: list[dict] = []
    elements.extend(_crew_elements(data, media_type))
    elements.extend(_cast_elements(data))
    elements.extend(_similar_elements(data, media_type))

    return {
        "title": display_title,
        "summary": (data.get("tagline") or data.get("overview") or "")[:160],
        "body": _title_body(data, media_type),
        "category": "entertainment_knowledge",
        "metadata": {
            "interactive_elements": elements,
            f"tmdb_{media_type}_id": data.get("id"),
            "media_type": media_type,
            "content_format": "markdown",
        },
    }


def _filter_person_works(credits: dict, kind: str) -> list[dict]:
    """Slice combined_credits into the works relevant for a person kind.

    actor    -> all cast credits (movies + TV)
    director -> crew where job=Director (almost exclusively movies)
    creator  -> TV crew where job is Creator / Executive Producer / Series Director
    """
    if kind == "director":
        return [
            c for c in (credits.get("crew") or [])
            if c.get("job") == "Director"
            and (c.get("media_type") or "movie") == "movie"
        ]
    if kind == "creator":
        creator_jobs = {"Creator", "Executive Producer", "Series Director"}
        return [
            c for c in (credits.get("crew") or [])
            if c.get("media_type") == "tv" and c.get("job") in creator_jobs
        ]
    return list(credits.get("cast") or [])


def _person_inbox_block(person: dict, *, kind: str) -> dict:
    """The {title, summary, body, category, metadata} block for a person crawl.

    Uses combined_credits so a person who works across film + TV (e.g. an
    actor with a TV show, or a director who also created a series) is
    represented honestly.
    """
    name = person.get("name") or "Unknown"
    bio = person.get("biography") or "_No bio available._"
    credits = person.get("combined_credits") or {}
    works = _filter_person_works(credits, kind)
    works.sort(
        key=lambda w: (w.get("release_date") or w.get("first_air_date") or ""),
        reverse=True,
    )

    title_suffix = {
        "actor": "Films & TV",
        "director": "Directed",
        "creator": "Created",
    }.get(kind, "Filmography")

    return {
        "title": f"{name} — {title_suffix}",
        "summary": (person.get("known_for_department") or "").strip()[:160],
        "body": bio[:1200],
        "category": "entertainment_knowledge",
        "metadata": {
            "interactive_elements": _filmography_chips(works),
            "tmdb_person_id": person.get("id"),
            "person_kind": kind,
            "content_format": "markdown",
        },
    }


def _emit_title_inbox(
    data: dict, media_type: str, *, user_id: int | None,
) -> str | None:
    """Build + POST the *initial* inbox item produced by a voice crawl.

    Used by run() only — callbacks return content via context_data instead.
    No household_id / node_id in the payload — CC resolves them from the
    authenticated node and injects node_id into metadata server-side.
    """
    block = _title_inbox_block(data, media_type)
    return _post_node_inbox_item({"user_id": user_id, **block})


# ── Command class ──────────────────────────────────────────────────────────


class EntertainmentKnowledgeCommand(IJarvisCommand):

    @property
    def command_name(self) -> str:
        return "entertainment_knowledge"

    @property
    def description(self) -> str:
        return (
            "Look up a movie or TV show by title and produce a rich inbox "
            "card with synopsis, cast, director (or creators for TV), and "
            "similar titles — each tappable to drill one level deeper into "
            "a person's filmography or a related title. Use when the user "
            "asks to look up, get info on, learn about, or explore a movie "
            "or show. Distinct from a ratings lookup."
        )

    @property
    def keywords(self) -> List[str]:
        return [
            "movie", "movies", "film", "imdb", "tmdb",
            "tv", "tv show", "show", "shows", "tv series", "series", "television",
            "look up", "info", "info on", "about the movie", "about the show",
            "tell me about",
            "cast", "actor", "actors", "director", "creator", "creators",
            "synopsis", "starring",
            "who is in", "who stars in",
        ]

    @property
    def parameters(self) -> List[JarvisParameter]:
        return [
            JarvisParameter(
                name="title",
                param_type="string",
                required=True,
                description="The movie or TV show title to look up.",
            ),
            JarvisParameter(
                name="media_type",
                param_type="string",
                required=False,
                enum_values=["movie", "tv"],
                default="movie",
                description=(
                    "Whether `title` is a movie or a TV show. Defaults to "
                    "movie when ambiguous."
                ),
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
                key="ENTERTAINMENT_KNOWLEDGE_PUSH_NOTIFICATIONS",
                description=(
                    "Send a push notification when a movie or TV crawl card "
                    "appears in the inbox."
                ),
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
                expected_parameters={"title": "The Matrix", "media_type": "movie"},
                is_primary=True,
            ),
            CommandExample(
                voice_command="tell me about the movie Inception",
                expected_parameters={"title": "Inception", "media_type": "movie"},
            ),
            CommandExample(
                voice_command="who's in the movie Forrest Gump",
                expected_parameters={"title": "Forrest Gump", "media_type": "movie"},
            ),
            CommandExample(
                voice_command="look up the show Breaking Bad",
                expected_parameters={"title": "Breaking Bad", "media_type": "tv"},
            ),
            CommandExample(
                voice_command="tell me about the tv show Severance",
                expected_parameters={"title": "Severance", "media_type": "tv"},
            ),
            CommandExample(
                voice_command="who's in the series The Bear",
                expected_parameters={"title": "The Bear", "media_type": "tv"},
            ),
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        movie_titles = [
            "The Matrix", "Inception", "Forrest Gump", "Pulp Fiction",
            "The Godfather", "Dune", "Oppenheimer", "Parasite",
            "Everything Everywhere All At Once", "Spirited Away",
            "The Shawshank Redemption", "Goodfellas",
        ]
        tv_titles = [
            "Breaking Bad", "Severance", "The Bear", "Succession",
            "The Sopranos", "Game of Thrones", "The Wire", "Mad Men",
            "Better Call Saul", "Arcane",
        ]
        out: list[CommandExample] = []
        for t in movie_titles:
            out.append(CommandExample(
                voice_command=f"look up the movie {t}",
                expected_parameters={"title": t, "media_type": "movie"},
            ))
            out.append(CommandExample(
                voice_command=f"tell me about the movie {t}",
                expected_parameters={"title": t, "media_type": "movie"},
            ))
        for t in tv_titles:
            out.append(CommandExample(
                voice_command=f"look up the show {t}",
                expected_parameters={"title": t, "media_type": "tv"},
            ))
            out.append(CommandExample(
                voice_command=f"tell me about the tv show {t}",
                expected_parameters={"title": t, "media_type": "tv"},
            ))
            out.append(CommandExample(
                voice_command=f"who's in the series {t}",
                expected_parameters={"title": t, "media_type": "tv"},
            ))
        return out

    # ── Fast-path patterns: skip the LLM for deterministic phrasings ───────

    @property
    def fast_path_patterns(self) -> List[FastPathPattern]:
        return [
            FastPathPattern(
                id="entertainment_knowledge.look_up",
                description=(
                    "Bypass LLM for 'look up / tell me about the "
                    "movie|show|series X'"
                ),
                example="look up the movie The Matrix",
                regex=_LOOK_UP_RE.pattern,
                handler="_fp_extract_title_and_media",
            ),
            FastPathPattern(
                id="entertainment_knowledge.whos_in",
                description=(
                    "Bypass LLM for 'who's in X' / 'who stars in X' "
                    "(media qualifier optional; defaults to movie)"
                ),
                example="who's in Forrest Gump",
                regex=_WHOS_IN_RE.pattern,
                handler="_fp_extract_title_and_media",
            ),
            FastPathPattern(
                id="entertainment_knowledge.imdb_crawl",
                description=(
                    "Bypass LLM for 'IMDB crawl X' (Whisper often mishears "
                    "this as 'I am DB crawl' — we accept both)"
                ),
                example="IMDB crawl The Matrix",
                regex=_IMDB_CRAWL_RE.pattern,
                handler="_fp_extract_title_and_media",
            ),
        ]

    def _fp_extract_title_and_media(
        self, match: "re.Match[str]", voice_command: str,
    ) -> PreRouteResult | None:
        title = (match.group("title") or "").strip()
        if not title:
            return None
        # `media` capture may be absent in regexes where it's an optional
        # branch; re raises IndexError rather than returning None for groups
        # that never participated in the match.
        try:
            media = _normalize_media_type(match.group("media"))
        except IndexError:
            media = "movie"
        return PreRouteResult(arguments={"title": title, "media_type": media})

    def run(self, request_info: RequestInformation, **kwargs) -> CommandResponse:
        title = (kwargs.get("title") or "").strip()
        if not title:
            return CommandResponse.error_response("Which title should I look up?")
        media_type = (kwargs.get("media_type") or "movie").strip().lower()
        if media_type not in ("movie", "tv"):
            media_type = "movie"

        api_key = _storage.get_secret("TMDB_API_KEY", scope="integration")
        if not api_key:
            return CommandResponse.error_response(
                "TMDB API key not configured. Add it in mobile settings.",
            )

        search = _search_tv if media_type == "tv" else _search_movie
        kind_label = "show" if media_type == "tv" else "movie"
        hit = search(api_key, title)
        if not hit or not hit.get("id"):
            return CommandResponse.success_response(
                {"message": f"I couldn't find a {kind_label} called '{title}' on TMDB."},
                wait_for_input=False,
            )

        detail_fn = _tv_detail if media_type == "tv" else _movie_detail
        detail = detail_fn(api_key, hit["id"])
        if not detail:
            return CommandResponse.error_response(
                f"Couldn't load details for '{title}'.",
            )

        inbox_id = _emit_title_inbox(detail, media_type, user_id=request_info.user_id)
        display_title = detail.get("title") or detail.get("name") or title
        if inbox_id:
            return CommandResponse.final_response({
                "message": f"Added {display_title} to your inbox — tap an actor to see more.",
                "inbox_item_id": inbox_id,
                f"tmdb_{media_type}_id": detail.get("id"),
            })
        # Inbox post failed — still answer the user with a brief reply.
        return CommandResponse.final_response({
            "message": (
                f"{display_title}: {(detail.get('overview') or '')[:240]}".strip()
            ),
            f"tmdb_{media_type}_id": detail.get("id"),
        })

    # ── Interactive callbacks ──────────────────────────────────────────────

    # Callbacks return the renderable content in ``context_data["inbox"]``.
    # CC consumes that block: for navigation_type=new_notification it
    # fans out to an inbox item server-side; for stack/popover it just
    # stores the result and the mobile screen polls and renders inline.

    @callback("expand_title")
    def expand_title(
        self, data: dict, request_info: RequestInformation,
    ) -> CommandResponse:
        """Crawl from a movie or TV id — used by similar-title and
        filmography chips. Same content shape as run()'s initial card."""
        tmdb_id = data.get("tmdb_id")
        if not tmdb_id:
            return CommandResponse.error_response("expand_title: missing tmdb_id")
        media_type = (data.get("media_type") or "movie").strip().lower()
        if media_type not in ("movie", "tv"):
            media_type = "movie"
        api_key = _storage.get_secret("TMDB_API_KEY", scope="integration")
        if not api_key:
            return CommandResponse.error_response("TMDB API key not configured.")
        detail_fn = _tv_detail if media_type == "tv" else _movie_detail
        detail = detail_fn(api_key, int(tmdb_id))
        if not detail:
            return CommandResponse.error_response(
                f"Couldn't load {'show' if media_type == 'tv' else 'movie'}.",
            )
        return CommandResponse.final_response({
            "inbox": _title_inbox_block(detail, media_type),
            f"tmdb_{media_type}_id": detail.get("id"),
        })

    @callback("expand_person")
    def expand_person(
        self, data: dict, request_info: RequestInformation,
    ) -> CommandResponse:
        """Crawl from a person id — used by actor / director / creator chips."""
        person_id = data.get("person_id")
        if not person_id:
            return CommandResponse.error_response("expand_person: missing person_id")
        kind = (data.get("kind") or "actor").strip().lower()
        if kind not in ("actor", "director", "creator"):
            kind = "actor"
        api_key = _storage.get_secret("TMDB_API_KEY", scope="integration")
        if not api_key:
            return CommandResponse.error_response("TMDB API key not configured.")
        person = _person_detail(api_key, int(person_id))
        if not person:
            return CommandResponse.error_response("Couldn't load person.")
        return CommandResponse.final_response({
            "inbox": _person_inbox_block(person, kind=kind),
            "tmdb_person_id": person.get("id"),
        })
