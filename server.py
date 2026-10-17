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
<title>Aloud</title>
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
  <header><h1>Aloud</h1></header>

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

  const voice = () => document.getElementById('voiceSelect').value;
  const speed = () => parseFloat(document.getElementById('speedSlider').value);
  const cacheKey = (i, s = speed()) => `${sessionId}:${i}:${voice()}:${s}`;

  function onSpeedChange() {
    document.getElementById('speedVal').textContent = speed().toFixed(1) + '×';
    savePrefs();
    speedChanged();
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

  function onVoiceChange() { savePrefs(); voiceChanged(); }

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
      'Select text in any app, right-click and choose Read in Aloud, press ⌘V to read the clipboard, or drop a .md or .txt file here.';
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
    speedChanged();
  }

  // Called by the macOS app when the voice changes in its settings.
  function readerSetVoice(v) {
    document.getElementById('voiceSelect').value = v;
    voiceChanged();
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
    paused = null;
    clearBuffers();
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

  // ---- Playback: one continuous Web Audio stream ----
  //
  // Every sentence is synthesized as its own clip, but they all play through a
  // single AudioContext, scheduled back to back. The output never restarts
  // between sentences, so nothing is lost while a new player warms up, and
  // there are no gaps. Highlight and progress follow the stream's clock.

  let ctx = null;
  let buffers = new Map();  // cacheKey -> { index, speed, promise: Promise<AudioBuffer> }
  let scheduled = [];       // clips queued in the stream, in play order
  let nextIndex = 0;        // next sentence to queue
  let scheduledEnd = 0;     // stream time when the last queued clip ends
  let paused = null;        // { index, frac }: where pause() stopped, as a fraction of the sentence
  // Bumped whenever the queue is replanned (pause, seek, speed or voice
  // change), so async work from an earlier plan sees it is stale.
  let playGen = 0;
  let ticker = null;
  let idleTimer = null;
  // True while playFrom waits for its first clip. The queue state still
  // describes the old plan then, so nothing may be queued from it.
  let starting = false;
  const LOOKAHEAD = 4;       // sentences synthesized ahead of the one playing
  const QUEUE_SECONDS = 4;   // audio queued in the stream ahead of the playhead
  const FADE = 0.03;         // seconds, to avoid clicks when cutting a clip

  function audioContext() {
    ctx ||= new (window.AudioContext || window.webkitAudioContext)();
    return ctx;
  }

  // Stream time the listener is hearing now.
  const playhead = () => ctx.currentTime - (ctx.outputLatency || ctx.baseLatency || 0);
  const clipEnd = clip => clip.startAt + clip.duration - clip.offset;

  function clearBuffers(keep = () => false) {
    for (const [key, entry] of buffers) if (!keep(entry)) buffers.delete(key);
  }

  // A clip the server or decoder rejected is skipped; a network failure stops playback.
  class SkippableError extends Error {}

  function getBuffer(index, synthSpeed = speed()) {
    const key = cacheKey(index, synthSpeed);
    if (!buffers.has(key)) {
      const cache = buffers;
      const promise = fetch(`/api/audio?session_id=${sessionId}&index=${index}&voice=${voice()}&speed=${synthSpeed}`)
        .then(r => {
          if (!r.ok) throw new SkippableError(`HTTP ${r.status}`);
          return r.arrayBuffer();
        })
        .then(data => audioContext().decodeAudioData(data).catch(err => { throw new SkippableError(err.message); }));
      cache.set(key, { index, speed: synthSpeed, promise });
      // A failed fetch must not stay cached, or retrying would fail forever.
      promise.catch(() => { if (cache.get(key)?.promise === promise) cache.delete(key); });
    }
    return buffers.get(key).promise;
  }

  // The server synthesizes one sentence at a time, so fetch sequentially:
  // the sentence needed now never queues behind ones needed later.
  async function prefetchAhead(index, gen) {
    const end = Math.min(index + LOOKAHEAD, sentences.length);
    for (let i = index; i < end && gen === playGen; i++) {
      try { await getBuffer(i); } catch { /* the queue reports or skips it */ }
    }
  }

  function schedule(index, buffer, synthSpeed, startAt, offset, fadeIn) {
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    const gain = ctx.createGain();
    source.connect(gain).connect(ctx.destination);
    if (fadeIn) {
      gain.gain.setValueAtTime(0, startAt);
      gain.gain.linearRampToValueAtTime(1, startAt + FADE);
    }
    // Clip boundaries also drive the queue: audio events keep firing in
    // background tabs where the ticker's timer is throttled.
    source.onended = () => { gain.disconnect(); if (isPlaying) tick(); };
    source.start(startAt, offset);
    scheduled.push({ index, speed: synthSpeed, source, gain, startAt, offset, duration: buffer.duration });
    nextIndex = index + 1;
    scheduledEnd = clipEnd(scheduled.at(-1));
  }

  // Stops queued clips except those `keep` accepts; with `at`, one already
  // playing fades out instead of clicking.
  function stopScheduled(at = null, keep = () => false) {
    const kept = [];
    for (const clip of scheduled) {
      if (keep(clip)) { kept.push(clip); continue; }
      if (at != null && clip.startAt < at) {
        clip.gain.gain.cancelScheduledValues(at);
        clip.gain.gain.setValueAtTime(clip.gain.gain.value, at);
        clip.gain.gain.linearRampToValueAtTime(0, at + FADE);
        clip.source.stop(at + FADE);
      } else {
        clip.source.stop();
      }
    }
    scheduled = kept;
  }

  // Drops everything queued after `clip` and queues afresh from the next
  // sentence, for changes that should apply from the next sentence on.
  function requeueAfter(clip) {
    stopScheduled(null, c => c.startAt <= clip.startAt);
    nextIndex = clip.index + 1;
    scheduledEnd = clipEnd(clip);
    pump(++playGen);
  }

  // The clip at stream time `t` (default: what is being heard) and the position in it.
  function current(t = ctx && playhead()) {
    if (!ctx) return null;
    const clip = scheduled.find(c => t < clipEnd(c));
    return clip ? { clip, pos: Math.max(clip.offset, clip.offset + t - clip.startAt) } : null;
  }

  // Keeps the stream fed: always the next sentence after the one playing,
  // and at least QUEUE_SECONDS of audio.
  let pumpingGen = -1;
  async function pump(gen) {
    if (starting || pumpingGen === gen) return;
    pumpingGen = gen;
    const needMore = () => {
      const upcoming = scheduled.filter(c => clipEnd(c) > ctx.currentTime).length;
      return upcoming < 2 || scheduledEnd - ctx.currentTime < QUEUE_SECONDS;
    };
    try {
      while (gen === playGen && nextIndex < sentences.length && needMore()) {
        const index = nextIndex;
        const synthSpeed = speed();
        let buffer;
        try {
          buffer = await getBuffer(index, synthSpeed);
        } catch (err) {
          if (gen !== playGen || index !== nextIndex) return;
          if (!(err instanceof SkippableError)) return playbackFailed(err);
          console.warn(`Skipping sentence ${index + 1}:`, err);
          nextIndex = index + 1;
          continue;
        }
        if (gen !== playGen || index !== nextIndex) return;
        // Speed changed while synthesizing: request it again at the new speed.
        if (synthSpeed !== speed()) continue;
        // If synthesis fell behind, the stream has a gap rather than an overlap.
        schedule(index, buffer, synthSpeed, Math.max(scheduledEnd, ctx.currentTime + 0.02), 0, false);
        prefetchAhead(nextIndex, gen);
      }
    } finally {
      if (pumpingGen === gen) pumpingGen = -1;
    }
  }

  let lastProgressAt = 0;
  function tick() {
    if (starting) return;
    const now = current();
    if (!now) {
      if (nextIndex >= sentences.length && scheduledEnd <= ctx.currentTime) finish();
      else pump(playGen);
      return;
    }
    const { clip, pos } = now;
    if (clip.index !== currentIndex) {
      currentIndex = clip.index;
      highlight(clip.index);
      setStatus(`${clip.index + 1} / ${sentences.length}`, true);
    }
    // The ticker runs every 50 ms for a responsive highlight; progress
    // (which also messages the app) only needs a few updates a second.
    if (performance.now() - lastProgressAt > 200) {
      lastProgressAt = performance.now();
      setProgress((clip.index + pos / clip.duration) / sentences.length);
    }
    scheduled = scheduled.filter(c => clipEnd(c) > ctx.currentTime - 1);
    // Played sentences won't be needed again unless clicked; drop their audio.
    clearBuffers(entry => entry.index >= currentIndex - 1);
    pump(playGen);
  }

  function finish() {
    pause();
    paused = null;
    clearHighlights();
    currentIndex = 0;
    setStatus('Done', true);
    setProgress(1);
  }

  function playbackFailed(err) {
    pause();
    setStatus('Audio failed, press play to retry');
    console.error(err);
  }

  async function playFrom(index) {
    const context = audioContext();
    const wasRunning = context.state === 'running';
    // Called synchronously so a click's user-gesture permission applies.
    if (!wasRunning) context.resume();
    clearTimeout(idleTimer);
    stopScheduled();
    const gen = ++playGen;
    starting = true;
    isPlaying = true;
    setIcon(true);
    // `paused` stays set until the clip is queued, so pausing again while
    // this one loads keeps the place.
    const resume = paused?.index === index ? paused : null;
    currentIndex = index;
    highlight(index);
    setStatus(`${index + 1} / ${sentences.length}`, true);
    let buffer, synthSpeed;
    do {
      synthSpeed = speed();
      try {
        buffer = await getBuffer(index, synthSpeed);
      } catch (err) {
        if (gen !== playGen) return;
        if (err instanceof SkippableError && index + 1 < sentences.length) {
          console.warn(`Skipping sentence ${index + 1}:`, err);
          paused = null;
          return playFrom(index + 1);
        }
        return playbackFailed(err);
      }
      if (gen !== playGen) return;
    } while (synthSpeed !== speed());  // speed changed while synthesizing
    if (context.state !== 'running') await context.resume();
    if (gen !== playGen) return;
    // A just-started output gets a moment before sound, and a resumed
    // sentence fades in rather than starting on a click.
    const startAt = context.currentTime + (wasRunning ? 0.05 : 0.15);
    const offset = resume ? Math.min(resume.frac, 0.999) * buffer.duration : 0;
    paused = null;
    scheduled = [];
    schedule(index, buffer, synthSpeed, startAt, offset, offset > 0);
    starting = false;
    clearInterval(ticker);
    ticker = setInterval(tick, 50);
    pump(gen);
    prefetchAhead(index + 1, gen);
  }

  function pause() {
    const now = isPlaying ? current() : null;
    if (now) paused = { index: now.clip.index, frac: now.pos / now.clip.duration };
    playGen++;
    starting = false;
    isPlaying = false;
    setIcon(false);
    clearInterval(ticker);
    if (ctx) {
      stopScheduled(ctx.currentTime);
      // Release the audio device after a while; resuming starts it again.
      clearTimeout(idleTimer);
      idleTimer = setTimeout(() => { if (!isPlaying) ctx.suspend(); }, 30000);
    }
  }

  // Speed applies to the sentence being read: it is re-synthesized at the new
  // speed and swapped in at the same relative position, so the voice keeps its
  // natural pitch. A slider drag sends many changes; swap once it settles.
  let speedTimer;
  let swapGen = 0;
  function speedChanged() {
    clearTimeout(speedTimer);
    speedTimer = setTimeout(swapSpeed, 300);
  }

  async function swapSpeed() {
    // Only the newest change matters; an earlier swap still synthesizing gives way.
    const mine = ++swapGen;
    const target = speed();
    clearBuffers(entry => entry.speed === target);
    // While paused, the stored fraction maps onto the new speed's audio on resume.
    // While the first clip is loading, playFrom requests it again at the new speed.
    while (isPlaying) {
      // Swap in render time, not heard time: the old clip's sample rendered
      // at `at` must line up with the new clip's sample rendered at `at`.
      const found = current(ctx.currentTime + 0.02);
      if (!found) return;
      const clip = found.clip;
      // Queued at the new speed already (e.g. while the last swap synthesized):
      // only clips after it may still be at an old speed.
      if (clip.speed === target) {
        if (scheduled.some(c => c.startAt > clip.startAt && c.speed !== target)) requeueAfter(clip);
        return;
      }
      const gen = playGen;
      let buffer;
      try { buffer = await getBuffer(clip.index, target); } catch { return; }
      if (mine !== swapGen || gen !== playGen) return;
      const at = ctx.currentTime + 0.02;
      // Moved on to the next sentence while synthesizing: check that one.
      if (current(at)?.clip !== clip) continue;
      const frac = (clip.offset + Math.max(0, at - clip.startAt)) / clip.duration;
      stopScheduled(at);
      const g = ++playGen;
      schedule(clip.index, buffer, target, at, Math.min(frac, 0.999) * buffer.duration, true);
      pump(g);
      prefetchAhead(clip.index + 1, g);
      return;
    }
  }

  // A sentence can't change voice midway: the new voice starts with the next one.
  function voiceChanged() {
    clearBuffers();
    if (paused) paused.frac = 0;
    if (!isPlaying) return;
    const now = current();
    if (now) requeueAfter(now.clip);
  }

  // Called by the macOS app (Edit > Clear).
  function readerClear() {
    pause();
    paused = null;
    clearBuffers();
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
    paused = null;
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
