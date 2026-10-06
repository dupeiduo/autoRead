"""EPUB 解析测试。

重点验证「不在换行/换页处卡顿」的根因修复：源码里被硬换行拆散的同一段话，
必须被还原成**一个**段落，而不是很多个短句。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401  (把项目根目录加入 sys.path)

from fixtures import build_epub

from autoread.epub import extract_blocks, load_book


class ExtractBlocksTest(unittest.TestCase):
    def test_soft_wrapped_lines_become_one_paragraph(self) -> None:
        # 一段话被硬换行成 4 行 —— 解析后必须是 1 个段落。
        html = "<html><body><p>第一行文字\n第二行文字\n第三行文字\n第四行文字</p></body></html>"
        blocks = extract_blocks(html)
        paragraphs = [block.text for block in blocks if block.kind == "paragraph"]
        self.assertEqual(len(paragraphs), 1)
        self.assertEqual(paragraphs[0], "第一行文字第二行文字第三行文字第四行文字")

    def test_br_is_not_a_sentence_break(self) -> None:
        # <br/> 只当空白：不能把一个段落切成两段（否则就是换行卡顿）。
        html = "<html><body><p>上句<br/>下句</p></body></html>"
        paragraphs = [b.text for b in extract_blocks(html) if b.kind == "paragraph"]
        self.assertEqual(len(paragraphs), 1)
        self.assertEqual(paragraphs[0], "上句下句")

    def test_nested_div_does_not_duplicate(self) -> None:
        html = "<html><body><div><p>正文一</p><p>正文二</p></div></body></html>"
        paragraphs = [b.text for b in extract_blocks(html) if b.kind == "paragraph"]
        self.assertEqual(paragraphs, ["正文一", "正文二"])

    def test_heading_and_paragraph_are_separate(self) -> None:
        html = "<html><body><h1>标题</h1><p>正文</p></body></html>"
        blocks = extract_blocks(html)
        self.assertEqual([(b.kind, b.text) for b in blocks], [("heading", "标题"), ("paragraph", "正文")])

    def test_script_and_style_are_ignored(self) -> None:
        html = "<html><body><style>p{color:red}</style><p>正文</p><script>var a=1;</script></body></html>"
        paragraphs = [b.text for b in extract_blocks(html) if b.kind == "paragraph"]
        self.assertEqual(paragraphs, ["正文"])

    def test_entities_and_fullwidth_spaces_normalized(self) -> None:
        html = "<html><body><p>你好&nbsp;&nbsp;世界（　测试　）</p></body></html>"
        paragraphs = [b.text for b in extract_blocks(html) if b.kind == "paragraph"]
        self.assertEqual(paragraphs, ["你好世界（测试）"])


class LoadBookTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_chapters_in_spine_order_with_nav_titles(self) -> None:
        path = build_epub(
            self.tmp / "book.epub",
            title="测试书",
            author="作者甲",
            chapters=[
                ("第一章 起风", ["风起了。", "雨也来了。"]),
                ("第二章 落雨", ["雨停了。"]),
            ],
        )
        book = load_book(path)
        self.assertEqual(book.title, "测试书")
        self.assertEqual(book.author, "作者甲")
        self.assertEqual([c.index for c in book.chapters], [1, 2])
        self.assertEqual([c.title for c in book.chapters], ["第一章 起风", "第二章 落雨"])
        # 标题保留「章号 + 空格 + 章名」的可读形式
        self.assertIn(" ", book.chapters[0].title)

    def test_cover_and_copyright_are_skipped(self) -> None:
        path = build_epub(self.tmp / "book.epub")
        book = load_book(path)
        self.assertEqual(len(book.chapters), 1)
        self.assertTrue(all("版权" not in c.title for c in book.chapters))

    def test_duplicate_body_heading_is_stripped(self) -> None:
        # fixtures 会在正文里再排一次标题（h2），解析后不该出现在段落里。
        path = build_epub(self.tmp / "book.epub", chapters=[("第一章 惊蛰", ["二月二，龙抬头。"])])
        book = load_book(path)
        self.assertEqual(book.chapters[0].paragraphs, ["二月二，龙抬头。"])

    def test_soft_wrapped_source_still_one_paragraph(self) -> None:
        path = build_epub(
            self.tmp / "book.epub",
            chapters=[("第一章 长段", ["暮色里，小镇名叫泥瓶巷的僻静地方，有个孤苦伶仃的清瘦少年，此时他正按照习俗驱赶蛇虫。"])],
            soft_wrap=True,
        )
        book = load_book(path)
        self.assertEqual(len(book.chapters[0].paragraphs), 1)

    def test_missing_file_raises(self) -> None:
        from autoread.errors import BookError

        with self.assertRaises(BookError):
            load_book(self.tmp / "nope.epub")

    def test_namespaceless_opf_still_parses(self) -> None:
        # 少数转换工具生成的 OPF 不带命名空间，不能因此整本书读不出来。
        import zipfile

        path = self.tmp / "plain.epub"
        opf = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<package version="2.0"><metadata>'
            "<title>裸标签书</title><creator>某人</creator></metadata>"
            '<manifest><item id="c1" href="Text/c1.xhtml" media-type="application/xhtml+xml"/></manifest>'
            '<spine><itemref idref="c1"/></spine></package>'
        )
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("mimetype", "application/epub+zip")
            zf.writestr(
                "META-INF/container.xml",
                '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                'media-type="application/oebps-package+xml"/></rootfiles></container>',
            )
            zf.writestr("OEBPS/content.opf", opf)
            zf.writestr("OEBPS/Text/c1.xhtml", "<html><body><p>正文一。</p><p>正文二。</p></body></html>")
        book = load_book(path)
        self.assertEqual(book.title, "裸标签书")
        self.assertEqual(book.author, "某人")
        self.assertEqual(book.chapters[0].paragraphs, ["正文一。", "正文二。"])

    def test_not_a_zip_raises(self) -> None:
        from autoread.errors import BookError

        bogus = self.tmp / "bogus.epub"
        bogus.write_text("not a zip", encoding="utf-8")
        with self.assertRaises(BookError):
            load_book(bogus)


if __name__ == "__main__":
    unittest.main()
