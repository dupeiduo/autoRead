"""EPUB 解析：目录、章节顺序、以及**按段落**抽取正文。

这里做对两件事，直接决定音频听感：

1. **段落 = 朗读单位，只有「段落换行」才留停顿**。EPUB 的正文经常把一段话硬换行
   成好几行（Sigil/Calibre 转换后尤甚），作者也可能用 ``<br>`` 主动折行。这些都是
   **排版换行**，读的时候不该停顿 —— 按它们朗读会让一句话一顿，也就是用户抱怨的
   「换行卡顿」。所以这里用 ``<p>`` / ``<li>`` / ``<blockquote>`` 这类**块级元素**
   界定段落，块内的换行与空白一律归并（见 ``normalize_cjk``）；停顿只留给
   **段落换行**（由 ``textsplit``/``prosody`` 施加）。
2. **章节顺序以 spine 为准，标题以 nav/NCX 为准**。顺序错了，听书就成了
   「跳着讲」；标题错了，文件名与列表都不可信。

纯标准库实现（``zipfile`` + ``html.parser`` + ``xml.etree``），不依赖 lxml/bs4。
"""

from __future__ import annotations

import posixpath
import re
import urllib.parse
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

from .errors import BookError
from .utils import normalize_cjk, normalize_title

#: 块级元素：这些元素界定「一个朗读段落」。
_PARAGRAPH_TAGS = {"p", "li", "blockquote", "dd", "dt", "td", "th", "pre", "figcaption"}
#: 标题元素：可作为章节标题，且默认不算正文。
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
#: 其它块级容器：用来兜住「没有 <p> 包裹的裸文本」。
_CONTAINER_TAGS = {
    "div", "section", "article", "aside", "header", "footer", "main",
    "body", "ul", "ol", "table", "tr", "figure", "nav", "center",
}
#: 这些标签里的文字不是正文（样式、脚本、书名页的 <title>）。
_IGNORED_TAGS = {"script", "style", "head", "title", "svg", "math"}

#: 这些标题不是「章节」，是书的骨架信息，朗读没有意义。
_SKIP_TITLE_KEYWORDS = (
    "版权", "目录", "封面", "扉页", "书名页", "插图", "彩页", "出版信息",
    "copyright", "cover", "titlepage", "title page", "table of contents", "toc",
)
#: href 命中这些片段也当成非正文（有些书 nav 里没有封面项）。
_SKIP_HREF_KEYWORDS = ("cover", "copyright", "titlepage", "toc")

_SQUASH_WS_RE = re.compile(r"\s+")
_XML_NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "ncx": "http://www.daisy.org/z3986/2005/ncx/",
    "xhtml": "http://www.w3.org/1999/xhtml",
    "epub": "http://www.idpf.org/2007/ops",
}


@dataclass
class Block:
    """正文里的一个块：``kind`` 为 ``paragraph`` 或 ``heading``。"""

    kind: str
    text: str


@dataclass
class Chapter:
    """一个可生成的章节（对应 EPUB spine 里的一个文档）。"""

    index: int  # 从 1 开始的全局章节号
    title: str
    href: str  # EPUB 内部路径（已规范化）
    volume: str = ""
    blocks: list[Block] = field(default_factory=list)

    @property
    def paragraphs(self) -> list[str]:
        """只取正文段落（不含标题），已做过空白归一（块内换行已并入）。"""
        return [block.text for block in self.blocks if block.kind == "paragraph" and block.text]

    @property
    def char_count(self) -> int:
        return sum(len(text) for text in self.paragraphs)


@dataclass
class Book:
    path: Path
    title: str
    author: str
    chapters: list[Chapter]

    def __len__(self) -> int:
        return len(self.chapters)


# ==========================================================================
# HTML → 块
# ==========================================================================
@dataclass
class _Buffer:
    kind: str
    order: int
    parts: list[str] = field(default_factory=list)

    def text(self) -> str:
        return normalize_cjk("".join(self.parts))


class _BlockExtractor(HTMLParser):
    """按块级元素抽取文本；同一段里的换行/空白会自然合并。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self._buffers: list[_Buffer] = []
        self._ignore_depth = 0
        self._counter = 0

    # -- HTMLParser 回调 ------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _IGNORED_TAGS:
            self._ignore_depth += 1
            return
        if self._ignore_depth:
            return
        if tag == "br":
            # 排版换行：只当空白，绝不插停顿 —— 停顿只留给「段落换行」。
            if self._buffers:
                self._buffers[-1].parts.append(" ")
            return
        if tag in _PARAGRAPH_TAGS:
            self._push("paragraph", tag)
        elif tag in _HEADING_TAGS:
            self._push("heading", tag)
        elif tag in _CONTAINER_TAGS:
            self._push("paragraph", tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # 自闭合的 <br/> 走这里
        if tag.lower() == "br" and not self._ignore_depth and self._buffers:
            self._buffers[-1].parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _IGNORED_TAGS:
            self._ignore_depth = max(0, self._ignore_depth - 1)
            return
        if self._ignore_depth:
            return
        if tag in _PARAGRAPH_TAGS or tag in _HEADING_TAGS or tag in _CONTAINER_TAGS:
            self._pop()

    def handle_data(self, data: str) -> None:
        if self._ignore_depth or not data or data.isspace():
            return
        if not self._buffers:
            # 裸文本（没有块级祖先）：造一个容器兜住它。
            self._push("paragraph", "body")
        self._buffers[-1].parts.append(data)

    # -- 内部 ----------------------------------------------------------
    def _push(self, kind: str, tag: str) -> None:
        self._counter += 1
        self._buffers.append(_Buffer(kind=kind, order=self._counter))

    def _pop(self) -> None:
        if not self._buffers:
            return
        buffer = self._buffers.pop()
        text = buffer.text()
        if len(text) < 1:
            return
        # 纯标点/纯符号（分隔符常见）不算正文。
        if not any(char.isalnum() or "\u4e00" <= char <= "\u9fff" for char in text):
            return
        self.blocks.append(Block(kind=buffer.kind, text=text))

    def close(self) -> None:  # noqa: D102 - 继承自 HTMLParser
        super().close()
        while self._buffers:
            self._pop()


def extract_blocks(html_text: str) -> list[Block]:
    """把一段 XHTML 抽成有序的块列表（标题 + 正文段落）。"""
    parser = _BlockExtractor()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:  # noqa: BLE001 - HTML 再脏也不能让整本书打不开
        pass
    return parser.blocks


# ==========================================================================
# EPUB 结构
# ==========================================================================
def _squash(text: str) -> str:
    return _SQUASH_WS_RE.sub("", str(text or ""))


def _strip_duplicate_heading(blocks: list[Block], title: str) -> list[Block]:
    """丢掉正文开头与章节标题重复的标题块。

    很多 EPUB 会在正文里再排一次「第一章 / 惊蛰」，与 nav 里的标题重复。
    读出来就是「第一章 惊蛰。第一章。惊蛰。」这种滑稽效果。
    只在**开头**的连续标题块整体能对上标题时才丢，避免误删正文小标题。
    """
    if not blocks or not title:
        return blocks
    head_count = 0
    while head_count < len(blocks) and blocks[head_count].kind == "heading":
        head_count += 1
    if not head_count:
        return blocks
    combined = _squash("".join(block.text for block in blocks[:head_count]))
    norm_title = _squash(title)
    if combined and (combined == norm_title or combined in norm_title or norm_title in combined):
        return blocks[head_count:]
    return blocks


class _Manifest:
    def __init__(self, opf_text: str, opf_dir: str) -> None:
        self.opf_dir = opf_dir
        self.items: dict[str, tuple[str, str, str]] = {}  # id -> (href, media_type, abs_path)
        self.spine: list[str] = []
        self.title = ""
        self.author = ""
        self._parse(opf_text)

    def _parse(self, opf_text: str) -> None:
        try:
            root = ElementTree.fromstring(opf_text)
        except ElementTree.ParseError as exc:
            raise BookError(f"OPF 解析失败: {exc}") from exc

        title_node = root.find(".//dc:title", _XML_NS)
        creator_node = root.find(".//dc:creator", _XML_NS)
        self.title = (title_node.text or "").strip() if title_node is not None else ""
        self.author = (creator_node.text or "").strip() if creator_node is not None else ""

        items = root.findall(".//opf:manifest/opf:item", _XML_NS)
        itemrefs = root.findall(".//opf:spine/opf:itemref", _XML_NS)
        if not items:
            # 少数转换工具生成的 OPF 不带命名空间；按裸标签再找一次，
            # 否则整本书会被判成「没有解析到任何章节」。
            items = root.findall(".//manifest/item")
            itemrefs = root.findall(".//spine/itemref")
            if not self.title:
                node = root.find(".//title")
                self.title = (node.text or "").strip() if node is not None else ""
            if not self.author:
                node = root.find(".//creator")
                self.author = (node.text or "").strip() if node is not None else ""

        for item in items:
            item_id = item.get("id") or ""
            href = item.get("href") or ""
            media_type = item.get("media-type") or ""
            if item_id and href:
                self.items[item_id] = (href, media_type, self._abs(href))

        for itemref in itemrefs:
            if (itemref.get("linear") or "").lower() == "no":
                continue
            idref = itemref.get("idref") or ""
            if idref:
                self.spine.append(idref)

    def _abs(self, href: str) -> str:
        href = urllib.parse.unquote(href.split("#", 1)[0]).lstrip("/")
        if self.opf_dir:
            return posixpath.normpath(posixpath.join(self.opf_dir, href))
        return posixpath.normpath(href)

    def abs_path(self, href: str, base_dir: str) -> str:
        href = urllib.parse.unquote(href.split("#", 1)[0]).lstrip("/")
        return posixpath.normpath(posixpath.join(base_dir, href)) if base_dir else posixpath.normpath(href)


def _nav_title_map(zip_file: zipfile.ZipFile, manifest: _Manifest) -> tuple[dict[str, str], list[str]]:
    """返回 ``href → 标题`` 以及**按目录顺序**排列的条目。

    同时负责识别「册/卷」分隔：目录里 ``剑来1：少年起微末`` 这类封面项的
    文档没有正文，我们把它记成当前卷名，其后的章节都归属这一卷。
    """
    titles: dict[str, str] = {}
    ordered: list[str] = []

    # ---- EPUB3 nav.xhtml ----
    nav_item = None
    for item_id, (href, media_type, abs_path) in manifest.items.items():
        if "nav" in href.lower() and media_type == "application/xhtml+xml":
            nav_item = abs_path
            break
    if nav_item:
        try:
            html_text = zip_file.read(nav_item).decode("utf-8", errors="replace")
        except KeyError:
            html_text = ""
        if html_text:
            base_dir = posixpath.dirname(nav_item)
            for href, label in _parse_nav_links(html_text):
                abs_path = manifest.abs_path(href, base_dir)
                titles[abs_path] = label
                ordered.append(abs_path)

    # ---- EPUB2 toc.ncx（nav 缺失时兜底）----
    if not titles:
        for item_id, (href, media_type, abs_path) in manifest.items.items():
            if media_type == "application/x-dtbncx+xml":
                try:
                    ncx_text = zip_file.read(abs_path).decode("utf-8", errors="replace")
                except KeyError:
                    continue
                base_dir = posixpath.dirname(abs_path)
                for href, label in _parse_ncx(ncx_text):
                    resolved = manifest.abs_path(href, base_dir)
                    titles.setdefault(resolved, label)
                    ordered.append(resolved)
                break
    return titles, ordered


def _parse_nav_links(html_text: str) -> list[tuple[str, str]]:
    """解析 EPUB3 ``<nav epub:type="toc">`` 里的 ``<a href>``（按文档顺序）。"""

    class _NavParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.links: list[tuple[str, str]] = []
            self._href: str | None = None
            self._parts: list[str] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag.lower() == "a":
                self._href = dict(attrs).get("href")
                self._parts = []

        def handle_data(self, data: str) -> None:
            if self._href is not None:
                self._parts.append(data)

        def handle_endtag(self, tag: str) -> None:
            if tag.lower() == "a" and self._href is not None:
                label = normalize_title("".join(self._parts))
                if label:
                    self.links.append((self._href, label))
                self._href = None
                self._parts = []

    parser = _NavParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:  # noqa: BLE001
        pass
    return parser.links


def _parse_ncx(ncx_text: str) -> list[tuple[str, str]]:
    try:
        root = ElementTree.fromstring(ncx_text)
    except ElementTree.ParseError:
        return []
    links: list[tuple[str, str]] = []
    for nav_point in root.findall(".//ncx:navPoint", _XML_NS):
        label_node = nav_point.find("ncx:navLabel/ncx:text", _XML_NS)
        content_node = nav_point.find("ncx:content", _XML_NS)
        if label_node is None or content_node is None:
            continue
        label = normalize_title(label_node.text or "")
        src = content_node.get("src") or ""
        if label and src:
            links.append((src, label))
    return links


def _looks_like_skip(title: str, href: str) -> bool:
    lowered_title = (title or "").lower()
    lowered_href = (href or "").lower()
    if any(keyword in lowered_title for keyword in _SKIP_TITLE_KEYWORDS):
        return True
    stem = posixpath.basename(lowered_href)
    return any(keyword in stem for keyword in _SKIP_HREF_KEYWORDS)


def load_book(path: str | Path, *, strip_duplicate_heading: bool = True) -> Book:
    """解析 EPUB，返回按阅读顺序排列的章节列表。"""
    book_path = Path(path).expanduser()
    if not book_path.is_file():
        raise BookError(f"找不到电子书: {book_path}")
    try:
        zip_file = zipfile.ZipFile(book_path)
    except zipfile.BadZipFile as exc:
        raise BookError(f"不是合法的 EPUB（zip 打不开）: {book_path}") from exc

    with zip_file:
        opf_path = _find_opf(zip_file, book_path)
        opf_dir = posixpath.dirname(opf_path)
        try:
            opf_text = zip_file.read(opf_path).decode("utf-8", errors="replace")
        except KeyError as exc:
            raise BookError(f"EPUB 缺少 OPF: {opf_path}") from exc
        manifest = _Manifest(opf_text, opf_dir)
        titles, ordered_hrefs = _nav_title_map(zip_file, manifest)

        # 目录顺序里没有的正文文档，按 spine 顺序补在后面（spine 是权威阅读顺序）。
        chapter_order: list[str] = []
        for href in ordered_hrefs:
            if href not in chapter_order:
                chapter_order.append(href)
        spine_paths: list[str] = []
        for idref in manifest.spine:
            entry = manifest.items.get(idref)
            if not entry:
                continue
            abs_path = entry[2]
            spine_paths.append(abs_path)
            if abs_path not in chapter_order:
                chapter_order.append(abs_path)

        # 以 spine 顺序为主：只保留出现在 spine 里的文档，避免 nav 里的锚点噪声。
        spine_set = set(spine_paths)
        if spine_set:
            chapter_order = [href for href in chapter_order if href in spine_set]

        chapters: list[Chapter] = []
        volume = ""
        for href in chapter_order:
            raw = _read_text(zip_file, href)
            if raw is None:
                continue
            blocks = extract_blocks(raw)
            body_blocks = [block for block in blocks if block.kind == "paragraph" and block.text]
            is_coverish = _looks_like_skip(titles.get(href, ""), href)

            if not body_blocks:
                # 没有正文 → 若是目录里的一项，多半是「册」的封面，用它做卷名。
                if href in titles and "cover" in href.lower():
                    volume = titles[href]
                continue
            if is_coverish:
                continue

            title = titles.get(href, "")
            if not title:
                heading = next((b.text for b in blocks if b.kind == "heading" and b.text), "")
                title = heading or f"第{len(chapters) + 1}章"
            if strip_duplicate_heading:
                blocks = _strip_duplicate_heading(blocks, title)
            chapters.append(
                Chapter(
                    index=len(chapters) + 1,
                    title=title,
                    href=href,
                    volume=volume,
                    blocks=blocks,
                )
            )

    if not chapters:
        raise BookError(f"没有解析到任何章节（可能是不含正文的 EPUB）: {book_path}")

    return Book(
        path=book_path,
        title=manifest.title or book_path.stem,
        author=manifest.author,
        chapters=chapters,
    )


def _find_opf(zip_file: zipfile.ZipFile, book_path: Path) -> str:
    try:
        container = zip_file.read("META-INF/container.xml").decode("utf-8", errors="replace")
    except KeyError as exc:
        raise BookError(f"EPUB 缺少 META-INF/container.xml: {book_path}") from exc
    try:
        root = ElementTree.fromstring(container)
    except ElementTree.ParseError as exc:
        raise BookError(f"container.xml 解析失败: {exc}") from exc
    rootfile = root.find(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile")
    if rootfile is None:
        rootfile = root.find(".//rootfile")
    full_path = (rootfile.get("full-path") if rootfile is not None else "") or ""
    if not full_path:
        raise BookError(f"container.xml 未指定 rootfile: {book_path}")
    return posixpath.normpath(urllib.parse.unquote(full_path).lstrip("/"))


def _read_text(zip_file: zipfile.ZipFile, href: str) -> str | None:
    try:
        return zip_file.read(href).decode("utf-8", errors="replace")
    except KeyError:
        return None
