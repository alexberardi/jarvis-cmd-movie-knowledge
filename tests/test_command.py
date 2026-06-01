"""Unit tests for the movie_knowledge command.

The TMDB calls are not exercised here — those are I/O. We cover:
  - command metadata (name, params, secrets, packages)
  - example shapes (primary count, parameter keys)
  - @callback registration (verifies the three callbacks are introspectable)
  - inbox-element builders (cast / director / similar / filmography)
  - body formatting

End-to-end TMDB hits live in a manual smoke-test script (not in this file)
so CI doesn't depend on an external API + a real key.
"""

import sys
from pathlib import Path

# Make the package importable directly from the repo.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from commands.movie_knowledge.command import (  # noqa: E402
    MovieKnowledgeCommand,
    _IMDB_CRAWL_RE,
    _LOOK_UP_MOVIE_RE,
    _WHOS_IN_RE,
    _cast_elements,
    _director_element,
    _director_of,
    _filmography_elements,
    _movie_body,
    _similar_elements,
    _year,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


def _movie() -> dict:
    """Realistic-shape TMDB movie detail with credits + similar."""
    return {
        "id": 603,
        "title": "The Matrix",
        "tagline": "Welcome to the Real World",
        "overview": "A hacker discovers the world is a simulation.",
        "release_date": "1999-03-31",
        "runtime": 136,
        "vote_average": 8.2,
        "genres": [{"name": "Action"}, {"name": "Science Fiction"}],
        "credits": {
            "cast": [
                {"id": 6384, "name": "Keanu Reeves", "character": "Neo"},
                {"id": 2975, "name": "Laurence Fishburne", "character": "Morpheus"},
                {"id": 530, "name": "Carrie-Anne Moss", "character": "Trinity"},
            ],
            "crew": [
                {"id": 9339, "name": "Lana Wachowski", "job": "Director"},
                {"id": 9340, "name": "Lilly Wachowski", "job": "Director"},
                {"id": 1, "name": "Some Person", "job": "Writer"},
            ],
        },
        "similar": {
            "results": [
                {"id": 604, "title": "The Matrix Reloaded", "release_date": "2003-05-15"},
                {"id": 605, "title": "The Matrix Revolutions", "release_date": "2003-11-05"},
            ],
        },
    }


# ── Command metadata ────────────────────────────────────────────────────────


class TestCommandMetadata:
    def test_command_name(self):
        assert MovieKnowledgeCommand().command_name == "movie_knowledge"

    def test_description_mentions_movie(self):
        assert "movie" in MovieKnowledgeCommand().description.lower()

    def test_keywords_include_imdb_and_film(self):
        kw = MovieKnowledgeCommand().keywords
        assert "imdb" in kw
        assert "film" in kw

    def test_parameters_require_movie_title(self):
        params = MovieKnowledgeCommand().parameters
        assert len(params) == 1
        assert params[0].name == "movie_title"
        assert params[0].required is True

    def test_secrets_include_tmdb_api_key_and_push_toggle(self):
        secrets = {s.key: s for s in MovieKnowledgeCommand().required_secrets}
        assert "TMDB_API_KEY" in secrets
        assert secrets["TMDB_API_KEY"].scope == "integration"
        assert secrets["TMDB_API_KEY"].required is True
        # Push opt-in toggle, surfaced as a switch in the mobile UI.
        assert "MOVIE_KNOWLEDGE_PUSH_NOTIFICATIONS" in secrets
        assert secrets["MOVIE_KNOWLEDGE_PUSH_NOTIFICATIONS"].value_type == "bool"
        assert secrets["MOVIE_KNOWLEDGE_PUSH_NOTIFICATIONS"].required is False
        assert secrets["MOVIE_KNOWLEDGE_PUSH_NOTIFICATIONS"].is_sensitive is False

    def test_required_packages_includes_httpx(self):
        names = [p.name for p in MovieKnowledgeCommand().required_packages]
        assert "httpx" in names


# ── Examples ────────────────────────────────────────────────────────────────


class TestExamples:
    def test_prompt_examples_have_one_primary(self):
        examples = MovieKnowledgeCommand().generate_prompt_examples()
        assert sum(1 for ex in examples if ex.is_primary) == 1

    def test_prompt_examples_extract_movie_title_param(self):
        for ex in MovieKnowledgeCommand().generate_prompt_examples():
            assert "movie_title" in ex.expected_parameters

    def test_adapter_examples_are_plentiful(self):
        # SDK doc recommends 10-20; we generate 2 phrasings per title.
        assert len(MovieKnowledgeCommand().generate_adapter_examples()) >= 10


# ── Callback registration (the new SDK primitive) ──────────────────────────


class TestCallbacks:
    def test_all_three_callbacks_registered(self):
        cmd = MovieKnowledgeCommand()
        names = set(cmd.get_callbacks().keys())
        assert names == {"expand_actor", "expand_director", "expand_movie"}

    def test_callbacks_are_bound_methods(self):
        cmd = MovieKnowledgeCommand()
        for fn in cmd.get_callbacks().values():
            assert callable(fn)


# ── Inbox builders ──────────────────────────────────────────────────────────


class TestCastElements:
    def test_each_actor_becomes_an_interactive_element(self):
        elements = _cast_elements(_movie())
        assert len(elements) == 3
        first = elements[0]
        assert first["kind"] == "actor"
        assert first["command"] == "movie_knowledge"
        assert first["callback"] == "expand_actor"
        assert first["data"] == {"actor_id": 6384}
        # Every drill-down chip uses stack navigation — see _STACK.
        assert first["navigation_type"] == "stack"

    def test_sublabel_uses_character_name(self):
        e = _cast_elements(_movie())[0]
        assert e["sublabel"] == "as Neo"

    def test_skips_entries_without_id_or_name(self):
        bad_movie = {"credits": {"cast": [
            {"id": 1, "name": ""},
            {"name": "Nobody"},  # no id
            {"id": 2, "name": "Real Person", "character": "Self"},
        ]}}
        elements = _cast_elements(bad_movie)
        assert len(elements) == 1
        assert elements[0]["data"] == {"actor_id": 2}


class TestDirectorElement:
    def test_picks_first_director(self):
        e = _director_element(_movie())
        assert e is not None
        assert e["kind"] == "director"
        assert e["data"] == {"director_id": 9339}

    def test_none_when_no_director(self):
        movie = {"credits": {"crew": [{"id": 1, "name": "X", "job": "Writer"}]}}
        assert _director_element(movie) is None

    def test_director_of_returns_first(self):
        d = _director_of(_movie())
        assert d is not None
        assert d["name"] == "Lana Wachowski"


class TestSimilarElements:
    def test_each_similar_is_an_interactive_element(self):
        elements = _similar_elements(_movie())
        assert len(elements) == 2
        assert elements[0]["callback"] == "expand_movie"
        assert elements[0]["data"] == {"movie_id": 604}
        assert elements[0]["sublabel"] == "2003"
        assert elements[0]["navigation_type"] == "stack"


class TestCallbackReturnShape:
    """Callbacks return content via context_data['inbox'] now — they no
    longer post to jarvis-notifications themselves. CC decides the surface
    based on the original tap's navigation_type."""

    def test_expand_actor_returns_inbox_block_and_does_not_post(self):
        from unittest.mock import patch
        from commands.movie_knowledge.command import MovieKnowledgeCommand

        cmd = MovieKnowledgeCommand()
        person = {
            "id": 31, "name": "Tom Hanks", "biography": "American actor",
            "known_for_department": "Acting",
            "movie_credits": {"cast": [
                {"id": 13, "title": "Forrest Gump", "release_date": "1994-07-06"},
            ]},
        }
        with patch("commands.movie_knowledge.command._storage.get_secret", return_value="fake-key"), \
             patch("commands.movie_knowledge.command._person_detail", return_value=person), \
             patch("commands.movie_knowledge.command._post_node_inbox_item") as post_mock:
            from jarvis_command_sdk import RequestInformation
            ri = RequestInformation(voice_command="cb:expand_actor", conversation_id="c-1", user_id=7)
            resp = cmd.expand_actor({"actor_id": 31}, ri)

        assert resp.success is True
        inbox = resp.context_data["inbox"]
        assert inbox["title"].startswith("Tom Hanks")
        assert inbox["category"] == "movie_knowledge"
        assert len(inbox["metadata"]["interactive_elements"]) == 1
        # Callbacks must not fan out to inbox themselves — CC does that.
        post_mock.assert_not_called()


class TestFilmographyElements:
    def test_builds_movie_chips(self):
        films = [
            {"id": 1, "title": "A", "release_date": "2010-01-01"},
            {"id": 2, "title": "B"},  # no release date
        ]
        elements = _filmography_elements(films)
        assert len(elements) == 2
        assert elements[0]["sublabel"] == "2010"
        assert elements[1]["sublabel"] is None


# ── Body formatting ─────────────────────────────────────────────────────────


class TestMovieBody:
    def test_includes_overview_director_genres_runtime_rating(self):
        body = _movie_body(_movie())
        assert "hacker discovers" in body
        assert "Lana Wachowski" in body
        assert "Action" in body
        assert "136" in body
        assert "8.2" in body


# ── Tiny helpers ────────────────────────────────────────────────────────────


class TestYear:
    def test_year_parses_iso_date(self):
        assert _year("1999-03-31") == "1999"

    def test_year_none_for_missing(self):
        assert _year(None) is None
        assert _year("") is None
        assert _year("19") is None


class TestFastPathRegexes:
    """The pre-route regexes have to capture the title cleanly and reject
    unrelated utterances — they bypass the LLM, so a sloppy match goes
    straight to TMDB."""

    def _title(self, regex, text):
        m = regex.search(text)
        return m.group(1).strip() if m else None

    def test_look_up_movie(self):
        assert self._title(_LOOK_UP_MOVIE_RE, "look up the movie The Matrix") == "The Matrix"
        assert self._title(_LOOK_UP_MOVIE_RE, "tell me about the movie Inception") == "Inception"
        assert self._title(_LOOK_UP_MOVIE_RE, "info on the movie Dune") == "Dune"
        assert self._title(_LOOK_UP_MOVIE_RE, "information about the movie Oppenheimer.") == "Oppenheimer"
        assert self._title(_LOOK_UP_MOVIE_RE, "what is the movie Parasite?") == "Parasite"

    def test_look_up_movie_handles_whisper_comma_insertion(self):
        # Whisper produces "Look up the movie, The Matrix." for the spoken form.
        assert self._title(_LOOK_UP_MOVIE_RE, "Look up the movie, The Matrix.") == "The Matrix"
        assert self._title(_LOOK_UP_MOVIE_RE, "Tell me about the movie: Inception") == "Inception"

    def test_look_up_movie_does_not_match_unrelated(self):
        # No "movie" keyword — must miss so we don't steal "look up the weather".
        assert _LOOK_UP_MOVIE_RE.search("look up the weather in Boston") is None
        assert _LOOK_UP_MOVIE_RE.search("tell me about Boston") is None

    def test_whos_in(self):
        assert self._title(_WHOS_IN_RE, "who's in Forrest Gump") == "Forrest Gump"
        assert self._title(_WHOS_IN_RE, "who is in the movie Goodfellas") == "Goodfellas"
        assert self._title(_WHOS_IN_RE, "who stars in Pulp Fiction?") == "Pulp Fiction"

    def test_imdb_crawl_accepts_whisper_mangling(self):
        # "IMDB crawl" mishears as "I am DB crawl" / "i.m.d.b crawl" — all must hit.
        assert self._title(_IMDB_CRAWL_RE, "IMDB crawl The Matrix") == "The Matrix"
        assert self._title(_IMDB_CRAWL_RE, "I am DB crawl The Matrix") == "The Matrix"
        assert self._title(_IMDB_CRAWL_RE, "i am db crawl Forrest Gump") == "Forrest Gump"
        assert self._title(_IMDB_CRAWL_RE, "I.M.D.B. crawl Inception") == "Inception"

    def test_fast_path_handlers_return_movie_title(self):
        cmd = MovieKnowledgeCommand()
        match = _LOOK_UP_MOVIE_RE.search("look up the movie The Matrix")
        result = cmd._fp_look_up_movie(match, "look up the movie The Matrix")
        assert result is not None
        assert result.arguments == {"movie_title": "The Matrix"}

    def test_pre_route_dispatches_via_default_implementation(self):
        """End-to-end: pre_route() iterates fast_path_patterns and returns
        the handler result for the first match."""
        cmd = MovieKnowledgeCommand()
        result = cmd.pre_route("look up the movie The Matrix")
        assert result is not None
        assert result.arguments == {"movie_title": "The Matrix"}

        result2 = cmd.pre_route("I am DB crawl Dune")
        assert result2 is not None
        assert result2.arguments == {"movie_title": "Dune"}

        # No match for unrelated utterances.
        assert cmd.pre_route("what's the weather in Boston") is None
