"""构造一个最小可用的 EPUB，供测试使用（不依赖真实电子书）。"""

from __future__ import annotations

import posixpath
import zipfile
from pathlib import Path

_CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""


def _chapter_html(title: str, paragraphs: list[str], *, soft_wrap: bool = False) -> str:
    body: list[str] = []
    for index, text in enumerate(paragraphs):
        if soft_wrap and index == 0:
            # 模拟「一段话在源码里被硬换行成好几行」——这是「换行卡顿」的根源。
            pieces = [text[i : i + 12] for i in range(0, len(text), 12)]
            body.append("<p>" + "\n      ".join(pieces) + "</p>")
        else:
            body.append(f"<p>{text}</p>")
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">\n'
        f"<head><title>{title}</title></head>\n<body>\n"
        f'<div class="header1"><h2>{title}</h2></div>\n'
        f'<div class="part">\n    ' + "\n    ".join(body) + "\n</div>\n"
        "</body>\n</html>\n"
    )


def build_epub(
    path: Path,
    *,
    title: str = "测试书",
    author: str = "测试作者",
    chapters: list[tuple[str, list[str]]] | None = None,
    include_copyright: bool = True,
    soft_wrap: bool = False,
) -> Path:
    """写出一个最小 EPUB3，返回其路径。"""
    chapters = chapters or [("第一章 起风", ["风起了。", "雨也来了。"])]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    manifest: list[str] = []
    spine: list[str] = []
    nav_items: list[str] = []

    # 封面（无正文，用来验证「被跳过 + 作为卷名」）
    manifest.append('<item id="cover" href="Text/cover.xhtml" media-type="application/xhtml+xml"/>')
    spine.append('<itemref idref="cover"/>')
    # 封面项指向封面文档：load_book 用它识别「册/卷」，并跳过这份无正文的文档。
    nav_items.append('<li><a href="cover.xhtml">%s</a></li>' % title)

    if include_copyright:
        manifest.append('<item id="copyright" href="Text/copyright.xhtml" media-type="application/xhtml+xml"/>')
        spine.append('<itemref idref="copyright"/>')
        nav_items.append('<li><a href="copyright.xhtml">版权信息</a></li>')

    for index, (chapter_title, paragraphs) in enumerate(chapters, start=1):
        item_id = f"ch{index}"
        href = f"Text/ch{index}.xhtml"
        manifest.append(f'<item id="{item_id}" href="{href}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="{item_id}"/>')
        # nav 里的链接是相对 nav.xhtml 自身所在目录（OEBPS/Text/）的
        nav_items.append(f'<li><a href="ch{index}.xhtml">{chapter_title}</a></li>')

    manifest.append('<item id="nav" href="Text/nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>')

    opf = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package version="3.0" unique-identifier="pub-id" xmlns="http://www.idpf.org/2007/opf">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f"    <dc:title>{title}</dc:title>\n"
        f"    <dc:creator>{author}</dc:creator>\n"
        "    <dc:language>zh</dc:language>\n"
        '    <dc:identifier id="pub-id">urn:uuid:00000000-0000-0000-0000-000000000000</dc:identifier>\n'
        "  </metadata>\n"
        "  <manifest>\n    " + "\n    ".join(manifest) + "\n  </manifest>\n"
        "  <spine>\n    " + "\n    ".join(spine) + "\n  </spine>\n"
        "</package>\n"
    )

    nav = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">\n'
        "<head><title>目录</title></head>\n<body>\n"
        '<nav epub:type="toc"><ol>\n    ' + "\n    ".join(nav_items) + "\n</ol></nav>\n"
        "</body>\n</html>\n"
    )

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        # mimetype 必须第一个且不压缩（EPUB 规范），测试里也照做。
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
        zf.writestr("META-INF/container.xml", _CONTAINER)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/Text/nav.xhtml", nav)
        zf.writestr("OEBPS/Text/cover.xhtml", "<html><body><img src='../Images/c.jpg'/></body></html>")
        if include_copyright:
            zf.writestr(
                "OEBPS/Text/copyright.xhtml",
                "<html><body><p>版权所有，侵权必究。</p></body></html>",
            )
        for index, (chapter_title, paragraphs) in enumerate(chapters, start=1):
            zf.writestr(
                f"OEBPS/Text/ch{index}.xhtml",
                _chapter_html(chapter_title, paragraphs, soft_wrap=soft_wrap),
            )
    return path


def opf_dir_href(href: str) -> str:
    return posixpath.normpath(href)
