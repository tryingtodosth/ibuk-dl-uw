
# IBUK Downloader (UW HAN Edition)

Download books from the PWN **IBUK / "Czytnik PWN"** reader and save them as PDF (or raw HTML).
This fork is adapted to the **University of Warsaw (UW) HAN authentication system** (BUW e-zbiory).

> **2026 rewrite:** BUW moved HAN to per-session *subdomain* proxying and PWN replaced the old
> `libra` reader with a new Angular **"Czytnik PWN"** app. The downloader was rewritten to match:
> it logs in through HAN, reserves a reading slot via the reader's REST API, and streams pages
> over socket.io (HTTP long-polling, because the WebSocket upgrade is blocked through HAN).

## Features

- Download a whole book (or a page range) and render it to a **PDF** with proper page breaks.
- Output raw **HTML** instead (use an `.html` filename, or `-` for stdout).
- Query book information (author, title, description, publisher, ISBN, pages).
- Works through the HAN proxy (subdomain rewriting + Bunny Shield + load balancing).
- Uses your **BUW (Biblioteka Uniwersytecka w Warszawie)** library account — no separate PWN/IBUK account needed.

## Installation

1. Clone the repository:
```shell
git clone https://github.com/tryingtodosth/ibuk-dl-uw
cd ibuk-dl-uw
```

2. Install the required Python packages:
```shell
pip install weasyprint requests tqdm
```

> **Note for Windows users:** `weasyprint` (used for PDF generation) needs the non-Python GTK3/Pango
> libraries. If you hit an `OSError` during PDF export, follow the official
> [WeasyPrint Windows installation guide](https://weasyprint.readthedocs.io/en/latest/install.html#windows).
> Linux/macOS usually work out of the box or via the system package manager.

## Usage

> **Login is required.** Access is granted through your library account, so every command needs
> `-u BUW_LOGIN -p BUW_PASSWORD`.

### 1. Find your book

Open the book in the BUW e-zbiory / IBUK catalogue in your browser and copy the address that
**ends with the book's numeric IBUK id**, e.g. `...-157425`:

```
https://libra-1ibuk-1pl-1XXXXXXXX.han.buw.uw.edu.pl/reader/biologia-molekularna-bakterii-jadwiga-baj-zdzislaw-157425
```

The script only needs that trailing number, so any of these also work:

- the HAN subdomain URL above,
- an old-style `https://han.buw.uw.edu.pl/han/libra/https/libra.ibuk.pl/reader/...-157425` URL,
- a plain `https://libra.ibuk.pl/reader/...-157425` URL.

> ⚠️ **Do not** use the in-app reader URL that looks like `/reader/6a1d75...ada9f/23` — that path
> contains the internal document id and a page number, not the IBUK id, and won't resolve.

### 2. Download the book

Save directly as PDF:

```shell
python -m ibuk_dl.main -v download -o "BOOK.pdf" \
  -u BUW_LOGIN -p "BUW_PASSWORD" \
  "https://libra-1ibuk-1pl-1XXXXXXXX.han.buw.uw.edu.pl/reader/biologia-molekularna-bakterii-jadwiga-baj-zdzislaw-157425"
```

> **⚠️ BUW credentials:** `BUW_LOGIN` is your Electronic Student ID (ELS) number or BUW Library
> Card number. **It is NOT your PESEL.** Wrap the password in quotes if it contains special characters.

### Options

- `--page-count N` — download only the first `N` pages. If omitted, the whole book is fetched.
- `-o`, `--output` — output file. A `.pdf` name renders a PDF; `.html` (or `-` for stdout) writes raw HTML.
- `-v` — add diagnostic log lines (login, access slots, per-page retries); `-q` — quiet.
- Relative `-o` paths are resolved against the directory you run the command in.

### Credentials from the system keyring (recommended)

Instead of typing `-u`/`-p` every time, the script also reads `IBUK_USER` and `IBUK_PASS` from the
environment (CLI flags still win). That keeps the password out of `argv`, so it is not visible to
other local users via `/proc/<pid>/cmdline` and never lands in your shell history.

Store the credentials once in your desktop wallet (libsecret / Secret Service — works with both
gnome-keyring and KWallet; on Arch/EndeavourOS: `pacman -S libsecret`):

```shell
secret-tool store --label='BUW / ibuk-dl' service ibuk-dl-uw key username
secret-tool store --label='BUW / ibuk-dl' service ibuk-dl-uw key password
```

Then install the `ibuk` wrapper to `~/.local/bin/ibuk` (see `contrib/ibuk`), which looks the secrets
up, creates the venv on first run, and derives the output filename from the URL slug. The PDF is
written to **the directory you are standing in**, not to the repo:

```shell
install -m 700 contrib/ibuk ~/.local/bin/ibuk
ibuk "https://libra-1ibuk-1pl-1XXXXXXXX.han.buw.uw.edu.pl/reader/biologia-molekularna-bakterii-jadwiga-baj-zdzislaw-157425"
```

Any extra `ibuk-dl` options still pass through, e.g. `ibuk "<URL>" -o Moja.pdf --page-count 5`.
Set `IBUK_VERBOSITY=-v` for the diagnostic log lines (the wrapper runs quiet by default).
If `secret-tool` is unavailable, the wrapper falls back to [`pass`](https://www.passwordstore.org/)
(`ibuk-dl-uw/username`, `ibuk-dl-uw/password`).

### Query book information

```shell
python -m ibuk_dl.main query -u BUW_LOGIN -p "BUW_PASSWORD" \
  "https://libra-1ibuk-1pl-1XXXXXXXX.han.buw.uw.edu.pl/reader/biologia-molekularna-bakterii-jadwiga-baj-zdzislaw-157425"
```

## Notes

- Downloads run **page by page** over long-polling, so a large book takes a few minutes; the reading
  slot is refreshed automatically and the connection reconnects on transient errors.
- The generated PDF stores only the book's **Title** and **Author** in its metadata (plus WeasyPrint's
  own producer/timestamps). Your login is **not** written into the file.

## Disclaimer

As stated in the license, the authors are not responsible for any damage caused by using this program.
Respect the terms of use of the IBUK/PWN service and the copyright and licensing of the content.
Downloading and/or sharing copyrighted material may be illegal in your country — download only what you
are entitled to, and keep it to yourself.
