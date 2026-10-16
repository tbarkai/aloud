import sys
import argparse
import sounddevice as sd
from kokoro import KPipeline


SAMPLE_RATE = 24000


def speak(text: str, voice: str = "af_heart", speed: float = 1.0):
    pipeline = KPipeline(lang_code="b", repo_id="hexgrad/Kokoro-82M")
    for result in pipeline(text, voice=voice, speed=speed):
        if result.audio is not None:
            audio = result.audio.numpy()
            sd.play(audio, samplerate=SAMPLE_RATE)
            sd.wait()


def main():
    parser = argparse.ArgumentParser(description="Read text aloud using Kokoro TTS")
    parser.add_argument("text", nargs="*", help="Text to speak (or pipe via stdin)")
    parser.add_argument("--voice", default="bf_emma", help="Voice to use (default: bf_emma)")
    parser.add_argument("--speed", type=float, default=1.0, help="Speech speed (default: 1.0)")
    args = parser.parse_args()

    if args.text:
        text = " ".join(args.text)
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        parser.print_help()
        sys.exit(1)

    speak(text, voice=args.voice, speed=args.speed)


if __name__ == "__main__":
    main()
