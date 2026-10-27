# src-to-pdf

Print the source code of one or more projects into a single, syntax-highlighted PDF.

Built for Flutter, Python and React/JavaScript projects, but works on any directory.

## Features

- **Title page** with the project name, version and company
- **Clickable table of contents**, grouped by directory, plus PDF bookmarks
- **Syntax highlighting** for every language [Pygments](https://pygments.org/languages/) supports
- **Every file starts on a new page**
- **Header** with the project name and version on the left and the file path on the right
- **Footer** with `page_number/total_pages`
- **Rules** dividing the header and footer from the code
- Line numbers, with long lines wrapped
- **Several projects in one PDF**, each with its own title page
- Respects `.gitignore` files (nested ones too) and skips binary files

## Installation

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                  # then run with: uv run to-pdf ...
uv tool install .        # or install the `to-pdf` command globally
```

## Usage

```bash
# One project -> my_app.pdf in the current directory
to-pdf path/to/my_app

# Choose the output file and set the company for the title page
to-pdf path/to/my_app -o listing.pdf --company "Acme Inc."

# Several projects combined into one PDF
to-pdf app/ backend/ admin_web/ --document-name "Acme Platform Source Code"
```

### Options

| Option | Description |
|---|---|
| `-o, --output PATH` | Output PDF. Default: `<project-folder>.pdf`, or `<document-name>.pdf` for several projects. |
| `--document-name NAME` | Title of the document title page when combining several projects. |
| `--company NAME` | Company name for the title page. Overrides the detected one. |
| `--title NAME` | Project name. Overrides the detected one (single project only). |
| `--project-version VERSION` | Project version. Overrides the detected one (single project only). |
| `--style STYLE` | Pygments color style, e.g. `default`, `friendly`, `monokai`, `github-dark`. Default: `default`. |
| `--font-size PT` | Code font size in points. Default: `8`. |
| `--font PATH` | Monospace `.ttf` to use instead of the auto-detected font. |
| `--paper {a4,letter}` | Paper size. Default: `a4`. |
| `--landscape` | Landscape orientation. |
| `--tab-size N` | Spaces per tab. Default: `4`. |
| `--no-line-numbers` | Hide line numbers. |
| `-x, --exclude GLOB` | Skip files or directories matching `GLOB` (name or relative path). Repeatable. |
| `--no-default-excludes` | Don't skip the built-in list (see below). |
| `--no-gitignore` | Don't apply `.gitignore` files. |
| `--max-size KB` | Skip files larger than this. Default: `1024`. |

## Document layout

**Single project**

1. Title page: project name, version, company
2. Contents
3. The project's files

**Several projects**

1. Document title page: the `--document-name` and company. Without `--document-name` the title is left blank and a warning is printed.
2. Contents, with a heading per project
3. For each project: a title page (name and version, no company), then its files

Every page after the title page has a header, with the project name and version on the left and the file path (or "Contents") on the right, and a `page/total` footer. Wherever the project is mentioned it carries its version, e.g. `PMVIC Queue Display v1.0.0+6`.

## Project detection

The name, version and company are read from the project files.

| Project | Project file | Also checked |
|---|---|---|
| Flutter / Dart | `pubspec.yaml` | App display names in `android/…/AndroidManifest.xml`, `ios/Runner/Info.plist`, `web/manifest.json`, `macos/Runner/Configs/AppInfo.xcconfig`, `windows/runner/Runner.rc` |
| Python | `pyproject.toml` (PEP 621 or Poetry), `setup.cfg`, `setup.py` | `__version__` in the package when the version is dynamic |
| JavaScript / React | `package.json` | `productName`, `displayName`, `public/manifest.json`, the `<title>` in `index.html` |

If a directory has more than one of these, they are checked in the order above.

**Name**, in order of preference:

1. A human-readable app display name, such as the Android label or the `<title>`.
2. The package name, if it doesn't look technical.
3. The description, if it is a short one-liner (60 characters and 8 words at most).
4. The package name, tidied up: `inventory-dashboard` becomes "Inventory Dashboard".

Generator boilerplate is ignored, e.g. "A new Flutter project.", "React App" and "Add your description here".

**Company**, in order of preference:

1. `--company`
2. An explicit company field: Windows `CompanyName`, macOS `PRODUCT_COPYRIGHT`, or `build.copyright` in `package.json`.
3. The copyright holder in `LICENSE`.
4. The author (`authors` / `author`).

Reverse-domain IDs such as `com.example` are never used as a company name. When combining several projects, the document title page shows `--company`, or the detected company if all projects share the same one.

## What gets included

Every text file in the project, sorted by path. Files in a directory come before its subdirectories. These are skipped:

- Anything matched by a `.gitignore`, including nested ones
- Binary files and files over `--max-size`
- The built-in excludes: `.git`, `.hg`, `.svn`, `node_modules`, `__pycache__`, `.venv`, `venv`, `.idea`, `.vscode`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `.tox`, `dist`, `build`, `.DS_Store`, `*.pyc`, `*.lock`, `package-lock.json`

Skipped binary and oversized files are listed when the PDF is written.

## Fonts

Code is set in Menlo on macOS, DejaVu Sans Mono on Linux, or Courier New, whichever is found first. Headings use Helvetica, DejaVu Sans or Arial. If none of these are available, the built-in PDF fonts are used. These don't cover characters outside Latin-1, so use `--font` to supply a TTF in that case.
