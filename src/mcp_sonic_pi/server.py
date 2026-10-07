#!/usr/bin/env python3
"""
MCP Server for controlling Sonic Pi 5.x (fork of vinayak-mehta/mcp-sonic-pi).

Changes from upstream (Apache-2.0):
  * Talks to Sonic Pi 5's token-authenticated OSC API directly with python-osc
    instead of psonic (psonic sends untokened /run-code to port 4560, which
    Sonic Pi 5 ignores).
  * Finds the spider server's port and token from its command line (the daemon
    only logs them on exit).
  * New record_to_mp3 tool: records Sonic Pi's main output, encodes MP3 with
    lameenc (free, local) and saves it into AIMUSIC_DIR (default ~/Documents/AiMusic).

Everything is local: OSC to 127.0.0.1 only, no API keys, no cloud services.
"""

import asyncio
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP
from pythonosc.udp_client import SimpleUDPClient

mcp = FastMCP("sonic-pi")

AIMUSIC_DIR = Path(os.environ.get("AIMUSIC_DIR", "~/Documents/AiMusic")).expanduser()

_SPIDER_RE = re.compile(
    r"spider-server\.rb\s+-u\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)"
)


@dataclass
class SonicPiConn:
    server_port: int  # where spider-server listens for GUI/API messages
    token: int  # daemon-issued auth token, first arg of every message

    def client(self) -> SimpleUDPClient:
        return SimpleUDPClient("127.0.0.1", self.server_port)

    def send(self, address: str, *args) -> None:
        self.client().send_message(address, [self.token, *args])


def find_sonic_pi() -> SonicPiConn | None:
    """Find a running Sonic Pi 5 spider server and read its port + token.

    Sonic Pi's daemon starts the spider server as:
      ruby ... spider-server.rb -u <server_port> <gui_port> <scsynth>
                                <scsynth_send> <osc_cues> <token>
    """
    try:
        out = subprocess.run(
            ["ps", "-axo", "args="], capture_output=True, text=True, timeout=5
        ).stdout
    except Exception:
        return None
    for line in out.splitlines():
        m = _SPIDER_RE.search(line)
        if m:
            return SonicPiConn(server_port=int(m.group(1)), token=int(m.group(6)))
    return None


NOT_RUNNING = (
    "Error: Sonic Pi 5 does not appear to be running (no spider-server process "
    "found). Start the Sonic Pi app first."
)


def _safe_name(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", name.strip()).strip(" ._")
    if stem.lower().endswith(".mp3"):
        stem = stem[:-4]
    return stem or time.strftime("sonic-pi-%Y%m%d-%H%M%S")


@mcp.tool()
async def initialize_sonic_pi() -> str:
    """Initialize the Sonic Pi connection

    Returns:
        The system prompt for the server
    """
    conn = find_sonic_pi()
    if not conn:
        return NOT_RUNNING
    return (
        f"Connected to Sonic Pi (server port {conn.server_port}).\n"
        + system_prompt()
    )


@mcp.tool()
async def play_music(code: str) -> str:
    """Play music using Sonic Pi code.

    Args:
        code: Sonic Pi Ruby code

    Returns:
        A confirmation message
    """
    conn = find_sonic_pi()
    if not conn:
        return NOT_RUNNING
    try:
        conn.send("/stop-all-jobs")
        await asyncio.sleep(0.2)
        conn.send("/run-code", code)
        return "Code is now running. If you don't hear anything, check Sonic Pi for errors."
    except Exception as e:
        return f"Error running code: {str(e)}"


@mcp.tool()
async def stop_music() -> str:
    """Stop all currently playing Sonic Pi music.

    Returns:
        A confirmation message
    """
    conn = find_sonic_pi()
    if not conn:
        return NOT_RUNNING
    try:
        conn.send("/stop-all-jobs")
        return "Music stopped"
    except Exception as e:
        return f"Error stopping music: {str(e)}"


async def _wait_for_file(path: Path, timeout: float = 15.0) -> bool:
    """Wait until path exists and its size stops changing."""
    deadline = time.monotonic() + timeout
    last = -1
    while time.monotonic() < deadline:
        if path.exists():
            size = path.stat().st_size
            if size > 44 and size == last:
                return True
            last = size
        await asyncio.sleep(0.4)
    return False


def _encode_mp3(
    wav_path: Path,
    mp3_path: Path,
    loop_seconds: float | None,
    title: str,
    code: str,
    embed_code: bool,
) -> dict:
    import lameenc
    import numpy as np
    import soundfile as sf
    from mutagen.id3 import ID3, TIT2, TXXX, USLT

    data, rate = sf.read(str(wav_path), dtype="float32", always_2d=True)
    if data.shape[1] == 1:
        data = np.repeat(data, 2, axis=1)
    data = data[:, :2]

    # Drop leading silence (recording starts before the code makes sound).
    audible = np.flatnonzero(np.abs(data).max(axis=1) > 0.003)
    onset = int(audible[0]) if audible.size else 0

    if loop_seconds:
        # Record >= 2 cycles; keep the 2nd, which already contains the reverb
        # tail of cycle 1, so the file loops without a gap or a click.
        n = int(round(loop_seconds * rate))
        start = onset + n
        if start + n > len(data):
            raise ValueError(
                "Recording is shorter than two loop cycles; this is a bug in "
                "record_to_mp3's duration handling."
            )
        data = data[start : start + n]
    else:
        data = data[max(onset - int(0.02 * rate), 0) :]
        fade = min(int(0.05 * rate), len(data))
        if fade:
            data[-fade:] *= np.linspace(1.0, 0.0, fade, dtype="float32")[:, None]

    peak = float(np.abs(data).max()) or 1.0
    if peak > 0.98:  # avoid clipping on encode, otherwise leave levels alone
        data = data * (0.98 / peak)
    pcm = (np.clip(data, -1.0, 1.0) * 32767.0).astype("<i2")

    enc = lameenc.Encoder()
    enc.set_bit_rate(192)
    enc.set_in_sample_rate(int(rate))
    enc.set_channels(2)
    enc.set_quality(2)
    mp3_bytes = enc.encode(pcm.tobytes()) + enc.flush()
    mp3_path.write_bytes(mp3_bytes)

    tags = ID3()
    tags.add(TIT2(encoding=3, text=title))
    tags.add(TXXX(encoding=3, desc="Generator", text="Sonic Pi via mcp-sonic-pi (fork)"))
    if loop_seconds:
        tags.add(TXXX(encoding=3, desc="LoopSeconds", text=f"{loop_seconds:g}"))
    if embed_code:
        # Keeps the music editable even though only the .mp3 is saved.
        tags.add(USLT(encoding=3, lang="eng", desc="Sonic Pi code", text=code))
    tags.save(str(mp3_path))
    return {
        "seconds": round(len(data) / rate, 2),
        "sample_rate": int(rate),
        "peak": round(peak, 3),
    }


@mcp.tool()
async def record_to_mp3(
    code: str,
    name: str,
    seconds: float = 30.0,
    loop_seconds: float = 0.0,
    tail_seconds: float = 2.0,
    embed_code: bool = True,
    ctx: Context | None = None,
) -> str:
    """Run Sonic Pi code, record its output and save it as an MP3 in the AiMusic folder.

    Only an .mp3 is written (no .rb). By default the Sonic Pi source is embedded in
    the MP3's lyrics (USLT) ID3 tag so it can be recovered and edited later.

    Args:
        code: Sonic Pi Ruby code to run and record.
        name: File name without extension (saved as <name>.mp3).
        seconds: Length to record for a one-shot take (ignored if loop_seconds is set).
        loop_seconds: If > 0, the code is a looping piece of exactly this length.
            It records two cycles and keeps the second, so the MP3 loops seamlessly.
            Needs the piece to make a sound on its first downbeat.
        tail_seconds: One-shot takes only: extra seconds recorded after the code is
            stopped so reverb/release tails are not cut off.
        embed_code: Embed the source in the MP3 tags.

    Returns:
        The saved file path.
    """
    conn = find_sonic_pi()
    if not conn:
        return NOT_RUNNING
    if loop_seconds < 0 or seconds <= 0 or tail_seconds < 0:
        return "Error: seconds must be > 0 and loop_seconds/tail_seconds must be >= 0."

    loop = loop_seconds if loop_seconds > 0 else None
    record_for = (2 * loop + 2.0) if loop else seconds
    tail = 0.0 if loop else tail_seconds

    try:
        AIMUSIC_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return f"Error: cannot create {AIMUSIC_DIR}: {e}"

    stem = _safe_name(name)
    mp3_path = AIMUSIC_DIR / f"{stem}.mp3"
    if mp3_path.exists():
        mp3_path = AIMUSIC_DIR / f"{stem}-{time.strftime('%Y%m%d-%H%M%S')}.mp3"

    with tempfile.TemporaryDirectory(prefix="mcp-sonic-pi-") as tmp:
        wav_path = Path(tmp) / "take.wav"
        try:
            conn.send("/stop-all-jobs")
            conn.send("/delete-recording")
            await asyncio.sleep(0.4)
            conn.send("/start-recording")
            await asyncio.sleep(0.4)
            conn.send("/run-code", code)

            total = record_for + tail
            waited = 0.0
            while waited < record_for:
                step = min(1.0, record_for - waited)
                await asyncio.sleep(step)
                waited += step
                if ctx is not None:
                    try:
                        await ctx.report_progress(waited, total, "recording")
                    except Exception:
                        pass
            conn.send("/stop-all-jobs")
            if tail:
                await asyncio.sleep(tail)
            conn.send("/stop-recording")
            await asyncio.sleep(0.5)
            conn.send("/save-recording", str(wav_path))
            if not await _wait_for_file(wav_path):
                return (
                    "Error: Sonic Pi did not write the recording. Check the Sonic Pi "
                    "log for errors in the code."
                )
            info = await asyncio.to_thread(
                _encode_mp3, wav_path, mp3_path, loop, stem, code, embed_code
            )
        except ImportError as e:
            return f"Error: missing audio dependency ({e}). Reinstall the fork."
        except Exception as e:
            try:
                conn.send("/stop-all-jobs")
            except Exception:
                pass
            return f"Error recording: {e}"

    size_kb = mp3_path.stat().st_size // 1024
    loop_note = " (seamless loop)" if loop else ""
    return (
        f"Saved {mp3_path} - {info['seconds']}s, {size_kb} KB, "
        f"{info['sample_rate']} Hz stereo MP3{loop_note}."
    )


@mcp.tool()
def get_beat_pattern(style: str) -> str:
    """Get drum beat patterns for Sonic Pi.

    Args:
        style: Beat style (blues, rock, jazz, hiphop, etc.)

    Returns:
        Sonic Pi code for the requested beat pattern
    """
    beats = {
        "blues": """
# Blues Beat
use_bpm 100
swing = 0.15  # Shuffle feel (0 for straight timing)
live_loop :blues_drums do
  sample :hat_tap, amp: 0.9
  sample :drum_bass_hard, amp: 0.9
  sleep 0.5+swing
  sample :hat_tap, amp: 0.7
  sample :drum_bass_hard, amp: 0.8
  sleep 0.5-swing
  sample :drum_snare_hard, amp: 0.8
  sample :hat_tap, amp: 0.8
  sleep 0.5+swing
  sample :hat_tap, amp: 0.7
  sleep 0.5-swing
end
""",
        "rock": """
# Rock Beat
use_bpm 120
live_loop :rock_drums do
  sample :drum_bass_hard, amp: 1
  sample :drum_cymbal_closed, amp: 0.7
  sleep 0.5
  sample :drum_cymbal_closed, amp: 0.7
  sleep 0.5
  sample :drum_snare_hard, amp: 0.9
  sample :drum_cymbal_closed, amp: 0.7
  sleep 0.5
  sample :drum_cymbal_closed, amp: 0.7
  sleep 0.5
end
""",
        "hiphop": """
# Hip-Hop Beat
use_bpm 90
live_loop :hip_hop_drums do
  sample :drum_bass_hard, amp: 1.2
  sleep 1
  sample :drum_snare_hard, amp: 0.9
  sleep 1
  sample :drum_bass_hard, amp: 1.2
  sleep 0.5
  sample :drum_bass_hard, amp: 0.8
  sleep 0.5
  sample :drum_snare_hard, amp: 0.9
  sleep 1
end
""",
        "electronic": """
# Electronic Beat
use_bpm 128
live_loop :electronic_beat do
  sample :bd_haus, amp: 1
  sample :drum_cymbal_closed, amp: 0.3
  sleep 0.5

  sample :drum_cymbal_closed, amp: 0.3
  sleep 0.5

  sample :bd_haus, amp: 0.9
  sample :drum_snare_hard, amp: 0.8
  sample :drum_cymbal_closed, amp: 0.3
  sleep 0.5

  sample :drum_cymbal_closed, amp: 0.3
  sleep 0.5
end
""",
    }

    if style.lower() in beats:
        return beats[style.lower()]
    else:
        return f"Beat style '{style}' not found. Available styles: {', '.join(beats.keys())}"


@mcp.prompt()
def system_prompt():
    return """
    You are a Sonic Pi assistant that helps users create musical compositions using code. Your knowledge includes various rhythm patterns, chord progressions, scales, and proper Sonic Pi syntax. Respond with accurate, executable Sonic Pi code based on user requests. Remember to call initialize_sonic_pi first before playing any music with Sonic Pi.

    When the user asks you to play a beat, you should use the get_beat_pattern tool to get the beat pattern, play the beat and add nothing else on top of it.

    When the user asks you to play a chord progression, construct one using the following chord format, and add it to the existing beat.

    Chords have the following format: chord  tonic (symbol), name (symbol)

    Here's an example chord with C tonic and various names:
    (chord :C, '1')
    (chord :C, '5')
    (chord :C, '+5')
    (chord :C, 'm+5')
    (chord :C, :sus2)
    (chord :C, :sus4)
    (chord :C, '6')
    (chord :C, :m6)
    (chord :C, '7sus2')
    (chord :C, '7sus4')
    (chord :C, '7-5')
    (chord :C, 'm7-5')
    (chord :C, '7+5')
    (chord :C, 'm7+5')
    (chord :C, '9')
    (chord :C, :m9)
    (chord :C, 'm7+9')
    (chord :C, :maj9)
    (chord :C, '9sus4')
    (chord :C, '6*9')
    (chord :C, 'm6*9')
    (chord :C, '7-9')
    (chord :C, 'm7-9')
    (chord :C, '7-10')
    (chord :C, '9+5')
    (chord :C, 'm9+5')
    (chord :C, '7+5-9')
    (chord :C, 'm7+5-9')
    (chord :C, '11')
    (chord :C, :m11)
    (chord :C, :maj11)
    (chord :C, '11+')
    (chord :C, 'm11+')
    (chord :C, '13')
    (chord :C, :m13)
    (chord :C, :add2)
    (chord :C, :add4)
    (chord :C, :add9)
    (chord :C, :add11)
    (chord :C, :add13)
    (chord :C, :madd2)
    (chord :C, :madd4)
    (chord :C, :madd9)
    (chord :C, :madd11)
    (chord :C, :madd13)
    (chord :C, :major)
    (chord :C, :M)
    (chord :C, :minor)
    (chord :C, :m)
    (chord :C, :major7)
    (chord :C, :dom7)
    (chord :C, '7')
    (chord :C, :M7)
    (chord :C, :minor7)
    (chord :C, :m7)
    (chord :C, :augmented)
    (chord :C, :a)
    (chord :C, :diminished)
    (chord :C, :dim)
    (chord :C, :i)
    (chord :C, :diminished7)
    (chord :C, :dim7)
    (chord :C, :i7)

    Remember that all Sonic Pi code must be valid Ruby code, with proper indentation, parameter passing, and loop definitions. When composing patterns, always ensure the timing adds up correctly within each loop.
"""


def main():
    conn = find_sonic_pi()
    if conn:
        print(f"Sonic Pi is running (server port {conn.server_port})")
    else:
        print("Warning: Sonic Pi 5 doesn't appear to be running")
        print("Start Sonic Pi before using this MCP server")
    print(f"MP3s are saved to {AIMUSIC_DIR}")
    print("Sonic Pi MCP Server initialized")

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
