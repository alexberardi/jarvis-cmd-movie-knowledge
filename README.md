# jarvis-cmd-movie-knowledge

TMDB-powered movie lookup for [Jarvis](https://github.com/alexberardi). Produces a
rich inbox card with synopsis, cast, director, and similar films. Each cast
member, director, and similar movie is **tappable** in the mobile app —
taps drill one level deeper (the actor's filmography, the director's other
films, or a fresh crawl of the related movie), rendered as a stacked screen
rather than spamming the inbox.

## Voice phrasings

- "Look up the movie *The Matrix*"
- "Tell me about the movie *Inception*"
- "Info on the movie *Dune*"
- "Who's in *Forrest Gump*"
- (And the obvious "IMDB crawl X" — accepted even when Whisper hears it as "I am DB crawl")

The first four are pre-routed regexes (LLM-bypass), so they fire deterministically
without round-tripping to command-center for parsing.

## Configuration

| Secret | Required | Notes |
|---|---|---|
| `TMDB_API_KEY` | yes | Free v3 key from [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api) |
| `MOVIE_KNOWLEDGE_PUSH_NOTIFICATIONS` | no | Toggle in mobile settings. Off by default — the inbox card always lands; this just decides whether your phone buzzes for it. |

## Architecture

Each card embeds `interactive_elements` in its metadata with `navigation_type: "stack"`,
so taps on actor / director / similar-movie chips:

1. POST `/api/v0/callbacks` to command-center (mobile, user JWT)
2. CC publishes the opaque job id over MQTT to the originating node
3. Node fetches the full payload from CC over its authenticated HTTPS channel
4. Node dispatches to a `@callback`-decorated method on this command, which returns
   the rendered content (no separate inbox row is created — see below)
5. Mobile polls `GET /api/v0/callbacks/{id}/status` and renders the new content
   on a screen pushed onto the inbox navigation stack

`navigation_type` defaults to `"new_notification"` for back-compat with commands
that prefer the original async-inbox surface. Movie Knowledge picks `"stack"`
everywhere because every drill-down is a single cheap TMDB call.

## Requirements

- `jarvis-command-sdk` ≥ 0.2.0 (for `@callback` decorator + `get_callbacks()`)
- `jarvis-config-client` (for command-center URL discovery — already on every node)
- `httpx`

## Development

```bash
jdt test .        # Run tests
jdt validate .    # Quick manifest check
jdt manifest .    # Regenerate manifest from code
jdt deploy local .                    # local install
jdt deploy ssh pi@jarvis-dev.local .  # ship to a Pi node
```

## License

MIT
