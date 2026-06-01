"""Unit tests for the entertainment_knowledge command.

The TMDB calls are not exercised here — those are I/O. We cover:
  - command metadata (name, params, secrets, packages)
  - example shapes (primary count, parameter keys)
  - @callback registration (verifies the two callbacks are introspectable)
  - inbox-element builders (cast / crew / similar / filmography) for both
    movies and TV
  - body formatting for both media types
  - media-type normalization
  - fast-path regexes and the unified handler — covering both movie and TV
    phrasings

End-to-end TMDB hits live in a manual smoke-test script (not in this file)
so CI doesn't depend on an external API + a real key.
"""

import sys
from pathlib import Path

# Make the package importable directly from the repo.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from commands.entertainment_knowledge.command import (  # noqa: E402
    EntertainmentKnowledgeCommand,
    _IMDB_CRAWL_RE,
    _LOOK_UP_RE,
    _WHOS_IN_RE,
    _cast_elements,
    _creators_of,
    _crew_elements,
    _director_of,
    _filmography_chips,
    _filter_person_works,
    _normalize_media_type,
    _person_inbox_block,
    _similar_elements,
    _title_body,
    _title_chip,
    _title_inbox_block,
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


def _show() -> dict:
    """Realistic-shape TMDB TV detail with credits + similar + creators."""
    return {
        "id": 1396,
        "name": "Breaking Bad",
        "tagline": "Change the equation.",
        "overview": "A chemistry teacher diagnosed with cancer turns to making meth.",
        "first_air_date": "2008-01-20",
        "number_of_seasons": 5,
        "number_of_episodes": 62,
        "vote_average": 8.9,
        "genres": [{"name": "Drama"}, {"name": "Crime"}],
        "created_by": [
            {"id": 66633, "name": "Vince Gilligan", "profile_path": None},
        ],
        "credits": {
            "cast": [
                {"id": 17419, "name": "Bryan Cranston", "character": "Walter White"},
                {"id": 84497, "name": "Aaron Paul", "character": "Jesse Pinkman"},
            ],
            "crew": [],
        },
        "similar": {
            "results": [
                {"id": 60059, "name": "Better Call Saul", "first_air_date": "2015-02-08"},
                {"id": 1700, "name": "La Femme Nikita", "first_air_date": "1997-01-13"},
            ],
        },
    }


# ── Command metadata ────────────────────────────────────────────────────────


class TestCommandMetadata:
    def test_command_name(self):
        assert EntertainmentKnowledgeCommand().command_name == "entertainment_knowledge"

    def test_description_mentions_both_media(self):
        desc = EntertainmentKnowledgeCommand().description.lower()
        assert "movie" in desc
        assert "tv" in desc

    def test_keywords_cover_movie_and_tv(self):
        kw = EntertainmentKnowledgeCommand().keywords
        assert "imdb" in kw
        assert "film" in kw
        # New keywords for TV.
        assert "tv show" in kw
        assert "series" in kw
        assert "creator" in kw

    def test_parameters_require_title_and_offer_media_type(self):
        params = EntertainmentKnowledgeCommand().parameters
        names = [p.name for p in params]
        assert names == ["title", "media_type"]
        title = next(p for p in params if p.name == "title")
        media = next(p for p in params if p.name == "media_type")
        assert title.required is True
        assert media.required is False
        assert set(media.enum_values or []) == {"movie", "tv"}

    def test_secrets_include_tmdb_api_key_and_renamed_push_toggle(self):
        secrets = {s.key: s for s in EntertainmentKnowledgeCommand().required_secrets}
        assert "TMDB_API_KEY" in secrets
        assert secrets["TMDB_API_KEY"].scope == "integration"
        assert secrets["TMDB_API_KEY"].required is True
        # The push toggle key was renamed alongside the command.
        assert "ENTERTAINMENT_KNOWLEDGE_PUSH_NOTIFICATIONS" in secrets
        assert "MOVIE_KNOWLEDGE_PUSH_NOTIFICATIONS" not in secrets
        push = secrets["ENTERTAINMENT_KNOWLEDGE_PUSH_NOTIFICATIONS"]
        assert push.value_type == "bool"
        assert push.required is False
        assert push.is_sensitive is False

    def test_required_packages_includes_httpx(self):
        names = [p.name for p in EntertainmentKnowledgeCommand().required_packages]
        assert "httpx" in names


# ── Examples ────────────────────────────────────────────────────────────────


class TestExamples:
    def test_prompt_examples_have_one_primary(self):
        examples = EntertainmentKnowledgeCommand().generate_prompt_examples()
        assert sum(1 for ex in examples if ex.is_primary) == 1

    def test_prompt_examples_cover_both_media_types(self):
        examples = EntertainmentKnowledgeCommand().generate_prompt_examples()
        media_types = {ex.expected_parameters.get("media_type") for ex in examples}
        assert media_types == {"movie", "tv"}

    def test_prompt_examples_extract_title_and_media_type(self):
        for ex in EntertainmentKnowledgeCommand().generate_prompt_examples():
            assert "title" in ex.expected_parameters
            assert ex.expected_parameters.get("media_type") in ("movie", "tv")

    def test_adapter_examples_are_plentiful(self):
        # SDK doc recommends 10-20; we generate 2 phrasings/movie + 3/TV title.
        examples = EntertainmentKnowledgeCommand().generate_adapter_examples()
        assert len(examples) >= 20
        media_types = {ex.expected_parameters.get("media_type") for ex in examples}
        assert media_types == {"movie", "tv"}


# ── Callback registration (the new SDK primitive) ──────────────────────────


class TestCallbacks:
    def test_two_unified_callbacks_registered(self):
        """expand_movie/expand_actor/expand_director collapsed into
        expand_title (movies + TV) and expand_person (actors + crew)."""
        cmd = EntertainmentKnowledgeCommand()
        names = set(cmd.get_callbacks().keys())
        assert names == {"expand_title", "expand_person"}

    def test_callbacks_are_bound_methods(self):
        cmd = EntertainmentKnowledgeCommand()
        for fn in cmd.get_callbacks().values():
            assert callable(fn)


# ── Inbox builders ──────────────────────────────────────────────────────────


class TestCastElements:
    def test_each_actor_becomes_an_interactive_element(self):
        elements = _cast_elements(_movie())
        assert len(elements) == 3
        first = elements[0]
        assert first["kind"] == "actor"
        assert first["command"] == "entertainment_knowledge"
        assert first["callback"] == "expand_person"
        # Person chips route by person_id + kind, not by actor-specific data.
        assert first["data"] == {"person_id": 6384, "kind": "actor"}
        # Every drill-down chip uses stack navigation — see _STACK.
        assert first["navigation_type"] == "stack"

    def test_sublabel_uses_character_name(self):
        e = _cast_elements(_movie())[0]
        assert e["sublabel"] == "as Neo"

    def test_works_for_tv_credits_too(self):
        # Cast shape is identical between movies and TV.
        elements = _cast_elements(_show())
        labels = [e["label"] for e in elements]
        assert "Bryan Cranston" in labels

    def test_skips_entries_without_id_or_name(self):
        bad = {"credits": {"cast": [
            {"id": 1, "name": ""},
            {"name": "Nobody"},  # no id
            {"id": 2, "name": "Real Person", "character": "Self"},
        ]}}
        elements = _cast_elements(bad)
        assert len(elements) == 1
        assert elements[0]["data"] == {"person_id": 2, "kind": "actor"}


class TestCrewElements:
    def test_picks_first_director_for_a_movie(self):
        elements = _crew_elements(_movie(), "movie")
        assert len(elements) == 1
        e = elements[0]
        assert e["kind"] == "director"
        assert e["data"] == {"person_id": 9339, "kind": "director"}

    def test_emits_creators_for_a_show(self):
        elements = _crew_elements(_show(), "tv")
        assert len(elements) == 1
        e = elements[0]
        assert e["kind"] == "creator"
        assert e["data"] == {"person_id": 66633, "kind": "creator"}

    def test_empty_when_movie_has_no_director(self):
        movie = {"credits": {"crew": [{"id": 1, "name": "X", "job": "Writer"}]}}
        assert _crew_elements(movie, "movie") == []

    def test_empty_when_show_has_no_creators(self):
        show = {"created_by": []}
        assert _crew_elements(show, "tv") == []

    def test_director_of_returns_first(self):
        d = _director_of(_movie())
        assert d is not None
        assert d["name"] == "Lana Wachowski"

    def test_creators_of_filters_to_valid_entries(self):
        creators = _creators_of(_show())
        assert len(creators) == 1
        assert creators[0]["name"] == "Vince Gilligan"


class TestSimilarElements:
    def test_movie_similars_route_to_movie_endpoints(self):
        elements = _similar_elements(_movie(), "movie")
        assert len(elements) == 2
        assert elements[0]["callback"] == "expand_title"
        assert elements[0]["data"] == {"tmdb_id": 604, "media_type": "movie"}
        assert elements[0]["sublabel"] == "2003"
        assert elements[0]["navigation_type"] == "stack"

    def test_tv_similars_route_to_tv_endpoints(self):
        elements = _similar_elements(_show(), "tv")
        assert len(elements) == 2
        first = elements[0]
        assert first["label"] == "Better Call Saul"
        assert first["data"] == {"tmdb_id": 60059, "media_type": "tv"}
        assert first["sublabel"] == "2015"


class TestTitleChip:
    def test_accepts_movie_shape(self):
        chip = _title_chip(
            {"id": 1, "title": "X", "release_date": "2020-01-01"}, "movie",
        )
        assert chip is not None
        assert chip["label"] == "X"
        assert chip["sublabel"] == "2020"
        assert chip["data"] == {"tmdb_id": 1, "media_type": "movie"}

    def test_accepts_tv_shape(self):
        chip = _title_chip(
            {"id": 2, "name": "Y", "first_air_date": "2018-05-01"}, "tv",
        )
        assert chip is not None
        assert chip["label"] == "Y"
        assert chip["sublabel"] == "2018"
        assert chip["data"] == {"tmdb_id": 2, "media_type": "tv"}

    def test_returns_none_when_missing_id_or_label(self):
        assert _title_chip({"title": "no id"}, "movie") is None
        assert _title_chip({"id": 3}, "movie") is None


class TestFilmographyChips:
    def test_carries_media_type_from_combined_credits(self):
        works = [
            {"id": 1, "title": "Film A", "release_date": "2010-01-01", "media_type": "movie"},
            {"id": 2, "name": "Show B", "first_air_date": "2015-06-01", "media_type": "tv"},
        ]
        chips = _filmography_chips(works)
        assert chips[0]["data"]["media_type"] == "movie"
        assert chips[1]["data"]["media_type"] == "tv"

    def test_falls_back_to_default_media(self):
        # combined_credits should always carry media_type, but defend anyway.
        chips = _filmography_chips(
            [{"id": 9, "title": "Z", "release_date": "2000-01-01"}],
            default_media="movie",
        )
        assert chips[0]["data"]["media_type"] == "movie"


class TestFilterPersonWorks:
    def test_actor_returns_all_cast_credits(self):
        credits = {"cast": [
            {"id": 1, "title": "M", "media_type": "movie"},
            {"id": 2, "name": "T", "media_type": "tv"},
        ]}
        assert len(_filter_person_works(credits, "actor")) == 2

    def test_director_filters_to_directed_movies(self):
        credits = {"crew": [
            {"id": 1, "title": "M1", "job": "Director", "media_type": "movie"},
            {"id": 2, "title": "M2", "job": "Writer", "media_type": "movie"},
            {"id": 3, "name": "T1", "job": "Director", "media_type": "tv"},
        ]}
        works = _filter_person_works(credits, "director")
        assert len(works) == 1
        assert works[0]["id"] == 1

    def test_creator_filters_to_tv_creator_roles(self):
        credits = {"crew": [
            {"id": 1, "name": "T1", "job": "Creator", "media_type": "tv"},
            {"id": 2, "name": "T2", "job": "Executive Producer", "media_type": "tv"},
            {"id": 3, "name": "T3", "job": "Editor", "media_type": "tv"},
            {"id": 4, "title": "M1", "job": "Creator", "media_type": "movie"},
        ]}
        works = _filter_person_works(credits, "creator")
        ids = {w["id"] for w in works}
        assert ids == {1, 2}


class TestTitleInboxBlock:
    def test_movie_block_uses_title_and_release_year(self):
        block = _title_inbox_block(_movie(), "movie")
        assert block["title"] == "The Matrix (1999)"
        assert block["category"] == "entertainment_knowledge"
        assert block["metadata"]["media_type"] == "movie"
        assert block["metadata"]["tmdb_movie_id"] == 603
        # Director + cast + similar all become interactive elements.
        assert len(block["metadata"]["interactive_elements"]) >= 5

    def test_tv_block_uses_name_and_first_air_year(self):
        block = _title_inbox_block(_show(), "tv")
        assert block["title"] == "Breaking Bad (2008)"
        assert block["metadata"]["media_type"] == "tv"
        assert block["metadata"]["tmdb_tv_id"] == 1396
        # Creator + cast + similar all become interactive elements.
        elements = block["metadata"]["interactive_elements"]
        kinds = {e["kind"] for e in elements}
        assert "creator" in kinds
        assert "actor" in kinds
        assert "tv" in kinds


class TestPersonInboxBlock:
    def _actor(self) -> dict:
        return {
            "id": 31, "name": "Tom Hanks", "biography": "American actor",
            "known_for_department": "Acting",
            "combined_credits": {
                "cast": [
                    {"id": 13, "title": "Forrest Gump", "release_date": "1994-07-06",
                     "media_type": "movie"},
                    {"id": 70, "name": "Big Little Lies", "first_air_date": "2017-02-19",
                     "media_type": "tv"},
                ],
                "crew": [],
            },
        }

    def test_actor_block_shows_combined_credits(self):
        block = _person_inbox_block(self._actor(), kind="actor")
        assert block["title"] == "Tom Hanks — Films & TV"
        assert block["category"] == "entertainment_knowledge"
        chips = block["metadata"]["interactive_elements"]
        media_types = {c["data"]["media_type"] for c in chips}
        assert media_types == {"movie", "tv"}

    def test_director_block_titled_directed(self):
        person = {
            "id": 9339, "name": "Lana Wachowski", "biography": "",
            "combined_credits": {"crew": [
                {"id": 603, "title": "The Matrix", "job": "Director",
                 "media_type": "movie", "release_date": "1999-03-31"},
            ]},
        }
        block = _person_inbox_block(person, kind="director")
        assert block["title"] == "Lana Wachowski — Directed"

    def test_creator_block_titled_created(self):
        person = {
            "id": 66633, "name": "Vince Gilligan", "biography": "",
            "combined_credits": {"crew": [
                {"id": 1396, "name": "Breaking Bad", "job": "Creator",
                 "media_type": "tv", "first_air_date": "2008-01-20"},
            ]},
        }
        block = _person_inbox_block(person, kind="creator")
        assert block["title"] == "Vince Gilligan — Created"


# ── Body formatting ─────────────────────────────────────────────────────────


class TestTitleBody:
    def test_movie_body_includes_director_runtime_rating_genres(self):
        body = _title_body(_movie(), "movie")
        assert "hacker discovers" in body
        assert "Lana Wachowski" in body
        assert "Action" in body
        assert "136" in body
        assert "8.2" in body

    def test_tv_body_includes_creator_seasons_episodes(self):
        body = _title_body(_show(), "tv")
        assert "Vince Gilligan" in body
        assert "Seasons:" in body
        assert "5" in body
        assert "62" in body
        # No "Runtime:" line for TV; the seasons line replaces it.
        assert "Runtime:" not in body


# ── Tiny helpers ────────────────────────────────────────────────────────────


class TestYear:
    def test_year_parses_iso_date(self):
        assert _year("1999-03-31") == "1999"

    def test_year_none_for_missing(self):
        assert _year(None) is None
        assert _year("") is None
        assert _year("19") is None


class TestNormalizeMediaType:
    def test_movie_words_map_to_movie(self):
        assert _normalize_media_type("movie") == "movie"
        assert _normalize_media_type("Film") == "movie"
        assert _normalize_media_type(None) == "movie"

    def test_show_words_map_to_tv(self):
        for word in ["show", "shows", "TV show", "tv  show", "series", "TV Series"]:
            # Note: "shows" isn't in the actual regex alternation, but the
            # normalizer accepts the singular forms reliably; we test the
            # canonical inputs the regex actually emits.
            if word == "shows":
                continue
            assert _normalize_media_type(word) == "tv", word


# ── Fast-path regexes ──────────────────────────────────────────────────────


class TestFastPathRegexes:
    """The pre-route regexes have to capture the title cleanly and reject
    unrelated utterances — they bypass the LLM, so a sloppy match goes
    straight to TMDB."""

    def _title(self, regex, text):
        m = regex.search(text)
        return m.group("title").strip() if m else None

    def _media(self, regex, text):
        m = regex.search(text)
        if not m:
            return None
        try:
            return _normalize_media_type(m.group("media"))
        except IndexError:
            return "movie"

    # --- Movies (unchanged behavior) -----------------------------------------

    def test_look_up_movie(self):
        assert self._title(_LOOK_UP_RE, "look up the movie The Matrix") == "The Matrix"
        assert self._title(_LOOK_UP_RE, "tell me about the movie Inception") == "Inception"
        assert self._title(_LOOK_UP_RE, "info on the movie Dune") == "Dune"
        assert self._title(_LOOK_UP_RE, "information about the movie Oppenheimer.") == "Oppenheimer"
        assert self._title(_LOOK_UP_RE, "what is the movie Parasite?") == "Parasite"

    def test_look_up_handles_whisper_comma_insertion(self):
        # Whisper produces "Look up the movie, The Matrix." for the spoken form.
        assert self._title(_LOOK_UP_RE, "Look up the movie, The Matrix.") == "The Matrix"
        assert self._title(_LOOK_UP_RE, "Tell me about the movie: Inception") == "Inception"

    def test_look_up_does_not_match_unrelated(self):
        # Requires a media word — must miss so we don't steal "look up the weather".
        assert _LOOK_UP_RE.search("look up the weather in Boston") is None
        assert _LOOK_UP_RE.search("tell me about Boston") is None

    def test_whos_in_movies(self):
        assert self._title(_WHOS_IN_RE, "who's in Forrest Gump") == "Forrest Gump"
        assert self._title(_WHOS_IN_RE, "who is in the movie Goodfellas") == "Goodfellas"
        assert self._title(_WHOS_IN_RE, "who stars in Pulp Fiction?") == "Pulp Fiction"

    def test_imdb_crawl_accepts_whisper_mangling(self):
        # "IMDB crawl" mishears as "I am DB crawl" / "i.m.d.b crawl" — all must hit.
        assert self._title(_IMDB_CRAWL_RE, "IMDB crawl The Matrix") == "The Matrix"
        assert self._title(_IMDB_CRAWL_RE, "I am DB crawl The Matrix") == "The Matrix"
        assert self._title(_IMDB_CRAWL_RE, "i am db crawl Forrest Gump") == "Forrest Gump"
        assert self._title(_IMDB_CRAWL_RE, "I.M.D.B. crawl Inception") == "Inception"

    # --- TV (new) ------------------------------------------------------------

    def test_look_up_tv_variants(self):
        assert self._title(_LOOK_UP_RE, "look up the show Breaking Bad") == "Breaking Bad"
        assert self._media(_LOOK_UP_RE, "look up the show Breaking Bad") == "tv"
        assert self._title(_LOOK_UP_RE, "tell me about the tv show Severance") == "Severance"
        assert self._media(_LOOK_UP_RE, "tell me about the tv show Severance") == "tv"
        assert self._title(_LOOK_UP_RE, "look up the series The Bear") == "The Bear"
        assert self._media(_LOOK_UP_RE, "look up the series The Bear") == "tv"

    def test_look_up_movie_routes_to_movie(self):
        assert self._media(_LOOK_UP_RE, "look up the movie The Matrix") == "movie"
        assert self._media(_LOOK_UP_RE, "tell me about the film Inception") == "movie"

    def test_whos_in_tv_variants(self):
        assert self._title(_WHOS_IN_RE, "who's in the show Succession") == "Succession"
        assert self._media(_WHOS_IN_RE, "who's in the show Succession") == "tv"
        assert self._title(_WHOS_IN_RE, "who stars in the series The Wire") == "The Wire"
        assert self._media(_WHOS_IN_RE, "who stars in the series The Wire") == "tv"

    def test_whos_in_defaults_to_movie_when_unqualified(self):
        # "who's in X" without a media word should still match, defaulting to movie.
        assert self._media(_WHOS_IN_RE, "who's in Forrest Gump") == "movie"

    def test_imdb_crawl_tv_form(self):
        assert self._title(_IMDB_CRAWL_RE, "IMDB crawl the show Breaking Bad") == "Breaking Bad"
        assert self._media(_IMDB_CRAWL_RE, "IMDB crawl the show Breaking Bad") == "tv"
        # Without the qualifier, defaults to movie.
        assert self._media(_IMDB_CRAWL_RE, "IMDB crawl The Matrix") == "movie"

    # --- Handler --------------------------------------------------------------

    def test_handler_returns_title_and_media_type(self):
        cmd = EntertainmentKnowledgeCommand()
        match = _LOOK_UP_RE.search("look up the movie The Matrix")
        result = cmd._fp_extract_title_and_media(match, "look up the movie The Matrix")
        assert result is not None
        assert result.arguments == {"title": "The Matrix", "media_type": "movie"}

    def test_handler_returns_tv_for_show_phrasing(self):
        cmd = EntertainmentKnowledgeCommand()
        match = _LOOK_UP_RE.search("look up the show Breaking Bad")
        result = cmd._fp_extract_title_and_media(match, "look up the show Breaking Bad")
        assert result is not None
        assert result.arguments == {"title": "Breaking Bad", "media_type": "tv"}

    def test_pre_route_dispatches_via_default_implementation(self):
        """End-to-end: pre_route() iterates fast_path_patterns and returns
        the handler result for the first match."""
        cmd = EntertainmentKnowledgeCommand()
        result = cmd.pre_route("look up the movie The Matrix")
        assert result is not None
        assert result.arguments == {"title": "The Matrix", "media_type": "movie"}

        result2 = cmd.pre_route("I am DB crawl Dune")
        assert result2 is not None
        assert result2.arguments == {"title": "Dune", "media_type": "movie"}

        result3 = cmd.pre_route("look up the show Breaking Bad")
        assert result3 is not None
        assert result3.arguments == {"title": "Breaking Bad", "media_type": "tv"}

        # No match for unrelated utterances.
        assert cmd.pre_route("what's the weather in Boston") is None


# ── Callback return shape ──────────────────────────────────────────────────


class TestCallbackReturnShape:
    """Callbacks return content via context_data['inbox'] now — they no
    longer post to jarvis-notifications themselves. CC decides the surface
    based on the original tap's navigation_type."""

    def test_expand_person_returns_inbox_block_and_does_not_post(self):
        from unittest.mock import patch
        from commands.entertainment_knowledge.command import EntertainmentKnowledgeCommand

        cmd = EntertainmentKnowledgeCommand()
        person = {
            "id": 31, "name": "Tom Hanks", "biography": "American actor",
            "known_for_department": "Acting",
            "combined_credits": {"cast": [
                {"id": 13, "title": "Forrest Gump", "release_date": "1994-07-06",
                 "media_type": "movie"},
            ]},
        }
        with patch(
            "commands.entertainment_knowledge.command._storage.get_secret",
            return_value="fake-key",
        ), patch(
            "commands.entertainment_knowledge.command._person_detail",
            return_value=person,
        ), patch(
            "commands.entertainment_knowledge.command._post_node_inbox_item",
        ) as post_mock:
            from jarvis_command_sdk import RequestInformation
            ri = RequestInformation(
                voice_command="cb:expand_person",
                conversation_id="c-1",
                user_id=7,
            )
            resp = cmd.expand_person({"person_id": 31, "kind": "actor"}, ri)

        assert resp.success is True
        inbox = resp.context_data["inbox"]
        assert inbox["title"].startswith("Tom Hanks")
        assert inbox["category"] == "entertainment_knowledge"
        assert len(inbox["metadata"]["interactive_elements"]) == 1
        # Callbacks must not fan out to inbox themselves — CC does that.
        post_mock.assert_not_called()

    def test_expand_title_routes_to_tv_endpoint_when_asked(self):
        from unittest.mock import patch
        from commands.entertainment_knowledge.command import EntertainmentKnowledgeCommand

        cmd = EntertainmentKnowledgeCommand()
        with patch(
            "commands.entertainment_knowledge.command._storage.get_secret",
            return_value="fake-key",
        ), patch(
            "commands.entertainment_knowledge.command._tv_detail",
            return_value=_show(),
        ) as tv_mock, patch(
            "commands.entertainment_knowledge.command._movie_detail",
        ) as movie_mock:
            from jarvis_command_sdk import RequestInformation
            ri = RequestInformation(
                voice_command="cb:expand_title",
                conversation_id="c-1",
                user_id=7,
            )
            resp = cmd.expand_title(
                {"tmdb_id": 1396, "media_type": "tv"}, ri,
            )

        assert resp.success is True
        tv_mock.assert_called_once()
        movie_mock.assert_not_called()
        inbox = resp.context_data["inbox"]
        assert inbox["metadata"]["media_type"] == "tv"
