# Aloud

Aloud reads text to you with [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M), a small open text-to-speech model that runs locally. Nothing leaves your machine. It uses Kokoro's British English voices.

You get the text on screen as it's read. The current sentence is highlighted, and clicking any sentence jumps there. Speed changes apply to the sentence being read, without changing the voice's pitch.

There are three ways to use it.

- A macOS menu bar app. Select text in any app, right-click, and choose Read in Aloud.
- A web page served by `server.py`. Paste text or upload a Markdown file.
- A command-line script, `main.py`, that just speaks.

## Requirements

- An Apple Silicon Mac. The menu bar app needs macOS 14 or later.
- [uv](https://docs.astral.sh/uv/). It installs Python 3.13 and the dependencies for you.
- A Swift compiler for the app, from either Xcode or the Command Line Tools (`xcode-select --install`). If Xcode is installed, accept its license once with `sudo xcodebuild -license accept`, or the build fails.

The first time it speaks, Kokoro downloads its weights from Hugging Face, about 320 MB. After that it works offline.

## Install the macOS app

```bash
git clone https://github.com/tbarkai/aloud.git
```

```bash
cd aloud
```

```bash
mac/install.sh
```

The script sets up the Python environment, builds `Aloud.app`, installs it to `~/Applications`, registers the Read in Aloud service and launches the app. A waveform icon appears in the menu bar.

The app runs `server.py` from this checkout on a free local port. It doesn't carry its own copy. So run `mac/install.sh` again after you change any code here, and after you move the folder.

### Using it

- Select text, right-click, and choose Read in Aloud. In apps with several services it sits under Services. Most native apps and Safari support this. Many Electron apps, such as Slack and VS Code, don't.
- Click the menu bar icon, or press ⌃⌘S from any app, to open the reader. Right-click the icon for its menu.
- In the reader, ⌘V reads whatever is on the clipboard. You can also drop a `.md` or `.txt` file on the window or the Dock icon. Markdown is stripped to plain text first.
- Click a sentence to jump to it. Space plays and pauses. ⌘K clears the reader.
- The speed slider in the toolbar applies to the current text only. New text starts at the default speed.

Open Settings from the menu bar menu, or with ⌘, while a reader window is open. It has the voice, the default speed, the Show Reader shortcut and Start at Login.

To read a selection with a key instead of the right-click menu, open System Settings › Keyboard › Keyboard Shortcuts › Services › Text, find Read in Aloud, and give it a shortcut. Pick one with ⌘ that differs from the Show Reader shortcut.

The server log is at `~/Library/Logs/Aloud/server.log`.

### Troubleshooting

If Read in Aloud doesn't show up in the right-click menu, refresh the services list, then quit and reopen the app you're selecting text in.

```bash
/System/Library/CoreServices/pbs -update
```

If the build stops with a message about the Xcode license, accept it as shown under Requirements and run `mac/install.sh` again.

### Uninstall

Quit Aloud from its menu bar menu, then delete the app.

```bash
rm -rf ~/Applications/Aloud.app
```

## Run the web reader

```bash
uv sync
```

```bash
uv run python server.py
```

Then open http://127.0.0.1:7860. Paste text and click Process Text, or upload a `.md` file. Pass `--port` to use a different port.

## Use the command line

```bash
uv run python main.py "The train left the station a little after nine."
```

It also reads from standard input, and takes `--voice` (default `bf_emma`) and `--speed` (default 1.0).

```bash
pbpaste | uv run python main.py --speed 1.3
```

## Run the tests

```bash
uv run python -m unittest discover -s tests
```
