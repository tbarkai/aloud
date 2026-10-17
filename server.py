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


@app.get("/api/voices")
def voices():
    return [{"id": v, "name": name} for v, name in VOICES.items()]


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
:root {
  --bg: #ffffff;
  --text: #1d1d1f;
  --muted: #86868b;
  --played: #a8a8ad;
  --hairline: rgba(0, 0, 0, 0.1);
  --hover: rgba(0, 0, 0, 0.045);
  --highlight: #fff1a8;
  --accent: #4f46e5;
  --accent-hover: #4338ca;
  --bar: rgba(255, 255, 255, 0.86);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #1c1c1e;
    --text: #ececf0;
    --muted: #8e8e93;
    --played: #6c6c70;
    --hairline: rgba(255, 255, 255, 0.12);
    --hover: rgba(255, 255, 255, 0.06);
    --highlight: rgba(255, 214, 10, 0.26);
    --accent: #7c74ff;
    --accent-hover: #918aff;
    --bar: rgba(28, 28, 30, 0.86);
  }
}

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font: 14px/1.4 -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  -webkit-font-smoothing: antialiased;
}

.page {
  max-width: 720px;
  margin: 0 auto;
  padding: 40px 40px 120px;
}

header { margin-bottom: 20px; }
header h1 { font-size: 15px; font-weight: 600; letter-spacing: -0.01em; }

.input { margin-bottom: 36px; }

textarea {
  width: 100%;
  height: 120px;
  border: 1px solid var(--hairline);
  border-radius: 8px;
  padding: 10px 12px;
  font: inherit;
  line-height: 1.5;
  color: inherit;
  background: transparent;
  resize: vertical;
}
textarea:focus { outline: none; border-color: var(--accent); }

.input-row {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 10px;
}

button.text-btn {
  font: inherit;
  font-weight: 500;
  border-radius: 7px;
  padding: 6px 14px;
  cursor: pointer;
  border: 1px solid var(--hairline);
  background: transparent;
  color: inherit;
}
button.text-btn:hover { background: var(--hover); }
button.text-btn.primary { background: var(--accent); border-color: transparent; color: white; }
button.text-btn.primary:hover { background: var(--accent-hover); }
button.text-btn:disabled { opacity: 0.5; cursor: default; }

#readerContent {
  font-family: ui-serif, 'New York', Charter, Georgia, serif;
  font-size: 19px;
  line-height: 1.7;
}
#readerContent p + p { margin-top: 0.9em; }

.placeholder {
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, sans-serif;
  color: var(--muted);
}

.sentence {
  cursor: pointer;
  border-radius: 3px;
  /* No side padding: any extra width after the final period reads as a trailing space. */
  padding: 1px 0;
  -webkit-box-decoration-break: clone;
  box-decoration-break: clone;
  transition: background 0.12s, color 0.12s;
}
.sentence:hover { background: var(--hover); }
.sentence.played { color: var(--played); }
.sentence.active { background: var(--highlight); color: var(--text); }

.controls {
  position: fixed;
  bottom: 0; left: 0; right: 0;
  background: var(--bar);
  -webkit-backdrop-filter: saturate(180%) blur(16px);
  backdrop-filter: saturate(180%) blur(16px);
  border-top: 1px solid var(--hairline);
}
.controls-inner {
  max-width: 720px;
  margin: 0 auto;
  padding: 10px 40px;
  display: flex;
  align-items: center;
  gap: 20px;
}

.btn-play {
  width: 36px; height: 36px;
  border-radius: 50%;
  background: var(--accent);
  color: white;
  border: none;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  transition: background 0.15s;
}
.btn-play:hover { background: var(--accent-hover); }
.btn-play:disabled { background: var(--hover); color: var(--muted); cursor: default; }

.ctrl-group { display: flex; align-items: center; gap: 8px; }
.ctrl-label { font-size: 12px; color: var(--muted); }

input[type=range] { width: 110px; accent-color: var(--accent); cursor: pointer; }

.speed-val { font-size: 12px; font-variant-numeric: tabular-nums; min-width: 30px; }

select {
  font: inherit;
  font-size: 13px;
  color: inherit;
  background: transparent;
  border: 1px solid var(--hairline);
  border-radius: 6px;
  padding: 3px 6px;
  cursor: pointer;
}

.status {
  margin-left: auto;
  font-size: 12px;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}

body.dragging::after {
  content: 'Drop a .md or .txt file to read it';
  position: fixed;
  inset: 12px;
  display: grid;
  place-items: center;
  border: 2px dashed var(--accent);
  border-radius: 12px;
  background: color-mix(in srgb, var(--bg) 88%, transparent);
  color: var(--accent);
  font-weight: 500;
  pointer-events: none;
  z-index: 20;
}

/* Embedded in the macOS app: settings own the voice, the app supplies text,
   and a native toolbar replaces the controls bar. WebKit already keeps the
   viewport below that toolbar and scrolls content under it. */
.embed header, .embed .input, .embed .controls { display: none; }
.embed .page { padding-top: 28px; padding-bottom: 64px; }
</style>
</head>
<body>

<div class="page">
  <header><h1>Kokoro Reader</h1></header>

  <div class="input">
    <textarea id="textInput" placeholder="Paste your text here…"></textarea>
    <div class="input-row">
      <input type="file" id="fileInput" accept=".md,.txt" style="display:none" onchange="uploadFile(this)">
      <button class="text-btn" id="uploadBtn" onclick="document.getElementById('fileInput').click()">Upload .md</button>
      <button class="text-btn primary btn-load" onclick="processText()">Process Text</button>
    </div>
  </div>

  <div id="readerContent">
    <p class="placeholder" id="placeholder">Paste text above and click Process Text, or drop a .md or .txt file here.</p>
  </div>
</div>

<div class="controls">
  <div class="controls-inner">
    <button class="btn-play" id="playBtn" onclick="togglePlay()" disabled aria-label="Play">
      <svg id="iconPlay" width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
        <polygon points="6,3 20,12 6,21"/>
      </svg>
      <svg id="iconPause" width="14" height="14" viewBox="0 0 24 24" fill="currentColor" style="display:none">
        <rect x="5" y="4" width="5" height="16" rx="1"/><rect x="14" y="4" width="5" height="16" rx="1"/>
      </svg>
    </button>

    <div class="ctrl-group">
      <span class="ctrl-label">Speed</span>
      <input type="range" id="speedSlider" min="0.5" max="2" step="0.1" value="1.3" oninput="onSpeedChange()">
      <span class="speed-val" id="speedVal">1.3×</span>
    </div>

    <div class="ctrl-group voice-group">
      <span class="ctrl-label">Voice</span>
      <select id="voiceSelect" onchange="onVoiceChange()">
        <!--VOICE_OPTIONS-->
      </select>
    </div>

    <span class="status" id="status"></span>
  </div>
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
    syncApp();
  }

  // In the macOS app, the native toolbar mirrors the page's playback state.
  // The app shows progress as a bar, so it only gets status text that isn't
  // progress (errors and notices).
  function syncApp() {
    window.webkit?.messageHandlers?.reader?.postMessage({
      playing: isPlaying,
      canPlay: !document.getElementById('playBtn').disabled,
      speed: speed(),
      progress,
      message: statusIsProgress ? '' : document.getElementById('status').textContent,
    });
  }

  // Fraction of the text read so far, 0 to 1, advancing within each sentence.
  let progress = 0;
  let statusIsProgress = true;

  function setProgress(value) {
    progress = value;
    syncApp();
  }

  function onVoiceChange() { savePrefs(); invalidateCache(); }

  // Remember voice and speed across windows. Storage can be unavailable
  // (private mode, blocked site data), in which case the defaults stay.
  function savePrefs() {
    if (EMBED) return;
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
    if (prefs.speed >= +slider.min && prefs.speed <= +slider.max) setSpeed(prefs.speed);
  }

  function setSpeed(value) {
    document.getElementById('speedSlider').value = value;
    document.getElementById('speedVal').textContent = speed().toFixed(1) + '×';
    syncApp();
  }

  // Embedded in the macOS app: the app pushes text in, and its settings own
  // the voice and default speed, so nothing is remembered in the page.
  const EMBED = new URLSearchParams(location.search).has('embed');
  // Kept so readerClear can put it back after sentences replace it.
  const placeholder = document.getElementById('placeholder');
  if (EMBED) {
    document.body.classList.add('embed');
    placeholder.textContent =
      'Select text in any app, right-click and choose TTS, press ⌘V to read the clipboard, or drop a .md or .txt file here.';
  } else {
    restorePrefs();
  }

  // Called by the macOS app with new text and the settings to read it with.
  // The speed slider starts at the default and can be changed for this text.
  // The app's settings from the last readerLoad, reused for files dropped on the page.
  let appSettings = {};

  async function readerLoad(text, settings = {}, markdown = false) {
    appSettings = settings;
    if (settings.voice) document.getElementById('voiceSelect').value = settings.voice;
    if (settings.speed) setSpeed(settings.speed);
    if (markdown) text = await cleanMarkdown(text);
    await loadSentences(text);
    if (sentences.length) playFrom(0);
  }

  async function cleanMarkdown(text) {
    const res = await fetch('/api/process', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text })
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return (await res.json()).text;
  }

  // Drop a .md or .txt file anywhere on the page to read it. Without
  // preventDefault the browser would navigate to the file instead.
  const hasFiles = e => [...e.dataTransfer.types].includes('Files');
  document.addEventListener('dragover', e => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
    document.body.classList.add('dragging');
  });
  document.addEventListener('dragleave', e => {
    if (!e.relatedTarget) document.body.classList.remove('dragging');
  });
  document.addEventListener('drop', async e => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    document.body.classList.remove('dragging');
    const file = [...e.dataTransfer.files].find(f => /\.(md|markdown|txt)$/i.test(f.name));
    if (!file) { setStatus('Only .md and .txt files can be read'); return; }
    try {
      const markdown = /\.(md|markdown)$/i.test(file.name);
      if (EMBED) {
        await readerLoad(await file.text(), appSettings, markdown);
      } else {
        const text = markdown ? await cleanMarkdown(await file.text()) : await file.text();
        document.getElementById('textInput').value = text;
        await loadSentences(text);
      }
    } catch (err) {
      setStatus(`Could not read ${file.name}`);
      console.error(err);
    }
  });

  // Called by the macOS app's toolbar speed slider.
  function readerSetSpeed(v) {
    setSpeed(v);
    invalidateCache();
  }

  // Called by the macOS app when the voice changes in its settings.
  function readerSetVoice(v) {
    document.getElementById('voiceSelect').value = v;
    invalidateCache();
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
      const cleaned = await cleanMarkdown(text);
      document.getElementById('textInput').value = cleaned;
      await loadSentences(cleaned);
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
    setStatus(`${sentences.length} sentence${sentences.length !== 1 ? 's' : ''}`, true);
    setProgress(0);
  }

  function renderSentences() {
    const el = document.getElementById('readerContent');
    el.innerHTML = '';
    // Pause markers separate lines of the source text; each run between them is a paragraph.
    let para = null;
    sentences.forEach((s, i) => {
      if (s === '') { para = null; return; }
      if (!para) para = el.appendChild(document.createElement('p'));
      else para.append(' ');
      const span = document.createElement('span');
      span.className = 'sentence';
      span.dataset.index = i;
      span.textContent = s;
      span.addEventListener('click', () => jumpTo(i));
      para.appendChild(span);
    });
  }

  function highlight(index) {
    document.querySelectorAll('.sentence').forEach(el => {
      const i = parseInt(el.dataset.index);
      el.classList.toggle('active', i === index);
      el.classList.toggle('played', i < index);
    });
    const active = document.querySelector(`.sentence[data-index="${index}"]`);
    if (active) keepInView(active);
  }

  // Scroll once the sentence nears the top or bottom of the visible area
  // (above the controls bar), bringing it back to about a third of the way down.
  function keepInView(el) {
    const r = el.getBoundingClientRect();
    const visible = window.innerHeight - document.querySelector('.controls').offsetHeight;
    const margin = visible * 0.2;
    if (r.top < margin || r.bottom > visible - margin) {
      window.scrollBy({ top: r.top - visible * 0.3, behavior: 'smooth' });
    }
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
    // Paused audio has the old voice/speed (or text); resume restarts the sentence instead.
    discardPaused();
  }

  // The audio that was playing when paused, so play continues mid-sentence.
  let paused = null;  // { audio, index }

  function discardPaused() {
    if (paused) URL.revokeObjectURL(paused.audio.src);
    paused = null;
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
    setStatus(`${index + 1} / ${sentences.length}`, true);
    setProgress(index / sentences.length);
    audio.ontimeupdate = () => {
      if (gen === playGen && audio.duration) {
        setProgress((index + audio.currentTime / audio.duration) / sentences.length);
      }
    };
    const next = () => {
      URL.revokeObjectURL(audio.src);
      if (currentAudio === audio) currentAudio = null;
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
      discardPaused();
      clearHighlights();
      currentIndex = 0;
      setStatus('Done', true);
      setProgress(1);
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
    const resume = paused;
    paused = null;
    if (resume && resume.index === index) {
      // play() continues from where pause() stopped the element.
      currentIndex = index;
      setupAndPlay(resume.audio, index, playGen);
      prefetchAhead(index + 1, playGen);
      return;
    }
    if (resume) URL.revokeObjectURL(resume.audio.src);
    playIndex(index);
  }

  function pause() {
    playGen++;
    isPlaying = false;
    setIcon(false);
    if (currentAudio) {
      currentAudio.pause();
      paused = { audio: currentAudio, index: currentIndex };
      currentAudio = null;
    }
  }

  // Called by the macOS app (Edit > Clear).
  function readerClear() {
    pause();
    invalidateCache();
    sessionId = null;
    sentences = [];
    currentIndex = 0;
    document.getElementById('readerContent').replaceChildren(placeholder);
    document.getElementById('playBtn').disabled = true;
    setStatus('', true);
    setProgress(0);
  }

  function togglePlay() {
    if (!sessionId || !sentences.length) return;
    isPlaying ? pause() : playFrom(currentIndex);
  }

  function jumpTo(index) {
    const wasPlaying = isPlaying;
    if (isPlaying) pause();
    // Clicking a sentence reads it from its start, even the paused one.
    discardPaused();
    currentIndex = index;
    highlight(index);
    setProgress(index / sentences.length);
    if (wasPlaying) playFrom(index);
  }

  function setIcon(playing) {
    document.getElementById('iconPlay').style.display = playing ? 'none' : '';
    document.getElementById('iconPause').style.display = playing ? '' : 'none';
    syncApp();
  }

  function setStatus(msg, isProgress = false) {
    statusIsProgress = isProgress;
    document.getElementById('status').textContent = msg;
    syncApp();
  }

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
