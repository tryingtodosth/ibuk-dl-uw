import argparse
import asyncio
import json
import logging
import re
import sys
import hashlib
import urllib.parse

import requests
import websockets
from bs4 import BeautifulSoup, Tag

from .yeast import yeast


class BookMetadata:
    def __init__(self, data) -> None:
        self._data = data
        self.author: str = data["author"]
        self.index: int = data["index"]
        self.isbn: str = data["isbn"]
        self.pages: str = data["pages"]
        self.publisher: str = data["redaction"]
        self.slugged_title: str = data["slugged_title"]
        self.title: str = data["title"]
        self.description: str = data["review"]


class IbukWebSession(requests.Session):
    def __init__(self, api_key=None) -> None:
        super().__init__()
        self._api_key = api_key
        
        # Maskujemy główną sesję HTTP (WAF nas nie zablokuje)
        self.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:147.0) Gecko/20100101 Firefox/147.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "pl,en-US;q=0.7,en;q=0.3",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1"
        })

    def api_key(self) -> str:
        if self._api_key is not None:
            return self._api_key

        r = self.get("https://libra.ibuk.pl/")
        assert r.status_code == 200

        self._api_key = self.cookies["ilApiKey"]
        return self._api_key

    def login_uw(self, username, password):
        logging.info("Logging in with UW (HAN)")
        
        target_url = "https://han.buw.uw.edu.pl/han/libra/https/libra.ibuk.pl/"
        self.get(target_url)
        
        md5_password = hashlib.md5(password.encode('utf-8')).hexdigest()

        data = {
            "plainuser": username,
            "pass2": "",
            "password": md5_password,
            "user": username
        }
        
        r = self.post("https://login.han.buw.uw.edu.pl/hhauth/login", data=data)
        
        r = self.get(target_url)
        assert r.status_code == 200

        self._api_key = self.cookies.get("libra.ibuk.pl/@ilApiKey")
        if not self._api_key:
            logging.error("Nie udało się pobrać klucza API z ciasteczek. Logowanie mogło się nie powieść.")
            raise PermissionError("Błąd logowania do systemu UW HAN.")

    def get_book_metadata(self, url):
        r = self.get(url)
        assert r.status_code == 200

        soup = BeautifulSoup(r.text, "html.parser")
        page_state = soup.find("script", {"id": "app-libra-2-state"})
        assert type(page_state) is Tag

        page_state = json.loads(str(page_state.contents[0]).replace("&q;", '"'))

        return BookMetadata(page_state["DETAILS_CACHE_KEY"])


class IbukWebSocketSession:
    # ZMIANA: Skierowaliśmy strumień na prawdziwy serwer wczytywania książek (libra22)
    def __init__(
        self, api_key: str, web_session: requests.Session, socket_io_base_url="libra22.ibuk.pl/socket.io"
    ) -> None:
        self._api_key = api_key
        self._session = web_session 
        self._socket_io_base_url = socket_io_base_url

    async def _connect(self):
        sid, cookie_str = self._create_session()
        
        await asyncio.sleep(0.5)
        
        # Na tym węźle apiKey musi być ponownie dołączone do samego WebSocketu
        params = {
            "apiKey": self._api_key,
            "isServer": "0",
            "EIO": "4",
            "transport": "websocket",
            "sid": sid
        }
        ws_url = f"wss://{self._socket_io_base_url}/?{urllib.parse.urlencode(params)}"
        logging.info(f"Nawiązywanie połączenia WebSocket z: {ws_url}")
        
        headers = {
            "Origin": "https://han.buw.uw.edu.pl",
            "Accept-Language": "pl,en-US;q=0.7,en;q=0.3",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        if cookie_str:
            headers["Cookie"] = cookie_str
            
        try:
            self.ws = await websockets.connect(
                ws_url,
                max_size=None,
                user_agent_header="Mozilla/5.0 (X11; Linux x86_64; rv:147.0) Gecko/20100101 Firefox/147.0",
                additional_headers=headers
            )
        except websockets.exceptions.InvalidStatus as e:
            logging.error(f"Odrzucono połączenie WebSocket ze statusem: {e.response.status_code}")
            raise

        await self._hello()

    def _create_session(self) -> tuple[str, str]:
        url = f"https://{self._socket_io_base_url}/"
        params = {
            "apiKey": self._api_key,
            "isServer": "0",
            "EIO": "4",
            "transport": "polling",
            "t": yeast(),
        }
        
        headers = {
            "Origin": "https://han.buw.uw.edu.pl",
            "Referer": "https://han.buw.uw.edu.pl/",
            "Accept": "*/*",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "cross-site"
        }
        
        logging.info(f"Inicjalizacja sesji Polling (GET {url})")
        r = self._session.get(url, params=params, headers=headers, timeout=15)
        assert r.status_code == 200
        
        json_start = r.text.find('{')
        json_data = json.loads(r.text[json_start:])
        
        cookie_parts = []
        for c in self._session.cookies:
            if "ibuk.pl" in c.domain:
                cookie_parts.append(f"{c.name}={c.value}")
        cookie_str = "; ".join(cookie_parts)
        
        return json_data["sid"], cookie_str

    async def __aenter__(self):
        await self._connect()
        return self

    async def __aexit__(self, *_):
        await self.close()

    async def close(self):
        await self.ws.close()

    async def _hello(self):
        await self.ws.send("2probe")
        assert await self.ws.recv() == "3probe"
        await self.ws.send("5")

        await self.ws.send("40/books,")
        res = str(await self.ws.recv())
        assert "40/books," in res
        
        # ZMIANA: Czasami Socket.IO w wersji 4 skleja pakiety za pomocą separatora bajtowego,
        # lub przesyła {"usrSocketId": ...}. Szukamy więc swobodnie słowa ["ready".
        if '42/books,["ready"' in res:
            return
            
        ready_msg = await self._handle_recv()
        if '42/books,["ready"' not in ready_msg:
            logging.error(f"Serwer odmówił wydania książki: {ready_msg}")
            
        assert '42/books,["ready"' in ready_msg

    async def _handle_recv(self):
        while True:
            msg = str(await self.ws.recv())
            if msg == "2":
                await self.ws.send("3")
            else:
                return msg


    async def get_page(self, book_id, page: int) -> str:
        await self.ws.send(
            f"""42/books,["page","{{\\"bookId\\":{book_id},\\"compressed\\":10,\\"format\\":\\"html\\",\\"pagenumber\\":{page},\\"fontSize\\":15.04,\\"pageNumber\\":{page},\\"compression\\":10,\\"type\\":\\"standard\\",\\"width\\":839}}"]"""
        )
        r = await self._handle_recv()

        data = json.loads(json.loads(r.split("42/books,")[1])[1])
        if data.get("error", False):
            logging.error(f"encountered while fetching page {page}: {data.get('message', '')}")
            raise PermissionError("Error while fetching page")
        return data["html"]

    async def get_css(self, book_id):
        await self.ws.send(
            f"""42/books,["css","{{\\"bookId\\":{book_id},\\"width\\":839,\\"fontSize\\":15.04}}"]"""
        )
        r = await self._handle_recv()
        return json.loads(json.loads(r.split("42/books,")[1])[1])["html"]

    async def get_fonts(self, book_id):
        await self.ws.send(f"""42/books,["font","{{\\"bookId\\":{book_id}}}"]""")
        r = await self._handle_recv()
        fonts = json.loads(json.loads(r.split("42/books,")[1])[1])["html"]
        fonts = re.sub("; format", " format", fonts)
        return fonts
    
    async def get_book_html(self, book_id, page_n: int) -> str:
        fonts = await self.get_fonts(book_id)
        style = await self.get_css(book_id)
        pages = []
        
        # Zmienne domyślne (zostaną nadpisane)
        page_width = 839
        page_height = 1187 

        for i in range(1, page_n + 1):
            print(f"Pobieranie strony {i} / {page_n} ...")
            logging.info(f"Pobieranie strony {i} z {page_n}...")
            try:
                page = await self.get_page(book_id, i)
            except PermissionError:
                break
                
            # Skanujemy pierwszą stronę w poszukiwaniu jej precyzyjnych wymiarów
            if i == 1:
                match = re.search(r'width:\s*([\d.]+)px;\s*height:\s*([\d.]+)px', page)
                if match:
                    page_width = float(match.group(1))
                    page_height = float(match.group(2))
                    print(f"Wykryto idealny rozmiar kartki docelowej: {page_width} x {page_height} px")
            
            pages.append(f'<div class="pdf-page-wrapper">{page}</div>')
            
        print("\nZakończono pobieranie z serwera! Składanie pliku...")

        pages_joined = "\n".join(pages)
        
        # Używamy wyciągniętych ze skryptu wymiarów bezpośrednio do stworzenia PDF
        pdf_styles = f"""
        @media print {{
            body, html {{ margin: 0; padding: 0; background-color: #fff; }}
            .pdf-page-wrapper {{
                page-break-after: always;
                break-after: page;
                position: relative;
                overflow: hidden;
                display: block;
                width: {page_width}px;
                height: {page_height}px;
            }}
        }}
        @page {{
            size: {page_width}px {page_height}px;
            margin: 0;
        }}
        """

        html = f"""
                <!DOCTYPE html>
                <html lang="en">
                    <head>
                        <title></title>
                        <meta charset="UTF-8">
                        <meta name="viewport" content="width=device-width, initial-scale=1">
                        <style>{style}</style>
                        <style>{pdf_styles}</style>
                        <style id='font-style'>{fonts}</style>
                    </head>
                    <body style="margin: 0; padding: 0;">
                    {pages_joined}
                    </body>
                </html>"""
        return html
async def download_action(
    url: str, page_count: int | None, ibs: IbukWebSession, output
):
    logging.info(f"Fetching book from URL: {url}")
    book_metadata = ibs.get_book_metadata(url)

    if not page_count:
        page_count = int(book_metadata.pages)

    logging.info(f"Downloading {book_metadata.author} - {book_metadata.title}")
    
    async with IbukWebSocketSession(ibs.api_key(), ibs) as ibws:
        book = await ibws.get_book_html(book_metadata.index, page_count)

    # ZMIANA: Sprawdzamy rozszerzenie pliku wyjściowego i decydujemy o formacie
    if output.lower().endswith(".pdf"):
        try:
            from weasyprint import HTML
        except ImportError:
            logging.error("Biblioteka 'weasyprint' nie jest zainstalowana! Zainstaluj ją wpisując: pip install weasyprint")
            sys.exit(1)
            
        print("Trwa konwersja do pliku PDF (to może zająć chwilę przy dużych książkach)...")
        # Generowanie PDF bez wchodzenia do przeglądarki!
        HTML(string=book).write_pdf(output)
        print(f"Gotowe! Książka \"{book_metadata.title}\" została z sukcesem zapisana jako: {output}")
        
    else:
        try:
            if output == "-":
                sys.stdout.write(book)
            else:
                # Wymuszamy kodowanie UTF-8, żeby polskie znaki się nie rozsypały
                with open(output, "w+", encoding="utf-8") as f:
                    f.write(book)
            print(f"Gotowe! Książka \"{book_metadata.title}\" została zapisana jako HTML: {output}")
        except Exception as e:
            logging.error(f"Wystąpił błąd przy zapisie HTML: {e}")

async def query_action(url: str, ibs: IbukWebSession):
    logging.info(f"Querying book info for URL: {url}")

    book_metadata = ibs.get_book_metadata(url)

    print(f"Author: {book_metadata.author}")
    print(f"Title: {book_metadata.title}")
    print(f"Description: {book_metadata.description}")
    print(f"Publisher: {book_metadata.publisher}")
    print(f"Isbn: {book_metadata.isbn}")
    print(f"Pages: {book_metadata.pages}")
    print(f"Index: {book_metadata.index}")


async def main():
    parser = argparse.ArgumentParser(
        prog="ibuk-dl",
        description="Download a book or query book info from a given libra.ibuk.pl URL (UW HAN version)",
    )

    visibility_group = parser.add_mutually_exclusive_group()
    visibility_group.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose mode"
    )
    visibility_group.add_argument(
        "-q", "--quiet", action="store_true", help="Enable quiet mode"
    )

    subparsers = parser.add_subparsers(dest="action")

    download_parser = subparsers.add_parser("download", help="Download a book")

    download_parser.add_argument("--page-count", type=int, help="Page count (optional)")
    download_parser.add_argument(
        "-o",
        "--output",
        default="-",
        help="Output destination (if -, output is STDOUT)",
    )

    uw_auth_group = download_parser.add_argument_group(
        title="UW authentication",
        description="Authenticate yourself through a han.buw.uw.edu.pl account (optional)",
    )
    uw_auth_group.add_argument("-u", "--username", help="Numer karty bibliotecznej / ELS")
    uw_auth_group.add_argument("-p", "--password", help="Hasło do konta bibliotecznego")

    subparsers.add_parser("query", help="Query book info")

    parser.add_argument("url", help="URL do książki (np. https://han.buw.uw.edu.pl/han/libra/https/libra.ibuk.pl/...)")

    args = parser.parse_args()

    logging_level = logging.WARNING
    if args.verbose:
        logging_level = logging.INFO
    elif args.quiet:
        logging_level = logging.CRITICAL

    logging.basicConfig(level=logging_level)

    ibs = IbukWebSession()

    if args.action == "download":
        if bool(args.username) ^ bool(args.password):
            parser.error("If username is provided, password must also be provided.")

        if args.username:
            ibs.login_uw(args.username, args.password)

        await download_action(args.url, args.page_count, ibs, args.output)
    elif args.action == "query":
        await query_action(args.url, ibs)


def run_main():
    asyncio.run(main())


if __name__ == "__main__":
    run_main()
