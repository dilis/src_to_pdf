"""Detect a project's display name, version and company from its project files.

Supported: Flutter (pubspec.yaml), Python (pyproject.toml / setup.cfg / setup.py)
and JavaScript/React (package.json). Anything else falls back to the folder name.
"""

from __future__ import annotations

import configparser
import json
import plistlib
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Placeholder values that project generators write and nobody bothers to change.
BOILERPLATE_DESCRIPTIONS = re.compile(
    r"^(a new flutter (project|application|package|plugin)|a new dart|add your description here"
    r"|a sample command-line application|a starting point for dart|a web server built using the shelf"
    r"|a short description|description)\b", re.I)
BOILERPLATE_DISPLAY_NAMES = {
    "react app", "create react app sample", "vite app", "vite + react", "vite + react + ts",
    "app name", "flutter demo", "flutter app",
}
ACRONYMS = {"api", "cli", "css", "db", "gui", "html", "http", "io", "ios", "js", "json", "pdf",
            "sdk", "sql", "ui", "url", "ux", "xml", "ai", "ml", "id", "qr", "pos", "erp", "crm"}

MAX_TITLE_CHARS = 60
MAX_TITLE_WORDS = 8


@dataclass
class ProjectInfo:
    name: str
    version: str | None = None
    company: str | None = None
    kind: str | None = None        # "flutter", "python", "node" or None
    source: str | None = None      # project file the info came from

    @property
    def label(self) -> str:
        """How the project is referred to everywhere: name plus version."""
        if not self.version:
            return self.name
        v = self.version if self.version[:1] in "vV" else f"v{self.version}"
        return f"{self.name} {v}"


@dataclass
class _Raw:
    kind: str
    source: str
    name: str | None = None
    version: str | None = None
    description: str | None = None
    display_names: list[str | None] = field(default_factory=list)  # human-facing app names
    companies: list[str | None] = field(default_factory=list)      # explicit company fields
    authors: list[str | None] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Heuristics
# --------------------------------------------------------------------------- #

def looks_technical(s: str) -> bool:
    """True for identifiers like `my_app`, `@acme/web-client`, `myapp`."""
    s = s.strip()
    return " " not in s and (any(ch in s for ch in "_-./@") or s.islower())


def humanize(name: str) -> str:
    name = name.split("/")[-1]  # drop npm scope
    words = [w for w in re.split(r"[-_.\s]+", name) if w]
    return " ".join(w.upper() if w.lower() in ACRONYMS else w[:1].upper() + w[1:] for w in words)


def short_description(desc: str | None) -> str | None:
    """Return the description if it is short enough to stand in as a title."""
    if not desc:
        return None
    desc = desc.strip()
    if ("\n" in desc or len(desc) > MAX_TITLE_CHARS or len(desc.split()) > MAX_TITLE_WORDS
            or BOILERPLATE_DESCRIPTIONS.match(desc)):
        return None
    return desc.rstrip(".")


def good_display_name(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip()
    if (not s or "$(" in s or "${" in s or s.startswith("@")
            or looks_technical(s) or s.lower() in BOILERPLATE_DISPLAY_NAMES):
        return None
    return s


def choose_name(raw: _Raw) -> str:
    for d in raw.display_names:
        if good := good_display_name(d):
            return good
    if raw.name and not looks_technical(raw.name):
        return raw.name
    if desc := short_description(raw.description):
        return desc
    return humanize(raw.name)


def clean_holder(s: str | None) -> str | None:
    """Turn 'Copyright (c) 2021-2024 Acme, Inc. All rights reserved.' into 'Acme, Inc.'."""
    if not s:
        return None
    s = re.sub(r"<[^>]*>|\([^)]*@[^)]*\)|\(https?:[^)]*\)", "", s)  # emails / urls
    s = re.sub(r"\bcopyright\b|\(c\)|©|all rights reserved\.?", "", s, flags=re.I)
    s = re.sub(r"\b\d{4}(\s*[-–,]\s*(\d{4}|present))*\b", "", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip(" ,-")
    if s.endswith(".") and not re.search(r"\b(Inc|Ltd|Co|Corp|Pty|S\.A|B\.V)\.$", s):
        s = s[:-1].rstrip(" ,")
    if not s or re.fullmatch(r"[a-z0-9]+(\.[a-z0-9_-]+)+", s) or "example" in s.lower() or "[" in s:
        return None  # empty, reverse-domain id such as com.example, or a template placeholder
    return s


def license_holder(root: Path) -> str | None:
    for f in sorted(root.glob("LICEN[CS]E*")) + sorted(root.glob("COPYING*")):
        try:
            head = f.read_text(errors="ignore").splitlines()[:40]
        except OSError:
            continue
        for line in head:
            if re.match(r"\s*copyright\s*(\(c\)|©)?\s*\d{4}", line, re.I):
                if holder := clean_holder(line):
                    return holder
    return None


def person(author) -> str | None:
    """npm/poetry `Name <email> (url)` string, or a {name: ...} table."""
    if isinstance(author, dict):
        author = author.get("name")
    if not isinstance(author, str):
        return None
    return re.sub(r"<[^>]*>|\([^)]*\)", "", author).strip() or None


def _read(path: Path) -> str | None:
    try:
        return path.read_text(errors="ignore")
    except OSError:
        return None


def _first(*values):
    return next((v for v in values if v), None)


# --------------------------------------------------------------------------- #
# Flutter
# --------------------------------------------------------------------------- #

def _flutter(root: Path) -> _Raw | None:
    f = root / "pubspec.yaml"
    if not f.is_file():
        return None
    data = yaml.safe_load(_read(f) or "") or {}
    raw = _Raw("flutter", f.name, name=data.get("name"), description=data.get("description"),
               version=str(data["version"]) if data.get("version") else None)

    # The app's user-facing name lives in the platform runners.
    if text := _read(root / "web/manifest.json"):
        try:
            raw.display_names.append(json.loads(text).get("name"))
        except json.JSONDecodeError:
            pass
    if text := _read(root / "android/app/src/main/AndroidManifest.xml"):
        if m := re.search(r"<application\b[^>]*?android:label=\"([^\"]+)\"", text, re.S):
            raw.display_names.append(m.group(1))
    for plist in ("ios/Runner/Info.plist", "macos/Runner/Info.plist"):
        try:
            with open(root / plist, "rb") as fh:
                p = plistlib.load(fh)
            raw.display_names += [p.get("CFBundleDisplayName"), p.get("CFBundleName")]
        except Exception:
            pass
    if text := _read(root / "macos/Runner/Configs/AppInfo.xcconfig"):
        cfg = dict(re.findall(r"^\s*(\w+)\s*=\s*(.*?)\s*$", text, re.M))
        raw.display_names.append(cfg.get("PRODUCT_NAME"))
        raw.companies.append(clean_holder(cfg.get("PRODUCT_COPYRIGHT")))
    if text := _read(root / "windows/runner/Runner.rc"):
        rc = dict(re.findall(r'VALUE\s+"(\w+)",\s*"([^"]*)"', text))
        raw.display_names.append(rc.get("ProductName"))
        raw.companies += [clean_holder(rc.get("CompanyName")), clean_holder(rc.get("LegalCopyright"))]
    return raw


# --------------------------------------------------------------------------- #
# Python
# --------------------------------------------------------------------------- #

VERSION_RE = re.compile(r"""^__version__\s*(?::\s*str\s*)?=\s*['"]([^'"]+)['"]""", re.M)


def _version_from_file(path: Path) -> str | None:
    text = _read(path)
    if text is None:
        return None
    if m := VERSION_RE.search(text) or re.search(r"""^version\s*=\s*['"]([^'"]+)['"]""", text, re.M):
        return m.group(1)
    return text.strip() if path.suffix != ".py" and "\n" not in text.strip() else None


def _version_from_attr(root: Path, attr: str) -> str | None:
    """Resolve `pkg.module.__version__` style references."""
    module = attr.rsplit(".", 1)[0].replace(".", "/")
    for base in (root, root / "src"):
        for cand in (base / module / "__init__.py", base / f"{module}.py"):
            if v := _version_from_file(cand):
                return v
    return None


def _guess_python_version(root: Path, name: str | None) -> str | None:
    if not name:
        return None
    pkg = re.sub(r"[-.]+", "_", name).lower()
    for base in (root, root / "src"):
        for cand in (base / pkg / "__init__.py", base / pkg / "__version__.py",
                     base / pkg / "_version.py", base / pkg / "version.py", base / f"{pkg}.py"):
            if v := _version_from_file(cand):
                return v
    return None


def _cfg_value(root: Path, value: str | None) -> str | None:
    """setup.cfg values may be `attr: pkg.__version__` or `file: VERSION`."""
    if not value:
        return None
    if value.startswith("attr:"):
        return _version_from_attr(root, value[5:].strip())
    if value.startswith("file:"):
        return _version_from_file(root / value[5:].split(",")[0].strip())
    return value


def _python(root: Path) -> _Raw | None:
    files = [f for f in ("pyproject.toml", "setup.cfg", "setup.py") if (root / f).is_file()]
    if not files:
        return None
    raw = _Raw("python", files[0])

    if "pyproject.toml" in files:
        try:
            data = tomllib.loads(_read(root / "pyproject.toml") or "")
        except tomllib.TOMLDecodeError:
            data = {}
        proj = data.get("project", {})
        tool = data.get("tool", {})
        poetry = tool.get("poetry", {})
        raw.name = _first(proj.get("name"), poetry.get("name"))
        raw.description = _first(proj.get("description"), poetry.get("description"))
        raw.version = _first(proj.get("version"), poetry.get("version"))
        if not raw.version:
            dyn = tool.get("setuptools", {}).get("dynamic", {}).get("version", {})
            if dyn.get("attr"):
                raw.version = _version_from_attr(root, dyn["attr"])
            elif dyn.get("file"):
                f = dyn["file"]
                raw.version = _version_from_file(root / (f[0] if isinstance(f, list) else f))
            for path in (tool.get("hatch", {}).get("version", {}).get("path"),
                         tool.get("pdm", {}).get("version", {}).get("path")):
                if path and not raw.version:
                    raw.version = _version_from_file(root / path)
        raw.authors += [person(a) for a in proj.get("authors", []) + proj.get("maintainers", [])
                        + poetry.get("authors", []) + poetry.get("maintainers", [])]

    if "setup.cfg" in files:
        cp = configparser.ConfigParser(interpolation=None)
        try:
            cp.read(root / "setup.cfg")
            md = cp["metadata"] if cp.has_section("metadata") else {}
        except configparser.Error:
            md = {}
        raw.name = raw.name or md.get("name")
        raw.description = raw.description or md.get("description")
        raw.version = raw.version or _cfg_value(root, md.get("version"))
        raw.authors += [md.get("author"), md.get("maintainer")]

    if "setup.py" in files:
        text = _read(root / "setup.py") or ""

        def kw(key):
            m = re.search(rf"""\b{key}\s*=\s*['"]([^'"]+)['"]""", text)
            return m.group(1) if m else None
        raw.name = raw.name or kw("name")
        raw.description = raw.description or kw("description")
        raw.version = raw.version or kw("version")
        raw.authors += [kw("author"), kw("maintainer")]

    raw.version = raw.version or _guess_python_version(root, raw.name)
    return raw


# --------------------------------------------------------------------------- #
# JavaScript / React
# --------------------------------------------------------------------------- #

def _node(root: Path) -> _Raw | None:
    f = root / "package.json"
    if not f.is_file():
        return None
    try:
        data = json.loads(_read(f) or "{}")
    except json.JSONDecodeError:
        data = {}
    build = data.get("build") if isinstance(data.get("build"), dict) else {}
    raw = _Raw("node", f.name, name=data.get("name"), version=data.get("version"),
               description=data.get("description"))
    raw.display_names += [data.get("productName"), data.get("displayName"), build.get("productName")]
    for manifest in ("public/manifest.json", "public/site.webmanifest", "manifest.json"):
        try:
            m = json.loads(_read(root / manifest) or "{}")
            raw.display_names += [m.get("name"), m.get("short_name")]
        except json.JSONDecodeError:
            pass
    for html in ("index.html", "public/index.html"):
        if (text := _read(root / html)) and (m := re.search(r"<title>(.*?)</title>", text, re.S | re.I)):
            raw.display_names.append(m.group(1).strip())
    raw.companies.append(clean_holder(build.get("copyright")))
    raw.authors += [person(data.get("author"))] + [person(c) for c in data.get("contributors", [])[:1]]
    return raw


# --------------------------------------------------------------------------- #

def detect(root: Path) -> ProjectInfo:
    raw = _flutter(root) or _python(root) or _node(root)
    if raw is None:
        return ProjectInfo(name=humanize(root.name) if looks_technical(root.name) else root.name,
                           company=license_holder(root))
    raw.name = raw.name or root.name
    return ProjectInfo(
        name=choose_name(raw),
        version=str(raw.version).strip() if raw.version else None,
        # explicit company fields beat the license holder, which beats individual authors
        company=_first(*raw.companies, license_holder(root), *raw.authors),
        kind=raw.kind,
        source=raw.source,
    )
