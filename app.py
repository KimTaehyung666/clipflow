import os, json, re, shutil, sqlite3, subprocess, sys, threading, time, uuid
from datetime import datetime, timezone
from flask import Flask, render_template, request, jsonify, send_file, abort
from dotenv import load_dotenv
import requests
from urllib.parse import urlparse, parse_qs

load_dotenv()

app = Flask(__name__)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
# Model lama "llama-3.3-70b-versatile" sudah dimatikan Groq per 16 Agu 2026.
# Pengganti resmi dari Groq: openai/gpt-oss-120b. Bisa dioverride via env GROQ_MODEL.
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Fallback transcript pihak ketiga. YouTube memblokir transcript dari IP cloud
# (Vercel/Render/dll), jadi di deploy server, transcript diambil lewat API ini.
TRANSCRIPT_API_KEY = os.getenv("TRANSCRIPT_API_KEY")
SUPADATA_API_KEY = os.getenv("SUPADATA_API_KEY")

# Penyimpanan lokal (riwayat, cache video, hasil potongan).
# Di Vercel filesystem cuma /tmp yang bisa ditulis.
if os.getenv("CLIPRADAR_DATA"):
    DATA_DIR = os.environ["CLIPRADAR_DATA"]
elif os.getenv("VERCEL"):
    DATA_DIR = "/tmp/clipradar"
else:
    DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CLIPS_DIR = os.path.join(DATA_DIR, "clips")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
SRT_DIR = os.path.join(DATA_DIR, "srt")
DB_PATH = os.path.join(DATA_DIR, "clipradar.db")
for _d in (DATA_DIR, CLIPS_DIR, CACHE_DIR, SRT_DIR):
    try:
        os.makedirs(_d, exist_ok=True)
    except OSError:
        pass

def find_ffmpeg():
    env = os.getenv("FFMPEG_BIN")
    if env and os.path.exists(env):
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None

def find_ytdlp_cmd():
    found = shutil.which("yt-dlp")
    if found:
        return [found]
    try:
        import yt_dlp  # noqa: F401
        return [sys.executable, "-m", "yt_dlp"]
    except Exception:
        return None

def call_groq(prompt, max_tokens=2500):
    if not GROQ_API_KEY:
        raise Exception("GROQ_API_KEY belum diset. Isi file .env (lokal) atau Environment Variable (deploy) dulu ya.")
    try:
        response = requests.post(
            GROQ_URL,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {GROQ_API_KEY}"},
            json={
                "model": GROQ_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.7,
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
            },
            timeout=60
        )
    except requests.Timeout:
        raise
    except requests.RequestException as e:
        raise Exception(f"Gagal menghubungi Groq: {e}")

    try:
        result = response.json()
    except ValueError:
        raise Exception(f"Groq membalas format aneh (HTTP {response.status_code}), coba lagi.")

    if response.status_code == 401:
        raise Exception("API key Groq tidak valid / sudah mati. Cek GROQ_API_KEY kamu.")
    if response.status_code == 429:
        raise Exception("Kena rate limit Groq, tunggu sebentar lalu coba lagi.")
    if "error" in result:
        raise Exception(result["error"].get("message", "Groq error"))
    if response.status_code >= 400:
        raise Exception(f"Groq error HTTP {response.status_code}")
    try:
        return result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise Exception("Response Groq tidak lengkap, coba lagi.")

def extract_video_id(url):
    parsed = urlparse(url)
    if parsed.hostname in ('youtu.be',):
        return parsed.path.strip('/').split('/')[0] or None
    if parsed.hostname in ('www.youtube.com', 'youtube.com', 'm.youtube.com', 'music.youtube.com'):
        if parsed.path == '/watch':
            return parse_qs(parsed.query).get('v', [None])[0]
        for prefix in ('/shorts/', '/live/', '/embed/'):
            if parsed.path.startswith(prefix):
                return parsed.path.split(prefix)[1].split('/')[0] or None
    return None

def _segment_to_dict(s):
    if isinstance(s, dict):
        return {"start": s.get('start', 0), "text": s.get('text', '')}
    return {"start": getattr(s, 'start', 0), "text": getattr(s, 'text', '')}

def fetch_transcript_with_timestamps(video_id):
    from youtube_transcript_api import YouTubeTranscriptApi
    api = YouTubeTranscriptApi()
    raw = None
    last_error = None

    # Cara utama (API versi >= 1.0): fetch langsung, utamakan Indonesia lalu Inggris
    try:
        raw = api.fetch(video_id, languages=['id', 'en'])
    except Exception as e:
        last_error = e

    # Fallback: ambil dari daftar transcript yang tersedia
    if raw is None:
        try:
            tlist = api.list(video_id)
            transcript = None
            for lang in (['id'], ['en']):
                try:
                    transcript = tlist.find_transcript(lang)
                    break
                except Exception:
                    continue
            if transcript is None:
                # Terakhir: terima transcript bahasa apa pun yang pertama ketemu
                for t in tlist:
                    transcript = t
                    break
            if transcript is not None:
                raw = transcript.fetch()
        except Exception as e:
            last_error = e

    if raw is None:
        raise Exception(_short_transcript_error(last_error))

    return [_segment_to_dict(s) for s in raw]

def _short_transcript_error(e):
    """Ubah exception mentah youtube-transcript-api (yang teksnya bisa satu
    halaman penuh) jadi pesan pendek yang enak dibaca user."""
    if e is None:
        return "Transcript tidak tersedia."
    name = type(e).__name__
    mapping = {
        "IpBlocked": "YouTube memblokir IP server",
        "RequestBlocked": "YouTube memblokir IP server",
        "TranscriptsDisabled": "transcript dimatikan pemilik video",
        "NoTranscriptFound": "video ini tidak punya transcript",
        "NoTranscriptAvailable": "video ini tidak punya transcript",
        "VideoUnavailable": "video tidak tersedia / private",
        "VideoUnplayable": "video tidak bisa diputar",
    }
    if name in mapping:
        return mapping[name]
    return f"{name}: {str(e)[:120]}"

def _clean_segments(raw_items, start_key="start", scale=1.0):
    """Normalisasi daftar segmen dari API pihak ketiga ke format internal."""
    segments = []
    for item in raw_items or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        try:
            start = float(item.get(start_key, 0)) / scale
        except (TypeError, ValueError):
            continue
        if text:
            segments.append({"start": start, "text": text})
    segments.sort(key=lambda s: s["start"])
    return segments

def fetch_transcript_transcriptapi(video_id):
    """Fallback 1: TranscriptAPI (transcriptapi.com). Balik None kalau key belum diset."""
    if not TRANSCRIPT_API_KEY:
        return None
    last_error = "tidak ada respons"
    for lang in ("id,en", "asr-id,asr-en"):
        try:
            r = requests.get(
                "https://transcriptapi.com/api/v2/youtube/transcript",
                params={"video_url": video_id, "language": lang},
                headers={"Authorization": f"Bearer {TRANSCRIPT_API_KEY}"},
                timeout=30,
            )
            if r.status_code == 200:
                segments = _clean_segments(r.json().get("transcript"))
                if segments:
                    return segments
            last_error = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last_error = str(e)[:100]
    raise Exception(f"TranscriptAPI gagal ({last_error})")

def fetch_transcript_supadata(video_id):
    """Fallback 2: Supadata (supadata.ai). offset/duration mereka milidetik.
    Balik None kalau key belum diset."""
    if not SUPADATA_API_KEY:
        return None
    last_error = "tidak ada respons"
    for lang in ("id", "en"):
        try:
            r = requests.get(
                "https://api.supadata.ai/v1/transcript",
                params={
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "mode": "native",
                    "lang": lang,
                    "text": "false",
                },
                headers={"x-api-key": SUPADATA_API_KEY},
                timeout=30,
            )
            if r.status_code == 200:
                content = r.json().get("content")
                if isinstance(content, list):
                    segments = _clean_segments(content, start_key="offset", scale=1000.0)
                    if segments:
                        return segments
            last_error = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last_error = str(e)[:100]
    raise Exception(f"Supadata gagal ({last_error})")

_TS_PATTERN = r'(?:(\d{1,2}):)?([0-5]?\d):([0-5]\d)'

def _ts_to_seconds(h, m, s):
    return int(h or 0) * 3600 + int(m) * 60 + int(s)

def parse_ts(ts):
    """Parse 'M:SS' / 'H:MM:SS' / detik polos jadi float detik. None kalau invalid."""
    if isinstance(ts, (int, float)):
        return float(ts)
    m = re.match(r'^\s*(?:(\d{1,2}):)?([0-5]?\d):([0-5]\d)\s*$', str(ts or ""))
    if m:
        return float(_ts_to_seconds(m.group(1), m.group(2), m.group(3)))
    try:
        return float(str(ts).strip())
    except (TypeError, ValueError):
        return None

def parse_manual_transcript(text):
    """Parse transcript yang ditempel user. Mendukung:
    - WebVTT / SRT (ada panah -->)
    - Format salinan panel transkrip YouTube: baris "0:00 teks" atau
      timestamp dan teks bergantian baris."""
    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        return []

    segments = []
    if "-->" in text:
        cue_re = re.compile(_TS_PATTERN + r'[.,]\d{1,3}\s*-->')
        matches = list(cue_re.finditer(text))
        for i, mt in enumerate(matches):
            start = _ts_to_seconds(mt.group(1), mt.group(2), mt.group(3))
            end_pos = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            block = text[mt.end():end_pos]
            # Sisa baris pertama setelah "-->" adalah timestamp selesai; buang.
            if "\n" in block:
                block = block.split("\n", 1)[1]
            lines = []
            for ln in block.splitlines():
                ln = re.sub(r'<[^>]+>', '', ln).strip()
                if ln and not ln.isdigit():
                    lines.append(ln)
            if lines:
                segments.append({"start": start, "text": " ".join(lines)})
        segments.sort(key=lambda s: s["start"])
        return segments

    line_re = re.compile(r'^[\[]?' + _TS_PATTERN + r'[\]]?\s*(.*)$')
    pending = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = line_re.match(line)
        if m:
            start = _ts_to_seconds(m.group(1), m.group(2), m.group(3))
            rest = m.group(4).strip()
            if rest:
                segments.append({"start": start, "text": rest})
                pending = None
            else:
                pending = start
        elif pending is not None:
            segments.append({"start": pending, "text": line})
            pending = None
        elif segments:
            segments[-1]["text"] += " " + line
    segments.sort(key=lambda s: s["start"])
    return segments

def get_transcript_segments(video_id, manual_text=""):
    """Sumber transcript berurutan: tempelan manual user -> library langsung
    (gratis, jalan dari IP rumahan) -> TranscriptAPI -> Supadata.
    Balik (segments, sumber)."""
    if manual_text:
        segments = parse_manual_transcript(manual_text)
        if len(segments) >= 5:
            return segments, "manual"

    try:
        return fetch_transcript_with_timestamps(video_id), "youtube"
    except Exception as e:
        direct_error = str(e)

    def _brief(msg):
        return " ".join(str(msg).split())[:120]

    errors = [_brief(direct_error)]
    for fn in (fetch_transcript_transcriptapi, fetch_transcript_supadata):
        try:
            segments = fn(video_id)
            if segments:
                return segments, fn.__name__.replace("fetch_transcript_", "")
        except Exception as e:
            errors.append(_brief(e))

    detail = "; ".join(errors)[:300]
    raise Exception(
        "Gagal ambil transcript otomatis. YouTube memblokir IP server (Vercel), "
        "dan API fallback belum aktif/gagal. Solusi: tempel transcript manual "
        "(kolom di bawah link video), atau isi TRANSCRIPT_API_KEY / SUPADATA_API_KEY "
        f"di Environment Variables. Detail: {detail}"
    )

def format_time(seconds):
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

def segments_to_windows(segments, window_sec=60, step_sec=30):
    if not segments:
        return []
    max_time = segments[-1]['start'] + 10
    windows = []
    t = 0
    while t < max_time:
        texts = [s['text'] for s in segments if t <= s['start'] < t + window_sec]
        if texts:
            windows.append({"start": t, "end": min(t + window_sec, max_time), "text": ' '.join(texts)})
        t += step_sec
    return windows

def parse_clips(raw_text):
    """Parse output model jadi list clip yang bersih. Model diminta membalas
    JSON object {"clips": [...]}, tapi tetap disiapkan fallback ekstraksi array."""
    data = None
    try:
        data = json.loads(raw_text)
    except (ValueError, TypeError):
        match = re.search(r'\[.*\]', raw_text or '', re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
            except ValueError:
                data = None
    if isinstance(data, dict):
        data = data.get("clips")
    if not isinstance(data, list):
        return None

    clips = []
    for c in data:
        if not isinstance(c, dict):
            continue
        start = str(c.get("start", "")).strip()
        end = str(c.get("end", "")).strip()
        hook = str(c.get("hook", "")).strip()
        if not start or not end or not hook:
            continue
        try:
            score = int(round(float(c.get("score", 5))))
        except (ValueError, TypeError):
            score = 5
        clips.append({
            "start": start,
            "end": end,
            "hook": hook,
            "hashtags": str(c.get("hashtags", "")).strip(),
            "reason": str(c.get("reason", "")).strip(),
            "score": max(1, min(10, score)),
        })
    return clips or None

HOOK_STYLE_GUIDE = {
    "VTuber / Vstreamer": """Gaya hook VTuber/streamer: casual, ekspresif, boleh pake bahasa gaul berat. Fokus ke reaksi dan ekspresi streamer.
Contoh: "dia nangis beneran pas momen ini... gue ga nyangka 😭", "chat langsung rame parah pas ini kejadian HAHAHA" """,

    "Gaming": """Gaya hook gaming: hype, dramatis, fokus ke momen gameplay atau reaksi. Boleh pake istilah gaming.
Contoh: "ini clutch paling gila yang pernah gue liat seumur hidup", "dia solo semua musuh sendirian... caranya gila banget" """,

    "IRL / Just Chatting": """Gaya hook IRL: relatable, conversational, kayak lagi cerita ke temen.
Contoh: "tolong dia ngomong apa barusan 💀", "moment paling awkward di stream dan dia malah ketawa" """,

    "Olahraga / Highlight": """Gaya hook olahraga: dramatis tapi natural, fokus ke momen krusial, bisa pake nama pemain.
Contoh: "gue rewind ini 10 kali dan masih ga percaya itu bisa masuk", "detik-detik yang bikin semua orang di stadion diem seketika" """,

    "Podcast": """Gaya hook podcast: bikin penasaran, fokus ke statement mengejutkan atau insight yang jarang dibahas.
Contoh: "dia baru aja ngungkapin sesuatu yang belum pernah diomongin ke siapapun", "pendapat dia soal ini bikin gue mikir ulang segalanya" """,

    "Edukasi": """Gaya hook edukasi: bikin penasaran, frame kayak 'fakta yang bikin kaget' atau 'hal yang kamu ga tau'.
Contoh: "ternyata yang selama ini kita percaya itu salah total", "cara kerja ini bikin gue sadar betapa bodohnya gue selama ini 😭" """,

    "Musik / Concert": """Gaya hook musik: emosional, vivid, fokus ke momen yang bikin bulu kuduk berdiri.
Contoh: "bagian ini setiap kali gue denger selalu bikin merinding", "dia improvisasi di sini dan hasilnya luar biasa banget" """,

    "Vlog": """Gaya hook vlog: personal, storytelling, bikin penonton ngerasa ikut ngalamin.
Contoh: "gue ga nyangka hari itu bakal berakhir kayak gini", "ini momen yang bikin gue sadar harus berubah" """,

    "Berita / Current Events": """Gaya hook berita: to the point tapi tetap bikin penasaran, hindari judul koran.
Contoh: "tunggu sampai lo denger apa yang dia bilang di bagian ini", "reaksi dia setelah denger kabar ini bikin semua orang kaget" """,

    "Review / Tech": """Gaya hook review/tech: fokus ke temuan mengejutkan atau perbandingan yang bikin penasaran.
Contoh: "gue ga nyangka hasilnya bakal separah ini pas ditest", "ini alasan kenapa gue ga bakal rekomendasiin produk ini ke siapapun" """,

    "Variety / Campuran": """Gaya hook variety: fleksibel, fokus ke momen paling memorable atau mengejutkan.
Contoh: "ini momen yang bikin semua orang langsung diem", "ga ada yang nyangka ini bakal kejadian hari ini" """
}

# ---------- Database (riwayat & jobs) ----------

_db_lock = threading.Lock()

def db_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    with _db_lock, db_conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id TEXT, url TEXT, platform TEXT, content_type TEXT,
            source TEXT, clips TEXT, segments TEXT, created_at TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            video_id TEXT, status TEXT, message TEXT,
            file TEXT, error TEXT, created_at TEXT)""")

def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def save_history(video_id, url, platform, content_type, source, clips, segments):
    with _db_lock, db_conn() as conn:
        cur = conn.execute(
            "INSERT INTO history (video_id, url, platform, content_type, source, clips, segments, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (video_id, url, platform, content_type, source,
             json.dumps(clips), json.dumps(segments[:3000]), _now_iso()))
        return cur.lastrowid

def list_history():
    with _db_lock, db_conn() as conn:
        rows = conn.execute(
            "SELECT id, video_id, url, platform, content_type, source, clips, created_at FROM history ORDER BY id DESC LIMIT 50").fetchall()
    out = []
    for r in rows:
        try:
            n_clips = len(json.loads(r["clips"] or "[]"))
        except ValueError:
            n_clips = 0
        out.append({"id": r["id"], "video_id": r["video_id"], "url": r["url"],
                    "platform": r["platform"], "content_type": r["content_type"],
                    "source": r["source"], "n_clips": n_clips, "created_at": r["created_at"]})
    return out

def get_history(hid):
    with _db_lock, db_conn() as conn:
        r = conn.execute("SELECT * FROM history WHERE id = ?", (hid,)).fetchone()
    if not r:
        return None
    return {"id": r["id"], "video_id": r["video_id"], "url": r["url"],
            "platform": r["platform"], "content_type": r["content_type"],
            "source": r["source"],
            "clips": json.loads(r["clips"] or "[]"),
            "segments": json.loads(r["segments"] or "[]"),
            "created_at": r["created_at"]}

def delete_history(hid):
    with _db_lock, db_conn() as conn:
        conn.execute("DELETE FROM history WHERE id = ?", (hid,))

def create_job(job_id, video_id):
    with _db_lock, db_conn() as conn:
        conn.execute(
            "INSERT INTO jobs (id, video_id, status, message, file, error, created_at) VALUES (?,?,?,?,?,?,?)",
            (job_id, video_id, "queued", "Antrian…", "", "", _now_iso()))

def update_job(job_id, status=None, message=None, file=None, error=None):
    fields, values = [], []
    for col, val in (("status", status), ("message", message), ("file", file), ("error", error)):
        if val is not None:
            fields.append(f"{col} = ?")
            values.append(val)
    if not fields:
        return
    values.append(job_id)
    with _db_lock, db_conn() as conn:
        conn.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?", values)

def get_job(job_id):
    with _db_lock, db_conn() as conn:
        r = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not r:
        return None
    return {"id": r["id"], "video_id": r["video_id"], "status": r["status"],
            "message": r["message"], "file": r["file"], "error": r["error"],
            "created_at": r["created_at"]}

def list_done_jobs():
    with _db_lock, db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status = 'done' ORDER BY created_at DESC LIMIT 50").fetchall()
    out = []
    for r in rows:
        path = os.path.join(CLIPS_DIR, r["file"] or "")
        if r["file"] and os.path.exists(path):
            out.append({"id": r["id"], "video_id": r["video_id"], "file": r["file"],
                        "size": os.path.getsize(path), "created_at": r["created_at"]})
    return out

try:
    db_init()
except Exception:
    pass

# ---------- Potong video (yt-dlp + ffmpeg) ----------

def _srt_ts(sec):
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def build_srt(segments, start, end):
    """Bikin SRT khusus rentang [start, end], timestamp digeser mulai dari 0."""
    cues = []
    segs = [s for s in segments if start - 1 <= float(s.get("start", 0)) < end]
    for i, s in enumerate(segs):
        s_start = float(s.get("start", 0))
        s_end = float(segs[i + 1].get("start", s_start + 4)) if i + 1 < len(segs) else s_start + 4
        cue_start = max(s_start, start) - start
        cue_end = min(s_end, end) - start
        text = " ".join(str(s.get("text", "")).split())
        if text and cue_end > cue_start:
            cues.append(f"{len(cues) + 1}\n{_srt_ts(cue_start)} --> {_srt_ts(cue_end)}\n{text}")
    return "\n\n".join(cues) + ("\n" if cues else "")

def ensure_source_video(video_id):
    cache = os.path.join(CACHE_DIR, f"{video_id}.mp4")
    if os.path.exists(cache) and os.path.getsize(cache) > 10_000:
        return cache
    cmd = find_ytdlp_cmd()
    if not cmd:
        raise Exception("yt-dlp belum terinstall. Di Termux: pip install yt-dlp (sudah termasuk requirements).")
    full = cmd + [
        "-f", "bv*[height<=720]+ba/b[height<=720]/b",
        "--merge-output-format", "mp4",
        "--no-playlist", "--no-warnings",
        "-o", cache,
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    proc = subprocess.run(full, capture_output=True, text=True, timeout=900)
    if proc.returncode != 0 or not os.path.exists(cache):
        tail = " ".join((proc.stderr or "").split())[-220:]
        raise Exception(f"Gagal download video sumber: {tail}")
    return cache

def _escape_filter_path(path):
    return path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")

def run_cut_job(job_id, video_id, start, end, vertical, captions, segments):
    ffmpeg = find_ffmpeg()
    try:
        if not ffmpeg:
            raise Exception("ffmpeg tidak ditemukan. Di Termux: pkg install ffmpeg lalu jalanin lagi installer-nya.")
        update_job(job_id, status="working", message="Ngambil video sumber… (download pertama agak lama, berikutnya cached)")
        src = ensure_source_video(video_id)

        tag = "vertikal" if vertical else "biasa"
        out_name = f"{video_id}_{int(start)}-{int(end)}_{tag}_{job_id[:6]}.mp4"
        out_path = os.path.join(CLIPS_DIR, out_name)
        tmp_path = out_path + ".tmp.mp4"

        vf_parts = []
        if vertical:
            vf_parts.append(
                "split=2[a][b];"
                "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:5[bg];"
                "[b]scale=1080:-2[fg];"
                "[bg][fg]overlay=(W-w)/2:(H-h)/2"
            )
        update_job(job_id, message="Lagi motong video…")
        cmd = [ffmpeg, "-y", "-ss", f"{start}", "-to", f"{end}", "-i", src,
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
               "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"]
        if vf_parts:
            cmd += ["-vf", ",".join(vf_parts)]
        cmd += [tmp_path]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
        if proc.returncode != 0 or not os.path.exists(tmp_path):
            tail = " ".join((proc.stderr or "").split())[-220:]
            raise Exception(f"ffmpeg gagal motong: {tail}")
        os.replace(tmp_path, out_path)

        note = ""
        if captions and segments:
            srt_text = build_srt(segments, start, end)
            if srt_text.strip():
                update_job(job_id, message="Nempel caption…")
                srt_path = os.path.join(SRT_DIR, f"{job_id}.srt")
                with open(srt_path, "w", encoding="utf-8") as f:
                    f.write(srt_text)
                cap_path = out_path + ".cap.mp4"
                cap_cmd = [ffmpeg, "-y", "-i", out_path,
                           "-vf", f"subtitles='{_escape_filter_path(srt_path)}'",
                           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                           "-c:a", "copy", cap_path]
                proc2 = subprocess.run(cap_cmd, capture_output=True, text=True, timeout=1200)
                if proc2.returncode == 0 and os.path.exists(cap_path):
                    os.replace(cap_path, out_path)
                else:
                    note = " (caption gagal nempel, video tetap jadi)"
                    if os.path.exists(cap_path):
                        os.remove(cap_path)
        update_job(job_id, status="done", message=f"Selesai!{note}", file=out_name)
    except Exception as e:
        update_job(job_id, status="error", message="", error=str(e)[:400])

# ---------- Routes ----------

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/capabilities")
def capabilities():
    ffmpeg = find_ffmpeg()
    ytdlp = find_ytdlp_cmd()
    return jsonify({
        "ffmpeg": bool(ffmpeg),
        "ytdlp": bool(ytdlp),
        "cutting": bool(ffmpeg and ytdlp),
        "transcript_api": bool(TRANSCRIPT_API_KEY or SUPADATA_API_KEY),
        "serverless": bool(os.getenv("VERCEL")),
    })

@app.route("/analyze", methods=["POST"])
def analyze():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()
    platform = data.get("platform", "YouTube Shorts")
    content_type = data.get("contentType", "Variety / Campuran")
    try:
        max_clips = int(data.get("maxClips", 5))
    except (ValueError, TypeError):
        max_clips = 5
    max_clips = max(1, min(10, max_clips))
    try:
        target_duration = int(data.get("targetDuration", 60))
    except (ValueError, TypeError):
        target_duration = 60
    target_duration = max(15, min(180, target_duration))

    if not url:
        return jsonify({"error": "URL tidak boleh kosong!"}), 400
    video_id = extract_video_id(url)
    if not video_id:
        return jsonify({"error": "URL bukan video YouTube yang valid!"}), 400

    manual_text = str(data.get("transcript", ""))[:200_000]

    try:
        segments, transcript_source = get_transcript_segments(video_id, manual_text)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    if len(segments) < 5:
        return jsonify({"error": "Transcript terlalu pendek untuk dianalisis!"}), 400

    windows = segments_to_windows(segments, window_sec=60, step_sec=30)
    if len(windows) > 40:
        step = len(windows) // 40
        windows = windows[::step][:40]

    windows_text = ""
    for w in windows:
        windows_text += f"[{format_time(w['start'])} - {format_time(w['end'])}] {w['text'][:300]}\n\n"

    hook_guide = HOOK_STYLE_GUIDE.get(content_type, HOOK_STYLE_GUIDE["Variety / Campuran"])

    prompt = f"""Lo adalah clipper berpengalaman yang spesialis konten {content_type} dan udah tau persis cara bikin hook yang bikin orang stop scrolling di {platform}.

{hook_guide}

Platform target: {platform}
Jenis konten: {content_type}
Target durasi tiap clip: sekitar {target_duration} detik (end = start + kurang lebih {target_duration} detik; boleh meleset dikit demi momen yang utuh).

Transcript video dengan timestamp:
---
{windows_text}
---

Tugas lo: pilih {max_clips} momen terbaik yang paling layak dijadiin clip.

Untuk tiap rekomendasi berikan:
- "hook": judul/caption NATURAL sesuai gaya {content_type} di atas. JANGAN kaku kayak judul berita. Panjang 60-120 karakter.
- "hashtags": 8-12 hashtag relevan untuk {platform}, mix antara hashtag besar dan spesifik. Format: "#tag1 #tag2 #tag3"
- "start": timestamp mulai (M:SS atau H:MM:SS)
- "end": timestamp selesai
- "reason": 1-2 kalimat kenapa momen ini bagus, tulis santai
- "score": potensi viral 1-10

Balas HANYA dengan JSON object persis seperti ini, tanpa teks lain:
{{"clips": [{{"start":"0:00","end":"0:45","hook":"...","hashtags":"#tag1 #tag2","reason":"...","score":8}}]}}"""

    try:
        raw = call_groq(prompt, max_tokens=2500)
        clips = parse_clips(raw)
        if not clips:
            return jsonify({"error": "Format response tidak valid, coba lagi!"}), 500
        history_id = None
        try:
            history_id = save_history(video_id, url, platform, content_type, transcript_source, clips, segments)
        except Exception:
            pass
        clean_segments = [{"start": float(s.get("start", 0)), "text": str(s.get("text", ""))} for s in segments[:3000]]
        return jsonify({"clips": clips, "video_id": video_id, "total_segments": len(segments),
                        "transcript_source": transcript_source, "segments": clean_segments,
                        "history_id": history_id})
    except requests.Timeout:
        return jsonify({"error": "Timeout, coba lagi!"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/clips", methods=["POST"])
def create_clip():
    if not find_ffmpeg() or not find_ytdlp_cmd():
        return jsonify({"error": "Fitur potong video butuh ffmpeg + yt-dlp di server. Di Vercel (serverless) fitur ini mati — jalanin app-nya di Termux ya."}), 400
    data = request.get_json(silent=True) or {}
    video_id = str(data.get("video_id", "")).strip()
    if not re.match(r'^[A-Za-z0-9_-]{6,20}$', video_id):
        return jsonify({"error": "video_id tidak valid."}), 400
    start = parse_ts(data.get("start"))
    end = parse_ts(data.get("end"))
    if start is None or end is None or end <= start:
        return jsonify({"error": "Timestamp start/end tidak valid."}), 400
    if end - start > 600:
        return jsonify({"error": "Durasi clip maksimal 10 menit."}), 400
    segments = data.get("segments") or []
    if not isinstance(segments, list):
        segments = []
    segments = segments[:3000]
    job_id = uuid.uuid4().hex[:12]
    try:
        create_job(job_id, video_id)
    except Exception as e:
        return jsonify({"error": f"Gagal bikin job: {e}"}), 500
    t = threading.Thread(
        target=run_cut_job,
        args=(job_id, video_id, float(start), float(end),
              bool(data.get("vertical")), bool(data.get("captions")), segments),
        daemon=True)
    t.start()
    return jsonify({"job_id": job_id, "status": "queued"})

@app.route("/api/jobs/<job_id>")
def job_status(job_id):
    job = get_job(job_id)
    if not job:
        abort(404)
    if job["status"] == "done" and job["file"]:
        job["download_url"] = f"/download/{job_id}"
    return jsonify(job)

@app.route("/api/clips", methods=["GET"])
def clips_list():
    return jsonify({"clips": list_done_jobs()})

@app.route("/download/<job_id>")
def download_clip(job_id):
    job = get_job(job_id)
    if not job or job["status"] != "done" or not job["file"]:
        abort(404)
    path = os.path.join(CLIPS_DIR, os.path.basename(job["file"]))
    if not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=job["file"])

@app.route("/api/history", methods=["GET"])
def history_list():
    return jsonify({"history": list_history()})

@app.route("/api/history/<int:hid>", methods=["GET"])
def history_get(hid):
    h = get_history(hid)
    if not h:
        abort(404)
    return jsonify(h)

@app.route("/api/history/<int:hid>", methods=["DELETE"])
def history_delete(hid):
    delete_history(hid)
    return jsonify({"ok": True})

@app.route("/api/history/<int:hid>/export")
def history_export(hid):
    h = get_history(hid)
    if not h:
        abort(404)
    lines = [f"ClipRadar — hasil analisis",
             f"Video: https://www.youtube.com/watch?v={h['video_id']}",
             f"Platform: {h['platform']} | Jenis: {h['content_type']} | Sumber transcript: {h['source']}",
             f"Tanggal: {h['created_at']}", ""]
    for i, c in enumerate(h["clips"], 1):
        lines.append(f"{i}. [{c['start']} -> {c['end']}] (skor {c['score']}/10)")
        lines.append(f"   Hook: {c['hook']}")
        if c.get("hashtags"):
            lines.append(f"   Hashtag: {c['hashtags']}")
        if c.get("reason"):
            lines.append(f"   Alasan: {c['reason']}")
        lines.append("")
    from io import BytesIO
    return send_file(BytesIO("\n".join(lines).encode("utf-8")),
                     as_attachment=True,
                     download_name=f"clipradar-{h['video_id']}.txt",
                     mimetype="text/plain")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5002)), debug=os.environ.get("DEBUG") == "1")
