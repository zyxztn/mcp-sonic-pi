# mcp-sonic-pi (Sonic Pi 5 fork)

Fork of [vinayak-mehta/mcp-sonic-pi](https://github.com/vinayak-mehta/mcp-sonic-pi) (Apache-2.0).
Everything is free and local: OSC to 127.0.0.1, no API keys, no cloud.

## What changed
- **Works with Sonic Pi 5.x.** v5 ignores OSC without the daemon's auth token, so upstream's
  psonic-based `/run-code` was silently dropped. This fork reads the spider server's port and
  token from its process arguments and talks to it directly with python-osc. psonic is gone.
- **`record_to_mp3` tool.** Records Sonic Pi's main output and saves an **.mp3** (no .rb) into
  `~/AiMusic` (override with the `AIMUSIC_DIR` env var). The Sonic Pi source is embedded in the
  MP3's lyrics ID3 tag so it stays editable. With `loop_seconds` set it records two cycles and
  keeps the second, so the reverb tail wraps and the loop is seamless.
- `mcp` is pinned `<2` (mcp 2.x removed `mcp.server.fastmcp`).

Tools: `initialize_sonic_pi`, `play_music`, `stop_music`, `record_to_mp3`, `get_beat_pattern`.

## Install (Claude Code)
Start Sonic Pi first, then, from the folder containing this fork:

    brew install uv        # if you don't have uv (free)
    claude mcp add sonic-pi -- uvx --from "$PWD" mcp-sonic-pi
    # optional: custom output folder
    claude mcp add sonic-pi -e AIMUSIC_DIR="$HOME/AiMusic" -- uvx --from "$PWD" mcp-sonic-pi

## Notes
- Sonic Pi must be running (the app, not just the server) when a tool is called.
- MP3 loop points: lameenc doesn't write a gapless header, so some players add ~25 ms of
  padding at the loop point. For sample-exact looping use the WAV or a gapless-aware engine.
- Reading the token from the process list works because Sonic Pi 5 only prints it to its GUI
  and passes it on the command line; any process of the same user can see it.
