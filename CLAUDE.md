# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Japanese-language tool ("議事録作成ツール" / meeting-minutes tool) that records a Discord voice
call, transcribes it per-speaker with Whisper, and summarizes the transcript into structured
meeting minutes with Gemini. It targets Windows (`.bat` launchers, `winget`, `py -3.13`/`py -3.14`
launcher syntax) but the Python code itself is cross-platform.

Comments, docstrings, prompts, and most UI/CLI text throughout the codebase are in Japanese —
match that convention when editing existing files.

## Two-Python-version architecture (important)

This is the central quirk of the codebase and explains several otherwise-odd design choices:

- **`discord_bot.py`** runs under **Python 3.13** in a `.venv` (`requirements-bot.txt`), because
  the voice-recording library (Pycord/`py-cord[voice]`) is only verified there.
- **`pipeline.py`** (transcription + summarization) runs under a separate **Python 3.14**
  environment with `torch`, `transformers`, and `google-generativeai` installed directly (no
  requirements file for this environment — see README setup section).
- The bot never imports the pipeline in-process. It shells out to it: `discord_bot.py` calls
  `_run_pipeline()`, which runs `pipeline.py` as a **subprocess** (`py -3.14 pipeline.py <meeting_dir>`,
  overridable via the `PIPELINE_PYTHON` env var) so the heavy `torch` dependency never needs to be
  importable from the bot's own venv, and so the blocking transcription work doesn't block the
  bot's event loop (`loop.run_in_executor`).
- The pipeline subprocess reports its output file back to the bot via a stdout convention: its
  final line is `RESULT_PATH::<path>`, which `discord_bot.py._run_pipeline()` parses. Preserve this
  contract if you touch either side.

When changing dependencies or imports, check which side of this boundary a module lives on.
`whisper_utils.py` and `gemini_utils.py` intentionally do heavy imports (`torch`, `transformers`,
`google.generativeai`) **inside function bodies**, not at module scope, specifically so the modules
stay importable (e.g. for tests) without those packages installed.

## Two entry-point flows

1. **Discord automatic mode**: `discord_bot.py` → `/record` starts per-speaker WAV recording via
   `discord.sinks.WaveSink` → `/stop` triggers `on_recording_finished`, which saves audio to
   `recordings/<YYYY-MM-DD_HHMMSS>/<speaker>.wav`, invokes `pipeline.py` as a subprocess, then
   posts the resulting minutes file back to the Discord text channel. `watch_discord.py` is an
   optional Windows-only background watcher (`pythonw.exe`) that auto-starts/stops the bot based
   on whether `Discord.exe` is running.
2. **Manual mode** (no Discord/bot involved): drop audio files in `input/`, then run
   `run_whisper.py` (transcribes each file in `input/` to `output/*.txt`) followed by
   `run_gemini.py` (summarizes each `output/*.txt` into `output/*_summary.txt`, skipping files
   already ending in `_summary.txt`). `run_pipeline.bat` chains both steps and pre-checks that
   `.env` and the two scripts exist. `文字起こし.bat` runs transcription alone and also accepts
   drag-and-dropped audio files, copying them into `input/` first.

Both flows converge on the same shared helpers: `whisper_utils.py` (model loading + transcription)
and `gemini_utils.py` (`generate_with_retry`, shared exponential-backoff wrapper around
`model.generate_content`). Don't duplicate transcription/summarization logic in the entry-point
scripts — extend the shared helpers instead.

### Transcript merge logic (`pipeline.py.build_transcript`)

The Discord-mode pipeline transcribes each speaker's audio file independently (filename minus
extension = speaker name, from `sanitize_filename(display_name)` in `discord_bot.py`), then merges
all speakers' timestamped segments into one chronological transcript by sorting on segment start
time across all tracks. This is what produces the "誰がいつ何を言ったか" (who said what, when)
format. `run_whisper.py`'s manual mode does not do this merge — it transcribes each file to its own
independent `output/*.txt`.

### Gemini summarization prompt

`prompt.txt` is the system instruction given to the Gemini model (currently `gemini-2.5-pro` in
both `pipeline.py` and `run_gemini.py` — keep these in sync if you change the model) and defines
the required output template for meeting minutes (会議名/開催日時/参加者/内容/宿題). If you change
the desired output format, edit `prompt.txt` rather than post-processing the model's response in
code.

## Running tests

```
python -m unittest discover -s tests
```

Tests (`tests/test_helpers.py`) only cover pure functions in `text_utils.py`
(`sanitize_filename`) and `whisper_utils.py` (`format_timestamp`) — deliberately chosen because
they don't require `torch`/`transformers`/Discord to be installed. If you add pure-logic helpers,
prefer testing them the same way; anything that needs a real Whisper model, Gemini API key, or a
live Discord connection is not covered by automated tests and needs to be reasoned about or
manually verified instead.

## Configuration

All runtime configuration is via `.env` (see `.env.example`), loaded with `python-dotenv`:

- `DISCORD_TOKEN`, `GEMINI_API_KEY` — required for bot mode / summarization respectively. Code
  checks for both "unset" and the placeholder value (a string starting with `ここに`, i.e. the
  literal Japanese placeholder from `.env.example`) before treating a key as configured.
- `DISCORD_GUILD_ID` — optional; scopes slash commands to one guild for instant registration
  during development (global registration can take ~1 hour to propagate).
- `PIPELINE_PYTHON` — optional full path to override the `py -3.14` interpreter used to launch
  `pipeline.py` as a subprocess.
- `DELETE_AUDIO_AFTER_PROCESSING` (default `true`) — deletes raw audio in a meeting folder after
  minutes are successfully generated (transcript/minutes text files are kept).
- `RECORDINGS_RETENTION_DAYS` (default `30`) — on bot startup, deletes `recordings/` subfolders
  older than this many days; `<= 0` disables cleanup.

`recordings/`, `input/*`, `output/*`, and `*.log` are all gitignored because they can contain real
meeting audio/content — never commit sample data into these paths beyond the existing
`.gitkeep` files.

## Working conventions in this codebase

- Heavy ML/API imports (`torch`, `transformers`, `google.generativeai`, `discord`) are done lazily
  inside functions in the shared helper modules (`whisper_utils.py`, `gemini_utils.py`), not at
  module top-level — this keeps those modules cheap to import for testing/tooling. Follow the same
  pattern if you add new shared helpers with heavy dependencies.
- `pipeline.py` and `run_whisper.py`/`run_gemini.py` deliberately duplicate some driver logic
  (directory scanning, supported-extension filtering) rather than sharing a single "run everything"
  entry point, because they serve different flows (bot subprocess vs. manual CLI) with different
  I/O shapes (per-meeting-folder vs. flat `input`/`output`). Keep genuinely shared logic (model
  loading, retry, timestamp formatting, filename sanitization) in the `*_utils.py` modules; leave
  the flow-specific driving code where it is.
- Per-file errors during batch processing (a single speaker's transcription, a single file's
  summarization) are caught and logged, not raised — one bad track/file should not abort the whole
  meeting or batch. Preserve this when touching the loops in `pipeline.py.build_transcript`,
  `run_whisper.py`, and `run_gemini.py`.
