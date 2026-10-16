import unittest

from server import strip_markdown


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


if __name__ == "__main__":
    unittest.main()
