"""Render every text file in a project directory into one syntax-highlighted PDF.

The document opens with a title page (project name, version, company) and a
clickable table of contents. Layout of every other page:
    header  : project name + version (left) | file path relative to project (right)
    ------------------------------------------------------------------
    body    : line-numbered, syntax-colored source
    ------------------------------------------------------------------
    footer  : page_number/total_pages (centered)

Every file starts on a new page.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pathspec
from pygments.lexers import get_lexer_for_filename, guess_lexer_for_filename
from pygments.lexers.special import TextLexer
from pygments.styles import get_style_by_name
from pygments.util import ClassNotFound
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4, LETTER, landscape
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import simpleSplit
from reportlab.pdfgen import canvas

from project_meta import ProjectInfo, detect

DEFAULT_EXCLUDES = [
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".idea", ".vscode", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    "dist", "build", ".DS_Store", "*.pyc", "*.lock", "package-lock.json",
]

# (regular, bold, italic, bold-italic) — each entry is (path, subfont index)
FONT_CANDIDATES = [
    [("/System/Library/Fonts/Menlo.ttc", i) for i in range(4)],
    [(f"/usr/share/fonts/truetype/dejavu/DejaVuSansMono{s}.ttf", 0)
     for s in ("", "-Bold", "-Oblique", "-BoldOblique")],
    [(f"/System/Library/Fonts/Supplemental/Courier New{s}.ttf", 0)
     for s in ("", " Bold", " Italic", " Bold Italic")],
]
BUILTIN_FONTS = ("Courier", "Courier-Bold", "Courier-Oblique", "Courier-BoldOblique")

# (regular, bold) sans-serif for the title page, contents and headers
UI_FONT_CANDIDATES = [
    [("/System/Library/Fonts/Helvetica.ttc", 0), ("/System/Library/Fonts/Helvetica.ttc", 1)],
    [("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0),
     ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0)],
    [("/System/Library/Fonts/Supplemental/Arial.ttf", 0),
     ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 0)],
]
BUILTIN_UI_FONTS = ("Helvetica", "Helvetica-Bold")

HEADER_FOOTER_COLOR = HexColor("#444444")
RULE_COLOR = HexColor("#999999")
LINE_NO_COLOR = HexColor("#aaaaaa")
TITLE_COLOR = HexColor("#1a1a1a")
SUBTITLE_COLOR = HexColor("#555555")
ACCENT_COLOR = HexColor("#2b6cb0")
LEADER_COLOR = HexColor("#bbbbbb")


# --------------------------------------------------------------------------- #
# File discovery
# --------------------------------------------------------------------------- #

def collect_files(root: Path, excludes: list[str], use_gitignore: bool,
                  skip: set[Path]) -> list[Path]:
    # .gitignore specs by the directory that holds them (nested ones apply to their subtree)
    specs: dict[Path, pathspec.GitIgnoreSpec] = {}

    def excluded(path: Path, is_dir: bool) -> bool:
        rel = path.relative_to(root).as_posix()
        if any(fnmatch.fnmatch(path.name, p) or fnmatch.fnmatch(rel, p) for p in excludes):
            return True
        for base, spec in specs.items():
            if base in path.parents:
                sub = path.relative_to(base).as_posix()
                if spec.match_file(sub + "/" if is_dir else sub):
                    return True
        return False

    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        cur = Path(dirpath)
        gitignore = cur / ".gitignore"
        if use_gitignore and gitignore.is_file():
            specs[cur] = pathspec.GitIgnoreSpec.from_lines(
                gitignore.read_text(errors="ignore").splitlines())
        dirnames[:] = sorted(d for d in dirnames if not excluded(cur / d, True))
        for name in sorted(filenames):
            path = cur / name
            if path.resolve() in skip or excluded(path, False) or not path.is_file():
                continue
            found.append(path)
    # files in a directory come before its subdirectories
    found.sort(key=lambda p: (p.relative_to(root).parent.parts, p.name))
    return found


def read_text(path: Path, max_bytes: int) -> str | None:
    """Return file contents, or None if it looks binary or is too large."""
    try:
        if path.stat().st_size > max_bytes:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    sample = data[:8192]
    if b"\0" in sample:
        return None
    # many control bytes (other than \t \n \f \r \x1b) => binary
    controls = sum(b < 32 and b not in (9, 10, 12, 13, 27) for b in sample)
    if sample and controls / len(sample) > 0.05:
        return None
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


# --------------------------------------------------------------------------- #
# Highlighting and layout
# --------------------------------------------------------------------------- #

@dataclass
class TokenStyle:
    color: str
    bold: bool
    italic: bool


@dataclass
class VisualLine:
    number: int | None  # None for wrapped continuation lines
    runs: list[tuple[str, TokenStyle]]


@dataclass
class FileDoc:
    rel_path: str
    pages: list[list[VisualLine]] = field(default_factory=list)


@dataclass
class TocRow:
    text: str
    doc: int | None = None  # index into docs; None for a directory heading
    indent: bool = False


def get_lexer(path: Path, text: str):
    for fn in (get_lexer_for_filename, guess_lexer_for_filename):
        try:
            return fn(path.name, text, stripnl=False, ensurenl=False)
        except ClassNotFound:
            continue
    return TextLexer(stripnl=False, ensurenl=False)


def highlight_lines(path: Path, text: str, style, tab_size: int) -> list[list[tuple[str, TokenStyle]]]:
    """Tokenize text into logical lines, each a list of (text, style) runs."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").expandtabs(tab_size)
    default_color = "#000000"
    cache: dict = {}
    lines: list[list[tuple[str, TokenStyle]]] = [[]]
    for ttype, value in get_lexer(path, text).get_tokens(text):
        if ttype not in cache:
            # some lexers emit token types the style doesn't know; use the nearest known parent
            known = ttype
            while not style.styles_token(known) and known.parent is not None:
                known = known.parent
            s = style.style_for_token(known)
            cache[ttype] = TokenStyle("#" + s["color"] if s["color"] else default_color,
                                      s["bold"], s["italic"])
        tstyle = cache[ttype]
        parts = value.split("\n")
        for i, part in enumerate(parts):
            if i:
                lines.append([])
            if part:
                lines[-1].append((part, tstyle))
    if len(lines) > 1 and not lines[-1]:
        lines.pop()  # trailing newline
    return lines


def wrap_line(number: int, runs: list[tuple[str, TokenStyle]], width: int) -> list[VisualLine]:
    out = [VisualLine(number, [])]
    used = 0
    for value, tstyle in runs:
        while value:
            if used == width:
                out.append(VisualLine(None, []))
                used = 0
            take = value[: width - used]
            out[-1].runs.append((take, tstyle))
            used += len(take)
            value = value[len(take):]
    return out


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

class Renderer:
    def __init__(self, args):
        self.page_w, self.page_h = args.page_size
        self.margin = 15 * mm
        self.font_size = args.font_size
        self.leading = self.font_size * 1.25
        self.header_size = 9
        self.toc_size = 10
        self.toc_leading = 16
        code_fonts = list(FONT_CANDIDATES)
        if args.font:
            code_fonts.insert(0, [(args.font, 0)] * 4)
        self.fonts = self._register_fonts("Code", code_fonts, BUILTIN_FONTS)
        self.sans, self.sans_bold = self._register_fonts("UI", UI_FONT_CANDIDATES, BUILTIN_UI_FONTS)
        self.char_w = pdfmetrics.stringWidth("M", self.fonts[0], self.font_size)

        self.header_y = self.page_h - self.margin
        self.header_rule_y = self.header_y - 4
        self.footer_y = self.margin
        self.footer_rule_y = self.footer_y + self.header_size + 3
        self.body_top = self.header_rule_y - 6 - self.font_size
        self.body_bottom = self.footer_rule_y + 6
        self.lines_per_page = max(1, int((self.body_top - self.body_bottom) / self.leading) + 1)

    @staticmethod
    def _register_fonts(prefix: str, candidates, builtin: tuple[str, ...]) -> tuple[str, ...]:
        """Register the first family whose files all exist; fall back to builtin PDF fonts."""
        for family in candidates:
            if not all(Path(p).is_file() for p, _ in family):
                continue
            names = []
            try:
                for idx, (p, sub) in enumerate(family):
                    name = f"{prefix}-{Path(p).stem}-{sub}-{idx}"
                    pdfmetrics.registerFont(TTFont(name, p, subfontIndex=sub))
                    names.append(name)
                return tuple(names)
            except Exception:
                continue
        return builtin

    def font_for(self, ts: TokenStyle) -> str:
        return self.fonts[(1 if ts.bold else 0) + (2 if ts.italic else 0)]

    def layout(self, rel_path: str, lines, show_line_numbers: bool) -> FileDoc:
        gutter = len(str(len(lines))) + 1 if show_line_numbers else 0
        width = int((self.page_w - 2 * self.margin) / self.char_w) - gutter
        visual: list[VisualLine] = []
        for n, runs in enumerate(lines, 1):
            visual.extend(wrap_line(n, runs, max(width, 10)))
        doc = FileDoc(rel_path)
        for i in range(0, max(len(visual), 1), self.lines_per_page):
            doc.pages.append(visual[i:i + self.lines_per_page])
        return doc

    # ---- table of contents ------------------------------------------------ #

    @staticmethod
    def _toc_rows(docs: list[FileDoc]) -> list[TocRow]:
        rows, cur_dir = [], ""
        for i, doc in enumerate(docs):
            parent, _, name = doc.rel_path.rpartition("/")
            if parent != cur_dir:
                cur_dir = parent
                rows.append(TocRow(parent + "/"))
            rows.append(TocRow(name, i, indent=bool(parent)))
        return rows

    def _paginate_toc(self, rows: list[TocRow]) -> list[list[TocRow]]:
        avail = self.header_rule_y - 14 - self.body_bottom
        per_page = max(2, int(avail / self.toc_leading))
        first_page = max(2, int((avail - self.toc_heading_h) / self.toc_leading))
        pages, i = [], 0
        while i < len(rows):
            n = first_page if not pages else per_page
            chunk = rows[i:i + n]
            if len(chunk) > 1 and chunk[-1].doc is None and i + n < len(rows):
                chunk.pop()  # don't leave a directory heading orphaned at the bottom
            pages.append(chunk)
            i += len(chunk)
        return pages or [[]]

    toc_heading_h = 44

    def _draw_toc(self, c, rows: list[TocRow], starts: list[int], first: bool):
        left, right = self.margin, self.page_w - self.margin
        y = self.header_rule_y - 14 - self.toc_size
        if first:
            c.setFillColor(TITLE_COLOR)
            c.setFont(self.sans_bold, 18)
            c.drawString(left, y - 8, "Contents")
            y -= self.toc_heading_h
        for row in rows:
            x = left + (14 if row.indent else 0)
            font = self.sans if row.doc is not None else self.sans_bold
            num = str(starts[row.doc]) if row.doc is not None else ""
            num_w = pdfmetrics.stringWidth(num, self.sans, self.toc_size)
            text = self._fit(row.text, font, self.toc_size, right - x - num_w - 30)
            text_w = pdfmetrics.stringWidth(text, font, self.toc_size)

            c.setFillColor(TITLE_COLOR)
            c.setFont(font, self.toc_size)
            c.drawString(x, y, text)
            if row.doc is not None:
                c.drawRightString(right, y, num)
                dot_w = pdfmetrics.stringWidth(" .", self.sans, self.toc_size)
                n_dots = int((right - num_w - 6 - (x + text_w + 6)) / dot_w)
                c.setFillColor(LEADER_COLOR)
                c.drawRightString(right - num_w - 6, y, " ." * max(n_dots, 0))
                c.linkAbsolute("", f"file{row.doc}",
                               Rect=(x, y - 4, right, y + self.toc_size), thickness=0)
            y -= self.toc_leading

    # ---- title page ---------------------------------------------------- #

    def _draw_title_page(self, c, info: ProjectInfo):
        cx = self.page_w / 2
        max_w = self.page_w - 2 * self.margin - 20 * mm
        size = 36
        while True:
            lines = simpleSplit(info.name, self.sans_bold, size, max_w)
            if len(lines) <= 3 or size <= 20:
                break
            size -= 2
        leading = size * 1.2

        y = self.page_h * 0.6 + (len(lines) - 1) * leading / 2
        c.setFillColor(TITLE_COLOR)
        c.setFont(self.sans_bold, size)
        for line in lines:
            c.drawCentredString(cx, y, line)
            y -= leading
        y += leading  # baseline of the last line

        c.setStrokeColor(ACCENT_COLOR)
        c.setLineWidth(2)
        c.line(cx - 30, y - 22, cx + 30, y - 22)

        if info.version:
            c.setFillColor(SUBTITLE_COLOR)
            c.setFont(self.sans, 16)
            c.drawCentredString(cx, y - 52, f"Version {info.version.lstrip('vV')}")

        if info.company:
            c.setFillColor(HEADER_FOOTER_COLOR)
            c.setFont(self.sans_bold, 13)
            company = simpleSplit(info.company, self.sans_bold, 13, max_w)[0]
            c.drawCentredString(cx, self.margin + 25 * mm, company)

    # ---- document ------------------------------------------------------ #

    def render(self, out_path: Path, info: ProjectInfo, docs: list[FileDoc],
               background: str | None, show_line_numbers: bool) -> int:
        toc_pages = self._paginate_toc(self._toc_rows(docs))
        starts, page = [], 2 + len(toc_pages)  # title page + contents come first
        for doc in docs:
            starts.append(page)
            page += len(doc.pages)
        total = page - 1

        c = canvas.Canvas(str(out_path), pagesize=(self.page_w, self.page_h))
        c.setTitle(info.label)
        c.setSubject("Source code listing")
        if info.company:
            c.setAuthor(info.company)

        c.bookmarkPage("title")
        c.addOutlineEntry(info.label, "title", level=0)
        self._draw_title_page(c, info)
        c.showPage()
        page_no = 1

        for i, rows in enumerate(toc_pages):
            page_no += 1
            if i == 0:
                c.bookmarkPage("contents")
                c.addOutlineEntry("Contents", "contents", level=0)
            self._draw_chrome(c, info.label, "Contents", page_no, total)
            self._draw_toc(c, rows, starts, first=i == 0)
            c.showPage()

        for doc_idx, doc in enumerate(docs):
            gutter = len(str(max((l.number or 0 for p in doc.pages for l in p), default=1))) + 1
            for p_idx, page in enumerate(doc.pages):
                page_no += 1
                if p_idx == 0:
                    key = f"file{doc_idx}"
                    c.bookmarkPage(key)
                    c.addOutlineEntry(doc.rel_path, key, level=0)
                self._fill_background(c, background)
                self._draw_chrome(c, info.label, doc.rel_path, page_no, total)
                self._draw_body(c, page, gutter if show_line_numbers else 0)
                c.showPage()
        c.save()
        return total

    def _fill_background(self, c, background: str | None):
        if background and background.lower() not in ("#fff", "#ffffff"):
            left, right = self.margin, self.page_w - self.margin
            c.setFillColor(HexColor(background))
            c.rect(left, self.body_bottom - 3, right - left,
                   self.header_rule_y - self.body_bottom, stroke=0, fill=1)

    def _draw_chrome(self, c, left_text, right_text, page_no, total):
        left, right = self.margin, self.page_w - self.margin
        size = self.header_size
        c.setFillColor(HEADER_FOOTER_COLOR)
        left_text = self._fit(left_text, self.sans_bold, size, (right - left) / 2, keep="start")
        c.setFont(self.sans_bold, size)
        c.drawString(left, self.header_y, left_text)
        c.setFont(self.sans, size)
        avail = right - left - c.stringWidth(left_text, self.sans_bold, size) - 20
        c.drawRightString(right, self.header_y, self._fit(right_text, self.sans, size, avail))
        c.drawCentredString(self.page_w / 2, self.footer_y, f"{page_no}/{total}")

        c.setStrokeColor(RULE_COLOR)
        c.setLineWidth(0.6)
        c.line(left, self.header_rule_y, right, self.header_rule_y)
        c.line(left, self.footer_rule_y, right, self.footer_rule_y)

    @staticmethod
    def _fit(text, font, size, max_w, keep="end"):
        """Truncate with an ellipsis; by default from the left so a path's file name stays visible."""
        width = lambda s: pdfmetrics.stringWidth(s, font, size)
        if width(text) <= max_w:
            return text
        while text and width(text + "…" if keep == "start" else "…" + text) > max_w:
            text = text[:-1] if keep == "start" else text[1:]
        return text + "…" if keep == "start" else "…" + text

    def _draw_body(self, c, page: list[VisualLine], gutter: int):
        x0 = self.margin
        code_x = x0 + gutter * self.char_w
        y = self.body_top
        for line in page:
            if gutter and line.number is not None:
                c.setFont(self.fonts[0], self.font_size)
                c.setFillColor(LINE_NO_COLOR)
                c.drawRightString(code_x - self.char_w, y, str(line.number))
            t = c.beginText(code_x, y)
            for value, ts in line.runs:
                t.setFont(self.font_for(ts), self.font_size)
                t.setFillColor(HexColor(ts.color))
                t.textOut(value)
            c.drawText(t)
            y -= self.leading


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Render all files of a project directory into one syntax-highlighted PDF.")
    p.add_argument("project", type=Path, help="project directory")
    p.add_argument("-o", "--output", type=Path,
                   help="output PDF path (default: <project-name>.pdf in the current directory)")
    p.add_argument("--company", help="company name for the title page (overrides the detected one)")
    p.add_argument("--title", help="project name for the title page and headers (overrides the detected one)")
    p.add_argument("--project-version", metavar="VERSION",
                   help="project version (overrides the detected one)")
    p.add_argument("--style", default="default",
                   help="Pygments color style, e.g. default, friendly, monokai, github-dark (default: default)")
    p.add_argument("--font-size", type=float, default=8.0, help="code font size in points (default: 8)")
    p.add_argument("--font", help="path to a monospace .ttf to use instead of the auto-detected one")
    p.add_argument("--paper", choices=["a4", "letter"], default="a4")
    p.add_argument("--landscape", action="store_true")
    p.add_argument("--tab-size", type=int, default=4)
    p.add_argument("--no-line-numbers", action="store_true")
    p.add_argument("-x", "--exclude", action="append", default=[], metavar="GLOB",
                   help="exclude files/dirs matching GLOB (name or relative path); repeatable")
    p.add_argument("--no-default-excludes", action="store_true",
                   help="do not skip .git, node_modules, __pycache__, etc.")
    p.add_argument("--no-gitignore", action="store_true", help="ignore the project's .gitignore files")
    p.add_argument("--max-size", type=int, default=1024, metavar="KB",
                   help="skip files larger than this many KB (default: 1024)")
    args = p.parse_args(argv)
    size = A4 if args.paper == "a4" else LETTER
    args.page_size = landscape(size) if args.landscape else size
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    root = args.project.resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 1
    out = (args.output or Path(f"{root.name}.pdf")).resolve()
    try:
        style = get_style_by_name(args.style)
    except ClassNotFound:
        print(f"error: unknown Pygments style {args.style!r}", file=sys.stderr)
        return 1

    excludes = ([] if args.no_default_excludes else DEFAULT_EXCLUDES) + args.exclude
    files = collect_files(root, excludes, not args.no_gitignore, skip={out})

    renderer = Renderer(args)
    docs, skipped = [], []
    for path in files:
        rel = path.relative_to(root).as_posix()
        text = read_text(path, args.max_size * 1024)
        if text is None:
            skipped.append(rel)
            continue
        lines = highlight_lines(path, text, style, args.tab_size)
        docs.append(renderer.layout(rel, lines, not args.no_line_numbers))

    if not docs:
        print("error: no text files found", file=sys.stderr)
        return 1

    info = detect(root)
    info.name = args.title or info.name
    info.version = args.project_version or info.version
    info.company = args.company or info.company
    print(f"Project: {info.label}" + (f" — {info.company}" if info.company else "")
          + (f"  (from {info.source})" if info.source else ""))

    out.parent.mkdir(parents=True, exist_ok=True)
    total = renderer.render(out, info, docs, style.background_color, not args.no_line_numbers)
    print(f"Wrote {out} — {len(docs)} files, {total} pages")
    if skipped:
        print(f"Skipped {len(skipped)} binary/oversized file(s):")
        for rel in skipped:
            print(f"  {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
