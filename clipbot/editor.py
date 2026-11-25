"""Raw clip -> finished Instagram Reel (1080x1920, 30fps, H.264 VideoToolbox, AAC 48k, -14 LUFS).

Pipeline
  1. audio -> 16 kHz wav; RMS envelope (10 Hz)                     (numpy)
  2. word timestamps                                                (mlx-whisper small.en, ~2.6s / 40s clip)
  3. smart trim: start just before the first speech/loud moment, end ~0.8s after the reaction dies
  4. layout: blurred+darkened full-frame background, gameplay full-width centred, punch-in zoom at the peak
  5. text (libass): hook title (first 3s, top), word-by-word captions (2-4 words, current word highlighted),
     account watermark. Everything inside IG safe zones (top 8%, bottom 20%, right 12% kept clear).
     Burned captions mask swear words (s**t) for reach; the video content itself is untouched.
  6. loudnorm I=-14 TP=-1; cover = loudest moment near the peak (thumb_offset ms)
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import log
from .captions import _BAD_RE, _EMOJI

L = log.get("editor")
W, H = 1080, 1920
FPS = 30
WHISPER_MODEL = "mlx-community/whisper-small.en-mlx"
FONT_HOOK = "Impact"
FONT_CAP = "Arial Black"
FONTS_DIR = "/System/Library/Fonts/Supplemental"


@dataclass
class EditResult:
    path: Path
    duration: float
    start_in_raw: float
    thumb_offset_ms: int
    words: int
    transcript: str
    hook: str
    speech_ratio: float
    loud_ratio: float
    took_s: float


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {r.stderr[-600:]}")
    return r


def probe(path: Path) -> dict:
    j = json.loads(_run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                         str(path)]).stdout)
    v = next((s for s in j["streams"] if s["codec_type"] == "video"), {})
    a = next((s for s in j["streams"] if s["codec_type"] == "audio"), None)
    return {"duration": float(j["format"].get("duration", 0)), "w": v.get("width"), "h": v.get("height"),
            "audio": a is not None, "size": int(j["format"].get("size", 0))}


# ---------------------------------------------------------------- analysis
def audio_env(raw: Path, start: float, dur: float, wav: Path) -> np.ndarray:
    _run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(raw),
          "-vn", "-ac", "1", "-ar", "16000", str(wav)])
    with wave.open(str(wav)) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    hop = 1600                                           # 0.1 s
    n = len(x) // hop
    if n == 0:
        return np.zeros(1)
    return np.sqrt((x[: n * hop].reshape(n, hop) ** 2).mean(axis=1) + 1e-9)


_model_ready = False


def transcribe(wav: Path) -> list[dict]:
    global _model_ready
    import mlx_whisper
    r = mlx_whisper.transcribe(str(wav), path_or_hf_repo=WHISPER_MODEL, word_timestamps=True, language="en",
                               condition_on_previous_text=False, no_speech_threshold=None)
    # game audio makes Whisper rate real speech ~0.7 "no speech"; rely on per-word confidence instead
    _model_ready = True
    words = []
    for s in r.get("segments", []):
        if s.get("no_speech_prob", 0) > 0.95 or s.get("avg_logprob", 0) < -1.6:
            continue
        for w in s.get("words", []):
            t = w["word"].strip()
            if t and w.get("probability", 1) > 0.15:
                words.append({"w": t, "s": float(w["start"]), "e": float(w["end"])})
    return words


def smart_trim(env: np.ndarray, words: list[dict], dur: float, peak: float,
               min_len: float = 12.0, max_len: float = 40.0) -> tuple[float, float]:
    """Return (a, b) seconds inside the window. Start just before action, end shortly after the reaction."""
    base = float(np.percentile(env, 30)) if len(env) else 0.0
    loud = env > max(base * 2.2, 0.02)
    t = np.arange(len(env)) / 10.0
    events = sorted([w["s"] for w in words] + list(t[loud]))
    a = max(0.0, (events[0] if events else 0.0) - 0.4)
    a = min(a, max(0.0, peak - 10.0), 8.0)              # never cut into the 10s before the peak; trim <=8s
    ends = [w["e"] for w in words] + list(t[loud] + 0.1)
    b = min(dur, (max(ends) if ends else dur) + 0.8)
    b = max(b, min(dur, peak + 3.0))                    # always show the reaction after the peak
    if b - a < min_len:
        b = min(dur, a + min_len)
        a = max(0.0, b - min_len)
    if b - a > max_len:
        a = max(a, b - max_len)
    return round(a, 2), round(b, 2)


# ---------------------------------------------------------------- hook titles (no AI: from chat reaction)
_HOOKS = [
    (r"\b(kekw|lul|lol+|lmao+|omegalul|icant|😂|🤣|haha)\b", ["CHAT COULDN'T BREATHE 😂", "{n} HAD CHAT IN TEARS 😂",
                                                              "THIS IS TOO FUNNY 😂"]),
    (r"(\?\?|huh|what|wtf|😳|confused)", ["WAIT… WHAT JUST HAPPENED?! 😳", "NOBODY EXPECTED THIS 😳",
                                          "{n} WAS NOT READY 😳"]),
    (r"\b(no ?way|omg|holy|yo+|bro+|😱)\b", ["NO WAY THIS HAPPENED 😱", "{n} CAN'T BELIEVE IT 😱",
                                            "GTA 6 IS INSANE 😱"]),
    (r"\b(rip|l|f|💀|dead|wasted|unlucky|sadge)\b", ["THIS DID NOT GO TO PLAN 💀", "{n} GOT WASTED 💀",
                                                    "WORST LUCK IN VICE CITY 💀"]),
    (r"\b(w|pog|pogchamp|ez|clean|goated|insane|🔥)\b", ["ABSOLUTE MOVIE 🔥", "{n} IS DIFFERENT 🔥",
                                                        "CLEANEST PLAY IN GTA 6 🔥"]),
    (r"\b(cops?|police|stars?|chase|wanted)\b", ["5 STARS IN 5 SECONDS 🚔", "{n} VS THE ENTIRE POLICE 🚔"]),
]


def hook_from_chat(top_messages: list[str], display: str, seed: int = 0) -> str:
    text = " ".join(m.rsplit(" x", 1)[0] for m in top_messages).lower()
    weights = []
    for pat, options in _HOOKS:
        hits = len(re.findall(pat, text))
        weights.append((hits, options))
    hits, options = max(weights, key=lambda x: x[0])
    if hits == 0:
        options = ["{n} CAN'T BELIEVE THIS 💀", "GTA 6 MOMENTS LIKE THIS 😭", "CHAT WENT CRAZY FOR THIS 🤯"]
    h = options[seed % len(options)].replace("{n}", display.upper())
    return h if not _BAD_RE.search(h.lower()) else "GTA 6 MOMENTS LIKE THIS 😭"


# ---------------------------------------------------------------- subtitles (ASS)
def _mask(word: str) -> str:
    def rep(m):
        s = m.group(0)
        return s[0] + "*" * (len(s) - 1)
    return _BAD_RE.sub(rep, word)


def _ass_time(t: float) -> str:
    t = max(t, 0)
    h, r = divmod(t, 3600)
    m, s = divmod(r, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


def group_words(words: list[dict], max_words: int = 4, max_chars: int = 18) -> list[list[dict]]:
    groups, cur = [], []
    for w in words:
        if cur and (len(cur) >= max_words or w["s"] - cur[-1]["e"] > 0.45
                    or len(" ".join(x["w"] for x in cur + [w])) > max_chars
                    or re.search(r"[.!?,]$", cur[-1]["w"])):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def build_ass(words: list[dict], hook: str, handle: str, accent_bgr: str, dur: float, cap_y: int,
              hook_y: int, wm_y: int) -> str:
    accent = accent_bgr if accent_bgr.startswith("&H") else "&H0000D7FF"
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,{FONT_HOOK},118,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,1,0,1,7,3,8,90,150,0,1
Style: Cap,{FONT_CAP},80,&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,0,0,0,0,100,100,0,0,1,7,2,8,90,150,0,1
Style: WM,{FONT_CAP},38,&H40FFFFFF,&H40FFFFFF,&H60000000,&H00000000,0,0,0,0,100,100,1,0,1,2,0,8,90,150,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    hook = re.sub(r"\s+", " ", _EMOJI.sub("", hook or "")).strip()   # fonts have no emoji glyphs
    if hook:
        ev.append(f"Dialogue: 2,{_ass_time(0)},{_ass_time(min(3.2, dur))},Hook,,0,0,0,,"
                  f"{{\\an8\\pos({W // 2 - 30},{hook_y})\\fad(80,250)\\fscx80\\fscy80\\t(0,160,\\fscx104\\fscy104)"
                  f"\\t(160,260,\\fscx100\\fscy100)\\q0}}{_esc(hook)}")
    groups = group_words(words)
    for gi, g in enumerate(groups):
        toks = [_esc(_mask(w["w"])).upper() for w in g]
        nxt = groups[gi + 1][0]["s"] if gi + 1 < len(groups) else dur
        for i, w in enumerate(g):
            st = w["s"]
            en = g[i + 1]["s"] if i + 1 < len(g) else min(max(w["e"], st + 0.25) + 0.12, nxt - 0.02)
            if en <= st:
                continue
            line = " ".join((f"{{\\c{accent}&\\fscx112\\fscy112}}{t}{{\\c&H00FFFFFF&\\fscx100\\fscy100}}" if j == i else t)
                            for j, t in enumerate(toks))
            pop = "\\fscx90\\fscy90\\t(0,90,\\fscx100\\fscy100)" if i == 0 else ""
            ev.append(f"Dialogue: 1,{_ass_time(st)},{_ass_time(en)},Cap,,0,0,0,,{{\\an8\\pos({W // 2 - 30},{cap_y}){pop}}}{line}")
    ev.append(f"Dialogue: 0,{_ass_time(0)},{_ass_time(dur)},WM,,0,0,0,,{{\\an8\\pos({W // 2 - 30},{wm_y})}}@{_esc(handle)}")
    return head + "\n".join(ev) + "\n"


# ---------------------------------------------------------------- render
def edit(raw: Path, offset: float, window: float, peak_rel: float, handle: str, accent: str, hook: str,
         out: Path, tmp: Path, zoom: bool = True) -> EditResult:
    t0 = time.time()
    tmp.mkdir(parents=True, exist_ok=True)
    info = probe(raw)
    window = min(window, max(info["duration"] - offset, 1))
    wav = tmp / f"{out.stem}.wav"
    env = audio_env(raw, offset, window, wav)
    words = transcribe(wav)
    a, b = smart_trim(env, words, window, peak_rel)
    dur = b - a
    words = [{"w": w["w"], "s": round(w["s"] - a, 2), "e": round(w["e"] - a, 2)} for w in words
             if w["s"] >= a - 0.05 and w["e"] <= b + 0.05]
    speech_ratio = sum(w["e"] - w["s"] for w in words) / max(dur, 1)
    seg_env = env[int(a * 10): int(b * 10)]
    base = float(np.percentile(env, 30)) if len(env) else 0
    loud_ratio = float((seg_env > max(base * 1.6, 0.015)).mean()) if len(seg_env) else 0.0

    # layout: gameplay scaled to full width, centred; text above/below it
    src_w, src_h = info["w"] or 1280, info["h"] or 720
    fg_h = int(round(W * src_h / src_w / 2) * 2)
    top = (H - fg_h) // 2
    hook_y = max(int(H * 0.09), top - 300)
    cap_y = top + fg_h + 50
    wm_y = min(int(H * 0.79) - 50, cap_y + 230)
    ass = tmp / f"{out.stem}.ass"
    ass.write_text(build_ass(words, hook, handle, accent, dur, cap_y, hook_y, wm_y), encoding="utf-8")

    p = max(0.0, peak_rel - a)
    zexpr = f"1+0.10*exp(-pow((t-{p:.2f})/0.8\\,2))" if zoom else "1"
    vf = (f"[0:v]split=2[b][f];"
          f"[b]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},gblur=sigma=28,eq=brightness=-0.13:saturation=1.15[bg];"
          f"[f]scale=w='trunc({W}*({zexpr})/2)*2':h=-2:eval=frame,crop={W}:ih[fg];"
          f"[bg][fg]overlay=(W-w)/2:{top}-(h-{fg_h})/2:eval=frame,fps={FPS},format=yuv420p,"
          f"subtitles={ass}:fontsdir={FONTS_DIR}[v]")
    out.parent.mkdir(parents=True, exist_ok=True)
    common_in = ["ffmpeg", "-v", "error", "-y", "-ss", f"{offset + a:.3f}", "-t", f"{dur:.3f}", "-i", str(raw),
                 "-filter_complex", vf, "-map", "[v]", "-map", "0:a:0?",
                 "-af", "loudnorm=I=-14:TP=-1:LRA=11,aresample=48000"]
    common_out = ["-g", "60", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart",
                  str(out)]
    try:
        _run(common_in + ["-c:v", "h264_videotoolbox", "-b:v", "9M", "-maxrate", "12M", "-bufsize", "18M",
                          "-profile:v", "high"] + common_out)
    except RuntimeError as e:          # VideoToolbox occasionally rejects a frame -> software encode
        L.warning("videotoolbox failed (%s) -> libx264 fallback", str(e)[-120:])
        # Instagram rejects B-frame/open-GOP uploads ("ProcessingFailedError"): match the VideoToolbox output
        _run(common_in + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-maxrate", "12M",
                          "-bufsize", "18M", "-profile:v", "high", "-pix_fmt", "yuv420p", "-bf", "0",
                          "-x264-params", "keyint=60:min-keyint=60:scenecut=0:open-gop=0"] + common_out)
    # cover: loudest 0.1s within peak-1s..peak+3s
    lo, hi = int(max(0, (a + p - 1) * 10)), int(min(len(env), (a + p + 3) * 10))
    k = int(np.argmax(env[lo:hi])) + lo if hi > lo else int((a + p) * 10)
    thumb = int(max(0, min(dur - 0.1, k / 10 - a)) * 1000)
    wav.unlink(missing_ok=True)
    return EditResult(out, round(dur, 2), round(offset + a, 2), thumb, len(words), " ".join(w["w"] for w in words),
                      hook, round(speech_ratio, 2), round(loud_ratio, 2), round(time.time() - t0, 1))


def frame_stats(path: Path, every_s: float = 1.0) -> dict:
    """Junk-screen heuristics: fraction of near-black frames and of near-static frames (loading screens)."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf",
                        f"fps=1/{every_s},scale=64:114,format=gray", "-f", "rawvideo", "-"], capture_output=True)
    fr = np.frombuffer(r.stdout, np.uint8)
    n = len(fr) // (64 * 114)
    if n == 0:
        return {"frames": 0, "black": 1.0, "static": 1.0}
    f = fr[: n * 64 * 114].reshape(n, 114, 64).astype(np.float32)
    centre = f[:, 35:80, :]                               # gameplay band only (not the blurred bg)
    black = float((centre.mean(axis=(1, 2)) < 18).mean())
    diffs = np.abs(np.diff(centre, axis=0)).mean(axis=(1, 2)) if n > 1 else np.array([0.0])
    static = float((diffs < 1.2).mean())
    return {"frames": n, "black": round(black, 2), "static": round(static, 2)}
