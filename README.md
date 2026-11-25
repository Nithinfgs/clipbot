# ClipBot

ClipBot watches live Twitch streams, detects hype moments from chat activity, cuts them into vertical
short-form videos with captions, and publishes them to Instagram Reels across several accounts on a
schedule. It runs locally on a Mac (built and tested on Apple Silicon).

It was built for a single launch-night event (dozens of streamers playing the same game at once), but
nothing in it is tied to one game: the game filter, streamer list and posting plan all live in
`config.yaml`.

## What it does

1. **Chat monitoring.** Joins Twitch chat anonymously for up to ~120 channels, split across several
   websocket connections.
2. **Hype detection.** For each channel it keeps rolling baselines (EWMA) of message rate, unique
   chatters and reaction-word share, and fires a trigger when they spike together for a few seconds.
   It filters raids, gift bombs, bot spam, copypasta, chat votes ("type 1 or 2") and reconnect bursts.
3. **Clip capture.** Cuts the moment out of the streamer's in-progress Twitch VOD at 720p60, using the
   playlist's wall-clock timestamps (both MPEG-TS and fragmented-MP4 VODs are supported). The biggest
   channels also keep a 480p rolling live buffer as a fallback when a VOD is unavailable.
4. **Editing.** Produces a 1080x1920 30 fps H.264 Reel: blurred background, full-width gameplay, a
   short zoom on the peak, a hook title, word-by-word burned-in captions (Whisper on Apple Silicon via
   mlx-whisper), the posting account's watermark, and -14 LUFS loudness normalisation. Swear words in
   burned captions are masked.
5. **Quality gates.** Rejects black screens, static loading screens, dead air, wrong duration or size,
   and duplicate moments.
6. **Captions.** Each post description credits the streamer (tagging their Instagram handle only when
   it has been verified), names the stream and the time of the moment, and adds rotating hashtags.
   A word filter keeps descriptions free of vulgar or explicit language.
7. **Assignment and scheduling.** Each clip goes to exactly one account, chosen by the clip's "vibe"
   (funny, fails, reactions, chaos, best-of) and by variety rules: no streamer twice in a row on an
   account, and no streamer on any account more than once in 20 minutes while alternatives exist.
   Accounts post on staggered slots with a daily cap.
8. **Publishing.** Uploads directly to Instagram through the official Graph API (resumable upload, no
   external hosting needed), with retries, crash-safe duplicate protection and per-account pausing on
   auth, rate-limit or spam errors.
9. **Logging and alerts.** Every trigger, edit, rejection and post (with permalink) goes to
   `logs/activity.csv`. Problems and an hourly summary can be pushed to a phone via ntfy.sh.

## Requirements

- macOS on Apple Silicon (VideoToolbox encoding and mlx-whisper)
- Homebrew packages: `ffmpeg` built with libass (`brew tap homebrew-ffmpeg/ffmpeg` then
  `brew install homebrew-ffmpeg/ffmpeg/ffmpeg`), `streamlink`, `uv`
- Python 3.13 (installed automatically by uv)
- A Meta developer app with the Instagram API use case, and Instagram professional (creator or
  business) accounts
- Roughly 30-40 Mbps of download bandwidth for ~50 channels, and at least 15 GB of free disk

## Setup

```bash
git clone https://github.com/Nithinfgs/clipbot.git
cd clipbot
uv sync
cp .env.example .env
```

### Instagram access

1. Switch each Instagram account to a professional account and make it public.
2. At developers.facebook.com, create an app and add the "Manage messaging and content on Instagram"
   use case. Connect a business portfolio when asked.
3. Under the use case's permissions, add `instagram_business_basic`,
   `instagram_business_content_publish` and `instagram_business_manage_insights`.
4. In App roles, add each Instagram account as an Instagram Tester and accept the invite from that
   account (Instagram settings, Apps and websites, Tester invites).
5. Under "API setup with Unified login", create a configuration, then use "Generate token" for each
   account. Put the tokens and the Instagram user IDs shown on that page into `.env` as
   `IG_TOKEN_n` and `IG_USER_ID_n`. Make sure each token is generated for the matching account.
6. Run `./ctl check`. It verifies every token and shows each account's posting quota without posting.

### Optional

- `NTFY_TOPIC` in `.env` for phone alerts (install the ntfy app and subscribe to the same topic).
- `TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET` (from dev.twitch.tv) to detect the game exactly and to
  discover new live streamers automatically. Without them, the game is inferred from stream titles and
  only the channels listed in `config.yaml` are used.

### Configuration

Edit `config.yaml`:

- `streamers`: Twitch channel names with a tier (A, B or C) and, optionally, a verified Instagram
  handle (`ig`) used for tagging.
- `accounts`: one entry per Instagram account, with its handle, vibe, hashtag pool, caption colour
  and slot offset. `env_index` maps it to `IG_TOKEN_n` / `IG_USER_ID_n`.
- `roster`: game keywords, how many live channels to keep active, and blocklists.
- `detector`: trigger thresholds per tier and the reaction-word lexicon.
- `posting`: interval between posts, daily cap per account, how long a finished clip stays fresh.
- `run.hours`: how long a run posts before stopping by itself.
- `mode: post_only`: skip streams entirely and only post clips that are already edited.

## Running

```bash
nohup ./run.sh > logs/run.out 2>&1 &
```

`run.sh` runs preflight checks, starts the bot under `caffeinate`, and restarts it automatically if it
exits. Keep the Mac plugged in with the lid open: on battery with the lid closed, macOS sleeps and the
bot cannot post.

### Controls

| Command | Effect |
|---|---|
| `./ctl check` | Verify tokens and show posting quota per account |
| `./ctl status` | Posted and queued clips per account, latest permalinks |
| `./ctl pause` / `./ctl resume` | Stop or restart posting on all accounts |
| `./ctl pause acc3` / `./ctl resume acc3` | Stop or restart one account |
| `touch data/STOP && pkill -INT -f "python -m clipbot.main"` | Stop the bot completely |

### Output

| Path | Contents |
|---|---|
| `logs/activity.csv` | One row per trigger, edit, rejection, post and roster change |
| `logs/clipbot.jsonl` | Full structured log |
| `clips/final/` | Finished Reels |
| `clips/raw/` | Raw cuts from streams |
| `data/clipbot.db` | SQLite state |

Storage is capped (`storage.clips_max_gb`); the oldest clip files are deleted first when it fills up.

## Project layout

```
clipbot/
  main.py        supervisor: chat, detection, capture, roster, storage, status
  chat/twitch.py anonymous Twitch IRC over websocket
  detector.py    per-channel hype detection
  recorder.py    streamlink + ffmpeg rolling buffer
  vodfetch.py    clip windows from in-progress Twitch VODs
  clipper.py     trigger -> raw clip
  editor.py      raw clip -> finished vertical Reel
  gates.py       quality checks and account assignment
  captions.py    post descriptions, hashtags, clean-language filter
  pipeline.py    edit queue, scheduler, publisher loop, reporting
  publisher.py   Instagram Graph API client
  roster.py      live/game filtering and streamer discovery
  storage.py     oldest-first clip cleanup
  activity.py    CSV activity log
  alerts.py      ntfy.sh notifications
  ctl.py         command-line controls
tests/           unit tests (uv run pytest)
tools/           manual helpers: render one clip, post one clip, make profile pictures
```

## Tests

```bash
uv run pytest
```

## Notes and limits

- Posting volume matters. Brand-new accounts that post dozens of Reels a day get little reach, and
  reposted stream footage is treated by Instagram as unoriginal content. Fewer, stronger posts work
  better than maximum volume.
- View counts need the `instagram_business_manage_insights` permission on the tokens.
- Only clip and repost streams you have permission to use, and follow each streamer's and community's
  rules for credit.
