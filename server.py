import re
import io
import threading
import uuid
import wave
from typing import Annotated, Literal

import markdown as md
from bs4 import BeautifulSoup
import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Response, UploadFile, File
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from kokoro import KPipeline

SAMPLE_RATE = 24000
app = FastAPI()
sessions: dict[str, list[str]] = {}
_pipeline = None
# Serializes synthesis (and the lazy model load inside it). torch already uses
# every core for one sentence, so parallel requests only slow each other down.
_synth_lock = threading.Lock()

# British voices, matching the pipeline's lang_code="b".
VOICES = {
    "bf_emma": "Emma (F)",
    "bf_alice": "Alice (F)",
    "bf_lily": "Lily (F)",
    "bm_george": "George (M)",
    "bm_daniel": "Daniel (M)",
    "bm_lewis": "Lewis (M)",
    "bm_fable": "Fable (M)",
}
Voice = Literal[tuple(VOICES)]


def get_pipeline() -> KPipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = KPipeline(lang_code="b", repo_id="hexgrad/Kokoro-82M")
    return _pipeline


PAUSE_MARKER = ""
PAUSE_DURATION = 0.4  # seconds of silence per newline

# Words whose trailing period doesn't end a sentence.
ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "e.g", "i.e", "fig"}
# Terminal punctuation, optional closing quotes/brackets, whitespace, then
# something that can start a sentence.
SENTENCE_BOUNDARY = re.compile(r"[.!?]+[\"'”’)\]]*\s+(?=[\"'“‘(\[]?[A-Z0-9])")


def split_line(line: str) -> list[str]:
    parts, start = [], 0
    for m in SENTENCE_BOUNDARY.finditer(line):
        word = line[start:m.start()].split()[-1].lstrip("\"'“‘([")
        if m.group().startswith(".") and (
            word.lower() in ABBREVIATIONS or re.fullmatch(r"[A-Z]", word)
        ):
            continue
        parts.append(line[start:m.end()].strip())
        start = m.end()
    parts.append(line[start:].strip())
    return [p for p in parts if p]


def split_sentences(text: str) -> list[str]:
    result = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            if result and result[-1] != PAUSE_MARKER:
                result.append(PAUSE_MARKER)
            continue
        result.extend(split_line(line))
        result.append(PAUSE_MARKER)
    while result and result[-1] == PAUSE_MARKER:
        result.pop()
    return result


def generate_silence_wav(duration: float) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"\x00" * int(SAMPLE_RATE * duration) * 2)
    return buf.getvalue()


LEAD_IN_FRAMES = int(SAMPLE_RATE * 0.05)  # 50ms lead-in to avoid clipping


def to_pcm16(audio: np.ndarray) -> bytes:
    # Kokoro occasionally overshoots ±1.0; clip so peaks saturate instead of wrapping.
    return (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tobytes()


def generate_wav(text: str, voice: str, speed: float) -> bytes:
    with _synth_lock:
        pipeline = get_pipeline()
        chunks = [
            r.audio.numpy()
            for r in pipeline(text, voice=voice, speed=speed, split_pattern=None)
            if r.audio is not None
        ]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"\x00" * LEAD_IN_FRAMES * 2)
        for chunk in chunks:
            wf.writeframes(to_pcm16(chunk))
    return buf.getvalue()


class PrepareRequest(BaseModel):
    text: str


@app.post("/api/prepare")
def prepare(req: PrepareRequest):
    session_id = str(uuid.uuid4())
    sentences = split_sentences(req.text)
    sessions[session_id] = sentences
    return {"session_id": session_id, "sentences": sentences}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    try:
        content = (await file.read()).decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File must be UTF-8 text")
    content = re.sub(r'!\[.*?\]\(data:[^)]+\)', '', content)
    return {"text": content}


class ProcessRequest(BaseModel):
    text: str


def strip_markdown(text: str) -> str:
    # Remove base64 images before parsing (they're huge and useless)
    text = re.sub(r'!\[.*?\]\(data:[^)]+\)', '', text)
    # Strip Pandoc-style attributes e.g. {#some-id .class key=val}. Only
    # brace groups made of such tokens, and never the newline before them.
    attr = r'(?:[#.][\w-]+|[\w-]+=(?:"[^"]*"|[^\s}]*))'
    text = re.sub(rf'[ \t]*\{{{attr}(?:\s+{attr})*\}}', '', text)
    # Convert markdown → HTML → plain text
    html = md.markdown(text, extensions=['tables', 'fenced_code', 'def_list'])
    soup = BeautifulSoup(html, 'html.parser')
    # Drop any remaining img tags entirely
    for img in soup.find_all('img'):
        img.decompose()

    # BeautifulSoup's ``separator`` is inserted between *all* text nodes. That
    # turns inline markup such as ``some **bold** text`` into three lines. Use
    # explicit markers for meaningful Markdown boundaries instead, then fold
    # source-line wrapping inside paragraphs into spaces.
    hard_break = '\ue000'
    block_break = '\ue001'

    for pre in soup.find_all('pre'):
        pre.string = pre.get_text().replace('\n', hard_break)
    for br in soup.find_all('br'):
        br.replace_with(hard_break)

    for row in soup.find_all('tr'):
        cells = row.find_all(['th', 'td'], recursive=False)
        for cell in cells[:-1]:
            cell.insert_after(' | ')

    block_tags = [
        'address', 'article', 'aside', 'blockquote', 'dd', 'div', 'dl', 'dt',
        'figcaption', 'figure', 'footer', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
        'header', 'li', 'main', 'nav', 'ol', 'p', 'pre', 'section', 'table',
        'tr', 'ul',
    ]
    for block in soup.find_all(block_tags):
        block.insert_after(block_break)

    plain = soup.get_text()
    plain = re.sub(r'\s+', ' ', plain)
    plain = re.sub(rf' *{re.escape(hard_break)} *', '\n', plain)
    plain = re.sub(rf' *{re.escape(block_break)} *', '\n\n', plain)
    # Collapse excess blank lines
    plain = re.sub(r'\n{3,}', '\n\n', plain)
    return plain.strip()


@app.post("/api/process")
def process(req: ProcessRequest):
    return {"text": strip_markdown(req.text)}


@app.get("/api/audio")
def audio(
    session_id: str,
    index: int,
    voice: Voice = "bf_emma",
    speed: Annotated[float, Query(ge=0.5, le=2.0)] = 1.0,
):
    if session_id not in sessions:
        return Response(status_code=404)
    sents = sessions[session_id]
    if index < 0 or index >= len(sents):
        return Response(status_code=404)
    if sents[index] == PAUSE_MARKER:
        wav_data = generate_silence_wav(PAUSE_DURATION)
    else:
        wav_data = generate_wav(sents[index], voice=voice, speed=speed)
    return Response(content=wav_data, media_type="audio/wav")


@app.get("/")
def root():
    options = "\n".join(
        f'<option value="{v}"{" selected" if v == "bf_emma" else ""}>{label}</option>'
        for v, label in VOICES.items()
    )
    return HTMLResponse(HTML.replace("<!--VOICE_OPTIONS-->", options))


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kokoro Reader</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: #f0f2f5;
  color: #111;
  min-height: 100vh;
}

header {
  background: white;
  border-bottom: 1px solid #e5e7eb;
  padding: 14px 24px;
  display: flex;
  align-items: center;
  gap: 10px;
  position: sticky;
  top: 0;
  z-index: 10;
}
header h1 { font-size: 1rem; font-weight: 600; }

.main {
  max-width: 780px;
  margin: 0 auto;
  padding: 24px 24px 110px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.card {
  background: white;
  border-radius: 12px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.08);
}

.input-card { padding: 16px; }

textarea {
  width: 100%;
  height: 120px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 12px;
  font-size: 14px;
  font-family: inherit;
  resize: vertical;
  line-height: 1.5;
}
textarea:focus { outline: none; border-color: #6366f1; }

.input-row {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
}

.btn-load {
  background: #6366f1;
  color: white;
  border: none;
  border-radius: 8px;
  padding: 9px 18px;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
}
.btn-load:hover { background: #4f46e5; }

.btn-upload {
  background: white;
  color: #374151;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 9px 18px;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
}
.btn-upload:hover { background: #f9fafb; }
.btn-upload:disabled { opacity: 0.5; cursor: not-allowed; }

.reader-card { padding: 28px; min-height: 180px; }

#readerContent { line-height: 2; font-size: 16.5px; color: #1a1a1a; }

.placeholder { color: #9ca3af; font-size: 14px; }

.sentence {
  cursor: pointer;
  border-radius: 4px;
  padding: 2px 3px;
  margin: 0 -3px;
  transition: background 0.1s;
}
.sentence:hover { background: #f3f4f6; }
.sentence.active { background: #fef08a; }
.sentence.played { color: #b0b7c3; }

/* bottom controls bar */
.controls {
  position: fixed;
  bottom: 0; left: 0; right: 0;
  background: white;
  border-top: 1px solid #e5e7eb;
  padding: 12px 24px;
  display: flex;
  align-items: center;
  gap: 18px;
  flex-wrap: wrap;
}

.btn-play {
  width: 44px; height: 44px;
  border-radius: 50%;
  background: #6366f1;
  color: white;
  border: none;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  transition: background 0.15s;
}
.btn-play:hover { background: #4f46e5; }
.btn-play:disabled { background: #d1d5db; cursor: not-allowed; }

.ctrl-group { display: flex; align-items: center; gap: 8px; }
.ctrl-label { font-size: 12px; font-weight: 500; color: #6b7280; white-space: nowrap; }

input[type=range] { width: 90px; accent-color: #6366f1; cursor: pointer; }

.speed-val { font-size: 13px; color: #374151; min-width: 32px; }

select {
  border: 1px solid #e5e7eb;
  border-radius: 6px;
  padding: 5px 8px;
  font-size: 13px;
  background: white;
  cursor: pointer;
}
select:focus { outline: none; border-color: #6366f1; }

.status { margin-left: auto; font-size: 12px; color: #9ca3af; }

.embed header, .embed .input-card { display: none; }
</style>
</head>
<body>

<header>
  <svg width="20" height="20" viewBox="0 0 24 24" fill="#6366f1">
    <path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02z"/>
  </svg>
  <h1>Kokoro Reader</h1>
</header>

<div class="main">
  <div class="card input-card">
    <textarea id="textInput" placeholder="Paste your text here…"></textarea>
    <div class="input-row">
      <input type="file" id="fileInput" accept=".md,.txt" style="display:none" onchange="uploadFile(this)">
      <button class="btn-upload" id="uploadBtn" onclick="document.getElementById('fileInput').click()">Upload .md</button>
      <button class="btn-load" onclick="processText()">Process Text</button>
    </div>
  </div>

  <div class="card reader-card">
    <div id="readerContent">
      <span class="placeholder">Paste text above and click Load Text to begin.</span>
    </div>
  </div>
</div>

<div class="controls">
  <button class="btn-play" id="playBtn" onclick="togglePlay()" disabled>
    <svg id="iconPlay" width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
      <polygon points="5,3 19,12 5,21"/>
    </svg>
    <svg id="iconPause" width="18" height="18" viewBox="0 0 24 24" fill="currentColor" style="display:none">
      <rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/>
    </svg>
  </button>

  <div class="ctrl-group">
    <span class="ctrl-label">Speed</span>
    <input type="range" id="speedSlider" min="0.5" max="2" step="0.1" value="1.3" oninput="onSpeedChange()">
    <span class="speed-val" id="speedVal">1.3×</span>
  </div>

  <div class="ctrl-group">
    <span class="ctrl-label">Voice</span>
    <select id="voiceSelect" onchange="onVoiceChange()">
      <!--VOICE_OPTIONS-->
    </select>
  </div>

  <span class="status" id="status"></span>
</div>

<script>
  let sessionId = null;
  let sentences = [];
  let currentIndex = 0;
  let isPlaying = false;
  let currentAudio = null;

  const voice = () => document.getElementById('voiceSelect').value;
  const speed = () => parseFloat(document.getElementById('speedSlider').value);
  const cacheKey = (i) => `${sessionId}:${i}:${voice()}:${speed()}`;

  function onSpeedChange() {
    document.getElementById('speedVal').textContent = speed().toFixed(1) + '×';
    savePrefs();
    invalidateCache();
  }

  function onVoiceChange() { savePrefs(); invalidateCache(); }

  // Remember voice and speed across windows. Storage can be unavailable
  // (private mode, blocked site data), in which case the defaults stay.
  function savePrefs() {
    try {
      localStorage.setItem('reader.prefs', JSON.stringify({ voice: voice(), speed: speed() }));
    } catch {}
  }

  function restorePrefs() {
    let prefs;
    try { prefs = JSON.parse(localStorage.getItem('reader.prefs')); } catch {}
    if (!prefs) return;
    const select = document.getElementById('voiceSelect');
    if ([...select.options].some(o => o.value === prefs.voice)) select.value = prefs.voice;
    const slider = document.getElementById('speedSlider');
    if (prefs.speed >= +slider.min && prefs.speed <= +slider.max) slider.value = prefs.speed;
    document.getElementById('speedVal').textContent = speed().toFixed(1) + '×';
  }

  // Embedded in the macOS app: no input card, and the app pushes text in.
  if (new URLSearchParams(location.search).has('embed')) document.body.classList.add('embed');
  restorePrefs();

  // Called by the macOS app with the text the user selected.
  async function readerLoad(text) {
    await loadSentences(text);
    if (sentences.length) playFrom(0);
  }

  async function uploadFile(input) {
    const file = input.files[0];
    if (!file) return;
    input.value = '';
    const form = new FormData();
    form.append('file', file);
    try {
      const res = await fetch('/api/upload', { method: 'POST', body: form });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      document.getElementById('textInput').value = data.text;
      setStatus('File loaded');
    } catch (e) {
      setStatus(`Upload failed: ${e.message}`);
      console.error(e);
    }
  }

  async function processText() {
    const text = document.getElementById('textInput').value.trim();
    if (!text) return;
    const btn = document.querySelector('.btn-load');
    btn.disabled = true;
    btn.textContent = 'Processing…';
    setStatus('Sending to Llama…');
    try {
      const res = await fetch('/api/process', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text })
      });
      const data = await res.json();
      document.getElementById('textInput').value = data.text;
      await loadSentences(data.text);
    } catch (e) {
      setStatus('Processing failed');
      console.error(e);
    } finally {
      btn.disabled = false;
      btn.textContent = 'Process Text';
    }
  }

  async function loadSentences(text) {
    pause();
    const res = await fetch('/api/prepare', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text })
    });
    const data = await res.json();
    sessionId = data.session_id;
    sentences = data.sentences;
    currentIndex = 0;
    invalidateCache();
    renderSentences();
    document.getElementById('playBtn').disabled = false;
    setStatus(`${sentences.length} sentence${sentences.length !== 1 ? 's' : ''}`);
  }

  function renderSentences() {
    const el = document.getElementById('readerContent');
    el.innerHTML = '';
    sentences.forEach((s, i) => {
      if (s === '') {
        const br = document.createElement('br');
        br.dataset.index = i;
        el.appendChild(br);
      } else {
        const span = document.createElement('span');
        span.className = 'sentence';
        span.dataset.index = i;
        span.textContent = s + ' ';
        span.addEventListener('click', () => jumpTo(i));
        el.appendChild(span);
      }
    });
  }

  function highlight(index) {
    document.querySelectorAll('.sentence').forEach(el => {
      const i = parseInt(el.dataset.index);
      el.classList.toggle('active', i === index);
      el.classList.toggle('played', i < index);
    });
    const active = document.querySelector(`.sentence[data-index="${index}"]`);
    if (active) active.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  function clearHighlights() {
    document.querySelectorAll('.sentence').forEach(el => el.classList.remove('active', 'played'));
  }

  // audioReady: key -> Audio element (loaded, ready to play)
  // audioPending: key -> Promise<Audio>
  let audioReady = {};
  let audioPending = {};
  // Bumped whenever playback stops or restarts, so callbacks from an earlier
  // run see they are stale and don't start a second, overlapping playback.
  let playGen = 0;
  // The server synthesizes one sentence at a time, so fetch sequentially:
  // the sentence needed now never queues behind ones needed later.
  const LOOKAHEAD = 4;

  function invalidateCache() {
    Object.values(audioReady).forEach(a => URL.revokeObjectURL(a.src));
    audioReady = {};
    audioPending = {};
  }

  function prefetch(index) {
    const key = cacheKey(index);
    if (audioReady[key]) return Promise.resolve(audioReady[key]);
    if (audioPending[key]) return audioPending[key];
    // A fetch that finishes after invalidateCache() must not land in the new cache.
    const ready = audioReady, pending = audioPending;
    const url = `/api/audio?session_id=${sessionId}&index=${index}&voice=${voice()}&speed=${speed()}`;
    pending[key] = fetch(url)
      .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.blob();
      })
      .then(blob => {
        const audio = new Audio(URL.createObjectURL(blob));
        audio.load();
        if (ready === audioReady) ready[key] = audio;
        return audio;
      })
      .finally(() => { delete pending[key]; });
    return pending[key];
  }

  async function prefetchAhead(index, gen) {
    const end = Math.min(index + LOOKAHEAD, sentences.length);
    for (let i = index; i < end && gen === playGen; i++) {
      try { await prefetch(i); } catch { return; }
    }
  }

  function setupAndPlay(audio, index, gen) {
    if (gen !== playGen) return;
    currentAudio = audio;
    delete audioReady[cacheKey(index)];
    highlight(index);
    setStatus(`${index + 1} / ${sentences.length}`);
    const next = () => {
      URL.revokeObjectURL(audio.src);
      if (gen === playGen) playIndex(index + 1);
    };
    audio.onended = next;
    audio.onerror = next;
    audio.play();
  }

  function playbackFailed(err) {
    pause();
    setStatus('Audio failed, press play to retry');
    console.error(err);
  }

  function playIndex(index) {
    if (!isPlaying) return;
    if (index >= sentences.length) {
      pause();
      clearHighlights();
      currentIndex = 0;
      setStatus('Done');
      return;
    }
    currentIndex = index;
    const gen = playGen;
    const ready = audioReady[cacheKey(index)];
    if (ready) {
      // Play synchronously so a click's user-gesture permission still applies.
      setupAndPlay(ready, index, gen);
      prefetchAhead(index + 1, gen);
    } else {
      prefetch(index).then(
        audio => { setupAndPlay(audio, index, gen); prefetchAhead(index + 1, gen); },
        err => { if (gen === playGen) playbackFailed(err); },
      );
    }
  }

  function playFrom(index) {
    playGen++;
    isPlaying = true;
    setIcon(true);
    playIndex(index);
  }

  function pause() {
    playGen++;
    isPlaying = false;
    setIcon(false);
    if (currentAudio) {
      currentAudio.pause();
      currentAudio = null;
    }
  }

  function togglePlay() {
    if (!sessionId || !sentences.length) return;
    isPlaying ? pause() : playFrom(currentIndex);
  }

  function jumpTo(index) {
    const wasPlaying = isPlaying;
    if (isPlaying) pause();
    currentIndex = index;
    highlight(index);
    if (wasPlaying) playFrom(index);
  }

  function setIcon(playing) {
    document.getElementById('iconPlay').style.display = playing ? 'none' : '';
    document.getElementById('iconPause').style.display = playing ? '' : 'none';
  }

  function setStatus(msg) { document.getElementById('status').textContent = msg; }

  document.addEventListener('keydown', e => {
    if (e.code === 'Space' && e.target.tagName !== 'TEXTAREA') {
      e.preventDefault();
      if (sessionId) togglePlay();
    }
  });
</script>
</body>
</html>
"""

def warm_up():
    # Load the model and default voice up front so the first request isn't slow.
    with _synth_lock:
        get_pipeline().load_voice("bf_emma")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=7860)
    args = parser.parse_args()
    threading.Thread(target=warm_up, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port)
