import unittest

import numpy as np
from fastapi.testclient import TestClient

from server import app, split_sentences, strip_markdown, to_pcm16


def make_client(**kwargs) -> TestClient:
    # The server only answers requests addressed to 127.0.0.1 or localhost.
    return TestClient(app, base_url="http://127.0.0.1", **kwargs)


class StripMarkdownTests(unittest.TestCase):
    def test_inline_markup_does_not_add_line_breaks(self):
        markdown = "This is **bold** and [a link](https://example.com)."

        self.assertEqual(strip_markdown(markdown), "This is bold and a link.")

    def test_soft_wrapped_paragraph_is_joined(self):
        markdown = "A paragraph can be\nwrapped in the source\nwithout hard breaks."

        self.assertEqual(
            strip_markdown(markdown),
            "A paragraph can be wrapped in the source without hard breaks.",
        )

    def test_semantic_breaks_are_preserved(self):
        markdown = "First paragraph.\n\nSecond line  \nwith a hard break."

        self.assertEqual(
            strip_markdown(markdown),
            "First paragraph.\n\nSecond line\nwith a hard break.",
        )

    def test_block_content_keeps_useful_boundaries(self):
        markdown = (
            "- first **item**\n"
            "- second item\n\n"
            "| A | B |\n"
            "|---|---|\n"
            "| one | two |\n\n"
            "```text\n"
            "code line one\n"
            "code line two\n"
            "```"
        )

        self.assertEqual(
            strip_markdown(markdown),
            "first item\n\nsecond item\n\nA | B\n\none | two\n\n"
            "code line one\ncode line two",
        )

    def test_pandoc_attributes_are_removed(self):
        self.assertEqual(strip_markdown("# Intro {#intro .big}"), "Intro")

    def test_braces_in_prose_are_kept(self):
        self.assertEqual(
            strip_markdown("Use the {name} placeholder."),
            "Use the {name} placeholder.",
        )


class SplitSentencesTests(unittest.TestCase):
    def test_title_abbreviations_do_not_end_a_sentence(self):
        self.assertEqual(
            split_sentences("Mr. Smith met Dr. Jones. They talked."),
            ["Mr. Smith met Dr. Jones.", "They talked."],
        )

    def test_sentence_ending_in_closing_quote_is_split(self):
        self.assertEqual(
            split_sentences('He said "Stop." Then he left.'),
            ['He said "Stop."', "Then he left."],
        )


class AudioTests(unittest.TestCase):
    def test_overshooting_samples_saturate_instead_of_wrapping(self):
        pcm = np.frombuffer(to_pcm16(np.array([1.02, -1.02])), dtype=np.int16)

        self.assertEqual(pcm.tolist(), [32767, -32767])

    def test_out_of_range_audio_params_are_rejected(self):
        client = make_client()
        for params in ({"speed": 0}, {"voice": "/tmp/evil.pt"}):
            with self.subTest(params=params):
                res = client.get(
                    "/api/audio", params={"session_id": "x", "index": 0, **params}
                )
                self.assertEqual(res.status_code, 422)


class UploadTests(unittest.TestCase):
    def test_non_utf8_upload_is_a_client_error(self):
        res = make_client().post(
            "/api/upload", files={"file": ("a.txt", "caf\xe9".encode("cp1252"))}
        )

        self.assertEqual(res.status_code, 400)

    def test_utf8_bom_is_not_part_of_the_text(self):
        res = make_client().post(
            "/api/upload", files={"file": ("a.md", "\ufeffHello".encode("utf-8"))}
        )

        self.assertEqual(res.json()["text"], "Hello")


class AccessTests(unittest.TestCase):
    def setUp(self):
        app.state.token = "secret"
        self.addCleanup(setattr, app.state, "token", None)

    def test_requests_without_the_app_token_are_rejected(self):
        for cookies in ({}, {"aloud_token": "guess"}):
            with self.subTest(cookies=cookies):
                res = make_client(cookies=cookies).get("/api/voices")
                self.assertEqual(res.status_code, 403)

    def test_requests_with_the_app_token_are_served(self):
        res = make_client(cookies={"aloud_token": "secret"}).get("/api/voices")

        self.assertEqual(res.status_code, 200)

    def test_requests_for_another_host_are_rejected(self):
        # A page that rebinds its own domain to 127.0.0.1 still sends that domain.
        res = TestClient(app, base_url="http://attacker.example", cookies={"aloud_token": "secret"}).get("/")

        self.assertEqual(res.status_code, 400)


if __name__ == "__main__":
    unittest.main()
