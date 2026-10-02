import argparse
import asyncio
import hashlib
import html as html_lib
import json
import logging
import os
import re
import sys
import time
import urllib.parse

import requests
from tqdm import tqdm

HAN_BASE = "han.buw.uw.edu.pl"


# --- wyjście dla człowieka -----------------------------------------------------
# Wszystko, co jest sprawozdaniem z postępu, idzie na STDERR: STDOUT musi zostać
# czysty, bo przy `-o -` leci tam HTML książki.

def _tty() -> bool:
    return sys.stderr.isatty()


_quiet = False   # -q: zadnego sprawozdania, tylko bledy


def _out(msg: str = "") -> None:
    if not _quiet:
        print(msg, file=sys.stderr, flush=True)


def _bold(msg: str) -> str:
    return f"\033[1m{msg}\033[0m" if _tty() else msg


_status_enabled = False   # ustawiane w main(): tylko terminal i tryb nie-verbose
_status_active = False


def status(msg: str) -> None:
    """Komunikat ulotny – nadpisywany i na końcu kasowany, nie zostaje na ekranie
    (dzięki temu pierwszą trwałą linią wydruku jest tytuł książki)."""
    global _status_active
    if not _status_enabled:
        return
    sys.stderr.write("\r\033[K" + msg)
    sys.stderr.flush()
    _status_active = True


def status_clear() -> None:
    global _status_active
    if _status_active:
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()
        _status_active = False


def _fmt_secs(s: float) -> str:
    if s < 60:
        return f"{s:.1f}s"
    minutes, seconds = divmod(int(s), 60)
    return f"{minutes}m{seconds:02d}s"


def _fmt_size(path: str) -> str:
    try:
        size = os.path.getsize(path)
    except OSError:
        return ""
    if size < 1024 * 1024:
        return f" ({size / 1024:.0f} kB)"
    return f" ({size / (1024 * 1024):.1f} MB)"


class _TqdmStreamHandler(logging.StreamHandler):
    """Pisze przez tqdm.write: komunikat trafia nad pasek postępu, a pasek jest
    przerysowany w tej samej linii (zwykły StreamHandler rozsypuje go na kilka)."""

    def __init__(self):
        super().__init__(sys.stderr)

    def emit(self, record):
        try:
            tqdm.write(self.format(record), file=sys.stderr)
        except Exception:   # noqa: BLE001 - logowanie nie może wysadzić programu
            self.handleError(record)


class _CleanFormatter(logging.Formatter):
    """Bez "INFO:root:" – w terminalu to tylko szum."""
    PREFIX = {
        logging.WARNING: "Uwaga: ",
        logging.ERROR: "Błąd: ",
        logging.CRITICAL: "Błąd: ",
    }

    def format(self, record):
        return self.PREFIX.get(record.levelno, "") + record.getMessage()


# Etapy WeasyPrint (nazwy zdarzeń "Step N - ..." z weasyprint.progress).
WP_STEPS = {
    1: "Wczytywanie HTML",
    2: "Wczytywanie CSS",
    3: "Stosowanie stylów CSS",
    4: "Budowa struktury dokumentu",
    5: "Układ stron i łamanie tekstu",
    6: "Rysowanie i zapis PDF",
    7: "Metadane PDF",
}


class WeasyprintProgressHandler(logging.Handler):
    """Każdy etap konwersji to osobna linia z czasem trwania.

    Etapy 5 (łamanie tekstu) i 6 (rysowanie) zajmują grubo ponad 90% czasu, więc
    etap 5 pokazuje dodatkowo numer przeliczanej strony – bez tego kilkuminutowe
    milczenie wygląda jak zawieszenie programu."""

    DESC_WIDTH = 44

    def __init__(self, total_pages=None):
        super().__init__()
        self._total = total_pages
        self._step = None
        self._label = ""
        self._detail = ""
        self._started = None
        self._last_draw = 0.0
        self._failed = False
        _out("Konwersja do PDF:")

    def _render(self, mark: str, elapsed=None) -> str:
        desc = f"{self._label} {self._detail}".strip()
        if elapsed is None:
            return f"  {mark} {desc}"
        return f"  {mark} {desc.ljust(self.DESC_WIDTH)} {_fmt_secs(elapsed)}"

    def _redraw(self) -> None:
        if not _tty():
            return
        sys.stderr.write("\r\033[K" + self._render("…"))
        sys.stderr.flush()

    def _finish_step(self) -> None:
        if self._step is None:
            return
        if self._step == 5:  # licznik stron ma sens tylko w trakcie
            self._detail = f"({self._total} stron)" if self._total else ""
        if _tty():
            sys.stderr.write("\r\033[K")
        _out(self._render("✗" if self._failed else "✓", time.time() - self._started))

    def emit(self, record):
        match = re.match(r"Step (\d+)(?: - (.*))?", record.getMessage())
        if not match:
            return
        step = int(match.group(1))
        rest = match.group(2) or ""

        if step != self._step:
            self._finish_step()
            self._step = step
            self._label = WP_STEPS.get(step) or rest or f"Etap {step}"
            self._detail = ""
            self._started = time.time()
            self._last_draw = 0.0
            self._redraw()

        if step != 5:
            return
        page = re.search(r"Page (\d+)", rest)
        if page:
            self._detail = f"(strona {page.group(1)}/{self._total})" if self._total \
                else f"(strona {page.group(1)})"
        elif "Repagination" in rest:
            self._detail = "(ponowne przeliczanie stron)"
        else:
            return
        now = time.time()
        if now - self._last_draw > 0.1:
            self._last_draw = now
            self._redraw()

    def fail(self) -> None:
        """Etap przerwany wyjatkiem – zeby nie zameldowac fałszywego sukcesu."""
        self._failed = True

    def close(self):
        self._finish_step()
        self._step = None
        super().close()


class BookMetadata:
    """Metadane książki z REST API czytnika PWN (books/ibuk/<ibukId>)."""
    def __init__(self, data) -> None:
        self._data = data
        self.ibuk_id: int = data.get("ibukId")
        self.internal_id: str = data.get("id")  # Mongo ObjectId – używane przez socket
        self.index = self.ibuk_id
        self.author: str = data.get("editorialOffice") or data.get("author") or ""
        self.isbn: str = data.get("isbn") or ""
        self.pages: str = str(data.get("pageCount") or data.get("pages") or "")
        publisher = data.get("publisher") or {}
        self.publisher: str = publisher.get("name") if isinstance(publisher, dict) else str(publisher)
        self.title: str = data.get("title") or ""
        self.description: str = data.get("description") or ""


class IbukSession(requests.Session):
    """Sesja HTTP: logowanie przez HAN (BUW) + REST czytnika PWN."""

    def __init__(self) -> None:
        super().__init__()
        self._token = None           # token sesji HAN (subdomena)
        self._session_host = None    # host libra po zalogowaniu
        self._reader_host = None     # host czytnik.ibuk.pl w wersji HAN

        self.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:147.0) Gecko/20100101 Firefox/147.0",
            "Accept-Language": "pl,en-US;q=0.7,en;q=0.3",
        })

    # --- HAN (subdomenowe proxy) ---

    def han_host(self, host: str) -> str:
        """libra.ibuk.pl -> libra-1ibuk-1pl-1<token>.han.buw.uw.edu.pl"""
        if not self._token:
            raise RuntimeError("Brak tokenu sesji HAN – najpierw zaloguj się (login_uw).")
        return f"{host.replace('.', '-1')}-1{self._token}.{HAN_BASE}"

    def _follow_manual(self, url: str, timeout: int = 30, max_hops: int = 10):
        """GET z ręcznym podążaniem za przekierowaniami (hop po hopie).

        Automatyczne podążanie za przekierowaniami w `requests` (resolve_redirects)
        potrafi zawiesić się na łańcuchu proxy HAN (żądanie nigdy nie dostaje
        odpowiedzi mimo że ten sam skok wykonany jako osobne, świeże GET wraca
        natychmiast) – dlatego przechodzimy przez przekierowania ręcznie."""
        for _ in range(max_hops):
            r = self.get(url, timeout=timeout, allow_redirects=False)
            location = r.headers.get("Location")
            if not location or r.status_code not in (301, 302, 303, 307, 308):
                return r
            url = urllib.parse.urljoin(url, location)
        raise RuntimeError(f"Zbyt wiele przekierowań przy: {url}")

    def login_uw(self, username, password, retries: int = 2):
        """Logowanie z ponowieniami – łańcuch proxy HAN czasem zawiesza pojedyncze
        żądanie na całym timeoucie (patrz `_follow_manual`); pojedynczy retry po
        krótkiej przerwie zwykle wystarcza."""
        for attempt in range(1, retries + 2):
            try:
                return self._login_uw_once(username, password)
            except requests.exceptions.Timeout as e:
                if attempt > retries:
                    raise
                delay = 10 * attempt
                logging.warning(
                    f"Logowanie: timeout HAN ({e}), próba {attempt}/{retries} nieudana, "
                    f"ponawiam za {delay}s."
                )
                time.sleep(delay)

    def _login_uw_once(self, username, password):
        logging.info("Logowanie przez UW (HAN)")
        entry_url = f"https://{HAN_BASE}/han/libra/https/libra.ibuk.pl/"
        self._follow_manual(entry_url)  # cookiecheck

        data = {
            "plainuser": username,
            "pass2": "",
            "password": hashlib.md5(password.encode("utf-8")).hexdigest(),
            "user": username,
        }
        r = self.post("https://login.han.buw.uw.edu.pl/hhauth/login", data=data,
                       timeout=30, allow_redirects=False)
        location = r.headers.get("Location")
        if location:
            self._follow_manual(urllib.parse.urljoin(r.url, location))

        r = self._follow_manual(entry_url)
        assert r.status_code == 200
        final_host = urllib.parse.urlsplit(r.url).netloc

        m = re.match(rf"^(.*)-1([^.-]+)\.{re.escape(HAN_BASE)}$", final_host)
        if not m or final_host == HAN_BASE:
            logging.error(
                "Logowanie nie przekierowało na subdomenę sesji HAN. "
                f"Adres: {r.url}. Sprawdź login/hasło (numer ELS/karty, nie PESEL)."
            )
            raise PermissionError("Błąd logowania do systemu UW HAN.")

        self._session_host = final_host
        self._token = m.group(2)
        self._reader_host = self.han_host("czytnik.ibuk.pl")
        logging.info(f"Zalogowano. Host czytnika: {self._reader_host}")

    # --- REST czytnika (brama /api/v2 przed ścieżkami /proxy/...) ---

    def _reader_api(self, path: str):
        if not self._reader_host:
            raise RuntimeError("Brak sesji czytnika – wymagane logowanie (-u/-p).")
        url = f"https://{self._reader_host}/api/v2{path}"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": f"https://{self._reader_host}",
            "Referer": f"https://{self._reader_host}/",
            "X-Requested-With": "XMLHttpRequest",
        }
        r = self.get(url, headers=headers, timeout=30)
        return r

    @staticmethod
    def _book_id_from_url(url: str):
        digits = re.findall(r"(\d+)", urllib.parse.urlsplit(url).path)
        if not digits:
            raise ValueError(f"Nie znaleziono numeru książki w adresie: {url}")
        return digits[-1]

    def get_book_metadata(self, url) -> BookMetadata:
        ibuk_id = self._book_id_from_url(url)
        r = self._reader_api(f"/proxy/offers/api/v2/books/ibuk/{ibuk_id}")
        if r.status_code != 200:
            raise RuntimeError(f"Nie udało się pobrać metadanych ({r.status_code}): {r.text[:200]}")
        data = r.json()
        data.setdefault("ibukId", int(ibuk_id) if str(ibuk_id).isdigit() else ibuk_id)
        return BookMetadata(data)

    def open_read(self, internal_id: str, log_level: int = logging.INFO) -> dict:
        """Otwiera książkę do czytania: rezerwuje slot i zwraca obiekt dostępu.
        Musi być wywołane tuż przed połączeniem socketu (slot ~5 min)."""
        r = self._reader_api(f"/proxy/content/api/v2/book/{internal_id}/read")
        if r.status_code != 200:
            raise PermissionError(
                f"Brak dostępu do książki (read {r.status_code}): {r.text[:300]}"
            )
        rd = r.json()
        access = rd.get("access") or {}
        if not access.get("type"):
            raise PermissionError(f"Serwer nie przyznał dostępu: {rd}")
        if rd.get("isAllSlotsTaken"):
            logging.warning("Wszystkie sloty czytania zajęte – zamknij inne otwarte książki.")
        logging.log(
            log_level,
            f"Dostęp: type={access.get('type')} source={access.get('source')} "
            f"slotów={access.get('count')}"
        )
        return access


class ContentSocket:
    """Klient socket.io (engine.io v4) po HTTP long-pollingu dla usługi 'content'.
    WebSocket jest blokowany przez HAN, dlatego używamy wyłącznie pollingu."""

    def __init__(self, session: IbukSession, reader_host: str, book_object_id: str,
                 access_type: str, access_source: str, width: int = 839, font_size: int = 15) -> None:
        self._s = session
        self._host = reader_host
        self._url = f"https://{reader_host}/socket.io/"
        self._book_id = book_object_id
        self._access_type = access_type
        self._access_source = access_source
        self._width = width
        self._font_size = font_size
        self._sid = None
        self._headers = {
            "Origin": f"https://{reader_host}",
            "Referer": f"https://{reader_host}/",
            "Accept": "*/*",
        }

    def _base_q(self):
        return {
            "service": "content",
            "isServer": "0",
            "bookId": self._book_id,
            "accessType": self._access_type,
            "accessSource": self._access_source,
            "EIO": "4",
            "transport": "polling",
        }

    @staticmethod
    def _t():
        return format(int(time.time() * 1000), "x")

    def _get(self):
        q = self._base_q()
        q["t"] = self._t()
        if self._sid:
            q["sid"] = self._sid
        return self._s.get(self._url, params=q, headers=self._headers, timeout=40)

    def _post(self, payload: str):
        q = self._base_q()
        q["t"] = self._t()
        q["sid"] = self._sid
        h = dict(self._headers)
        h["Content-Type"] = "text/plain;charset=UTF-8"
        return self._s.post(self._url, params=q, data=payload.encode("utf-8"), headers=h, timeout=40)

    def _poll_packets(self):
        """Zwraca listę pakietów (bez pingów; na ping odsyła pong)."""
        r = self._get()
        out = []
        for pkt in (r.text.split("\x1e") if r.text else []):
            if not pkt:
                continue
            if pkt == "2":
                self._post("3")
                continue
            out.append(pkt)
        return out

    @staticmethod
    def _parse_event(pkt: str):
        if pkt.startswith("42"):
            try:
                arr = json.loads(pkt[2:])
                return arr[0], (arr[1] if len(arr) > 1 else None)
            except (ValueError, IndexError):
                return None
        return None

    def connect(self):
        # 1) handshake engine.io -> sid
        r = self._get()
        first = (r.text.split("\x1e")[0] if r.text else "")
        try:
            self._sid = json.loads(first[first.find("{"):])["sid"]
        except (ValueError, KeyError) as e:
            raise RuntimeError(f"Handshake socket.io nieudany: {e}; body={r.text[:200]!r}")

        # 2) socket.io CONNECT (namespace domyślny)
        self._post("40")
        reply = self._get().text
        if "44" in reply or "Unauthorized" in reply:
            raise PermissionError(f"Socket odrzucony (Unauthorized): {reply[:200]}")

    def request_combined(self, start: int, end: int, want_css=False, want_font=False, timeout=60) -> dict:
        """Wysyła book_combined_request i zbiera page-result (+ opcjonalnie css/font).
        Zwraca {'page': html, 'css': html|None, 'font': html|None}."""
        req = {
            "bookId": self._book_id,
            "page": {
                "start_pageNumber": start,
                "end_pageNumber": end,
                "fontSize": self._font_size,
                "compression": 10,
                "format": "html",
                "type": "standard",
                "width": self._width,
            },
        }
        if want_css:
            req["css"] = {"width": self._width, "fontSize": self._font_size}
        if want_font:
            req["font"] = {}

        self._post("42" + json.dumps(["book_combined_request", req], ensure_ascii=False))

        need = {"page-result"}
        if want_css:
            need.add("css-result")
        if want_font:
            need.add("font-result")

        got = {}
        deadline = time.time() + timeout
        while (need - got.keys()) and time.time() < deadline:
            for pkt in self._poll_packets():
                evt = self._parse_event(pkt)
                if evt is None:
                    continue
                name, payload = evt
                if name in ("exception", "error"):
                    raise RuntimeError(f"Serwer zwrócił błąd: {payload}")
                if name == "book_session_expired":
                    raise TimeoutError("Sesja czytania wygasła (book_session_expired).")
                if name.endswith("-result"):
                    html = (payload or {}).get("data", {}).get("html", "")
                    got[name] = html
        missing = need - got.keys()
        if missing:
            raise TimeoutError(f"Brak odpowiedzi serwera dla: {missing}")
        return {
            "page": got.get("page-result", ""),
            "css": got.get("css-result"),
            "font": got.get("font-result"),
        }

    def close(self):
        try:
            self._post("41")  # socket.io DISCONNECT (best-effort)
        except requests.RequestException:
            pass


def _clean(text: str) -> str:
    """Zamienia HTML na czysty tekst (do pól metadanych PDF)."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _meta_tags(meta: "BookMetadata") -> str:
    """Buduje znaczniki <meta> dla WeasyPrint -> metadane PDF."""
    if meta is None:
        return "<title></title>"
    title = _clean(meta.title)
    author = _clean(meta.author)

    tags = [f"<title>{html_lib.escape(title)}</title>"]
    if author:
        tags.append(f'<meta name="author" content="{html_lib.escape(author)}">')
    return "\n                    ".join(tags)


def build_html(pages, css, fonts, meta=None) -> str:
    page_width = 954.667
    page_height = 1326.65

    fonts = re.sub("; format", " format", fonts or "")
    pages_joined = "\n".join(f'<div class="pdf-page-wrapper">{p}</div>' for p in pages)

    pdf_styles = f"""
    @media print {{
        @page {{ size: {page_width}px {page_height}px; margin: 0; }}
        body, html {{ margin: 0 !important; padding: 0 !important; background-color: #fff; }}
        .pdf-page-wrapper {{
            page-break-after: always;
            break-after: page;
            position: relative;
            width: 100% !important;
            height: 100% !important;
            overflow: hidden;
            display: block;
            box-sizing: border-box;
        }}
        .pagetext {{
            width: 100% !important;
            height: 100% !important;
            transform-origin: top left;
            position: relative !important;
        }}
    }}
    """

    return f"""
            <!DOCTYPE html>
            <html lang="pl">
                <head>
                    <meta charset="UTF-8">
                    <meta name="viewport" content="width=device-width, initial-scale=1">
                    {_meta_tags(meta)}
                    <style>{css or ''}</style>
                    <style>{pdf_styles}</style>
                    <style id='font-style'>{fonts}</style>
                </head>
                <body style="margin: 0; padding: 0;">
                {pages_joined}
                </body>
            </html>"""


def download_book_html(ibs: IbukSession, meta: BookMetadata, page_count: int) -> str:
    """Pobiera wszystkie strony przez socket, odnawiając rezerwację w razie potrzeby."""
    def new_socket():
        access = ibs.open_read(meta.internal_id)
        sock = ContentSocket(
            ibs, ibs._reader_host, meta.internal_id,
            access.get("type"), access.get("source"),
        )
        sock.connect()
        return sock

    sock = new_socket()
    last_refresh = time.time()
    pages = []
    css = fonts = None

    try:
        i = 1
        pbar = tqdm(total=page_count, desc="Pobieranie stron", unit="str",
                    leave=True, file=sys.stderr, disable=_quiet)
        while i <= page_count:
            # Serwer wymaga pola "css" w KAŻDYM żądaniu strony, inaczej przeliczanie
            # pozycji/rozmiarów czcionek na danej stronie wychodzi jako NaN (px->rem);
            # fonty (@font-face) są za to wspólne dla całej książki, więc wystarczy raz.
            want_font = (i == 1)
            try:
                # Odświeżanie rezerwacji slotu (slot ~5 min).
                if time.time() - last_refresh > 200:
                    try:
                        ibs.open_read(meta.internal_id, log_level=logging.DEBUG)
                    except PermissionError:
                        pass
                    last_refresh = time.time()

                res = sock.request_combined(i, i, want_css=True, want_font=want_font)
            except (TimeoutError, RuntimeError, requests.RequestException) as e:
                logging.warning(f"Problem przy stronie {i} ({e}) – ponawiam połączenie.")
                sock.close()
                sock = new_socket()
                last_refresh = time.time()
                res = sock.request_combined(i, i, want_css=True, want_font=want_font)

            css = res.get("css") or css
            if want_font:
                fonts = res.get("font") or fonts
            pages.append(res["page"])
            pbar.update(1)
            i += 1
        pbar.close()
    finally:
        sock.close()

    status("Składanie dokumentu...")
    html = build_html(pages, css or "", fonts or "", meta)
    status_clear()
    return html


def write_output(book_html: str, output: str, page_count=None):
    if output.lower().endswith(".pdf"):
        wp_logger = logging.getLogger("weasyprint.progress")
        wp_logger.propagate = False
        wp_logger.setLevel(logging.INFO)
        try:
            from weasyprint import HTML
            import logging as wp_logging
        except ImportError:
            logging.error("Biblioteka 'weasyprint' nie jest zainstalowana! Zainstaluj: pip install weasyprint")
            sys.exit(1)

        progress_handler = None
        if not _quiet:
            progress_handler = WeasyprintProgressHandler(page_count)
            wp_logger.addHandler(progress_handler)

        # Wyciszamy zalew ostrzeżeń "Ignored ... NaNrem" z CSS generowanego przez serwer
        # (WeasyPrint bezpiecznie pomija te pojedyncze właściwości).
        main_wp_logger = wp_logging.getLogger("weasyprint")
        prev_level = main_wp_logger.level
        main_wp_logger.setLevel(wp_logging.ERROR)
        try:
            HTML(string=book_html).write_pdf(output)
        except BaseException:
            if progress_handler is not None:
                progress_handler.fail()
            raise
        finally:
            main_wp_logger.setLevel(prev_level)
            if progress_handler is not None:
                progress_handler.close()
                wp_logger.removeHandler(progress_handler)
        _out(_bold(f"Gotowe: {os.path.abspath(output)}") + _fmt_size(output))
    else:
        if output == "-":
            sys.stdout.write(book_html)
        else:
            with open(output, "w+", encoding="utf-8") as f:
                f.write(book_html)
        if output != "-":
            _out(_bold(f"Gotowe (HTML): {os.path.abspath(output)}") + _fmt_size(output))


async def download_action(url: str, page_count, ibs: IbukSession, output):
    logging.info(f"Pobieranie książki z: {url}")
    status("Pobieranie informacji o książce...")
    meta = ibs.get_book_metadata(url)

    if not page_count:
        page_count = int(meta.pages)

    status_clear()
    # Pierwsza trwała linia wydruku: co właściwie się pobiera.
    _out(_bold(meta.title) + (f" — {meta.author}" if meta.author else ""))
    target = "STDOUT" if output == "-" else os.path.abspath(output)
    _out(f"{page_count} stron → {target}")

    # Sieć jest synchroniczna (requests) – uruchamiamy w wątku, by nie blokować pętli.
    book_html = await asyncio.to_thread(download_book_html, ibs, meta, page_count)
    write_output(book_html, output, page_count)


async def query_action(url: str, ibs: IbukSession):
    logging.info(f"Pobieranie informacji o książce: {url}")
    meta = ibs.get_book_metadata(url)
    print(f"Author: {meta.author}")
    print(f"Title: {meta.title}")
    print(f"Description: {meta.description}")
    print(f"Publisher: {meta.publisher}")
    print(f"Isbn: {meta.isbn}")
    print(f"Pages: {meta.pages}")
    print(f"Index: {meta.index}")


async def main():
    parser = argparse.ArgumentParser(
        prog="ibuk-dl",
        description="Pobierz książkę z czytnika PWN (ibuk) przez UW HAN.",
    )

    visibility_group = parser.add_mutually_exclusive_group()
    visibility_group.add_argument("-v", "--verbose", action="store_true", help="Tryb szczegółowy")
    visibility_group.add_argument("-q", "--quiet", action="store_true", help="Tryb cichy")

    subparsers = parser.add_subparsers(dest="action")

    download_parser = subparsers.add_parser("download", help="Pobierz książkę")
    download_parser.add_argument("--page-count", type=int, help="Liczba stron (opcjonalnie)")
    download_parser.add_argument("-o", "--output", default="-", help="Plik docelowy (- = STDOUT)")

    uw_auth_group = download_parser.add_argument_group(
        title="UW authentication",
        description="Logowanie kontem han.buw.uw.edu.pl (wymagane).",
    )
    uw_auth_group.add_argument("-u", "--username", help="Numer karty bibliotecznej / ELS")
    uw_auth_group.add_argument("-p", "--password", help="Hasło do konta bibliotecznego")

    query_parser = subparsers.add_parser("query", help="Pokaż informacje o książce")
    query_parser.add_argument("-u", "--username", help="Numer karty bibliotecznej / ELS")
    query_parser.add_argument("-p", "--password", help="Hasło do konta bibliotecznego")

    parser.add_argument("url", help="Adres książki (np. https://libra-...han.buw.uw.edu.pl/reader/...-157425)")

    args = parser.parse_args()

    logging_level = logging.WARNING
    if args.verbose:
        logging_level = logging.INFO
    elif args.quiet:
        logging_level = logging.CRITICAL
    handler = _TqdmStreamHandler()
    handler.setFormatter(_CleanFormatter())
    logging.basicConfig(level=logging_level, handlers=[handler])

    global _status_enabled, _quiet
    _quiet = bool(args.quiet)
    _status_enabled = _tty() and not args.verbose and not _quiet

    ibs = IbukSession()

    # Dane logowania: argumenty wiersza polecen maja priorytet, ale moga tez
    # przyjsc ze zmiennych srodowiskowych (IBUK_USER / IBUK_PASS) -- wtedy haslo
    # nie trafia do argv, czyli nie widza go inni uzytkownicy w /proc ani historia shella.
    username = getattr(args, "username", None) or os.environ.get("IBUK_USER")
    password = getattr(args, "password", None) or os.environ.get("IBUK_PASS")
    if bool(username) ^ bool(password):
        parser.error("Podaj zarowno login (-u / IBUK_USER), jak i haslo (-p / IBUK_PASS).")
    if not username:
        parser.error("Logowanie jest wymagane: podaj -u LOGIN -p HASLO lub ustaw IBUK_USER / IBUK_PASS.")
    status("Logowanie...")
    try:
        ibs.login_uw(username, password)
    finally:
        status_clear()

    if args.action == "download":
        await download_action(args.url, args.page_count, ibs, args.output)
    elif args.action == "query":
        await query_action(args.url, ibs)
    else:
        parser.error("Wybierz akcję: download albo query.")


def run_main():
    asyncio.run(main())


if __name__ == "__main__":
    run_main()
