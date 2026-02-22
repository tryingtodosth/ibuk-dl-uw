
# IBUK Downloader (UW HAN Edition)

This script allows you to download books from the libra.ibuk.pl website and query book information from a given URL. 
This speciﬁc fork/version has been adapted to work with the **University of Warsaw (UW) HAN authentication system**.

## Features

- Download books directly from libra.ibuk.pl.
- **Auto-PDF Generation:** Automatically converts the downloaded book into a perfectly formatted PDF with proper page breaks (if the output file ends with `.pdf`).
- Query book information, including author, title, description, publisher, ISBN, pages, and index.
- Bypass HAN WAF and Load Balancers.
- Support for **BUW (Biblioteka Uniwersytecka w Warszawie)** authentication to access restricted content.

## Installation

1. Clone the repository:
```shell
git clone https://github.com/tryingtodosth/ibuk-dl-uw
cd ibuk-dl-uw

```

2. Install required Python packages:

```shell
pip install weasyprint beautifulsoup4 requests websockets

```

> **Note for Windows users:** > The `weasyprint` library (used for PDF generation) requires additional non-Python dependencies (GTK3/Pango) to work on Windows. If you encounter an `OSError` during PDF export, please follow the official [WeasyPrint Windows Installation Guide](https://www.google.com/search?q=https://weasyprint.readthedocs.io/en/latest/install.html%23windows). Linux and macOS usually handle it out of the box or via a simple package manager install.

## Usage

### 1. Find your book

Go to: https://han.buw.uw.edu.pl/han/libra/https/libra.ibuk.pl/ksiazki
The website will ask you for your BUW credentials (the ones this script needs to work).
*Note: You DON'T need a dedicated PWN/IBUK account.*

### 2. Download the book

To download a book and save it directly as a PDF, use the following command (assuming you run it as a module):

```shell
python -m ibuk_dl.main -v download -o "BOOK.pdf" -u BUW_LOGIN -p "BUW_PASSWORD" "https://han.buw.uw.edu.pl/han/libra/https/libra.ibuk.pl/reader/wspolczesne-wyzwania-prawa-wlasnosci-intelektualnej-jan-olszewski-elzbieta-206614"

```

> **⚠️ IMPORTANT: BUW Credentials**
> Your `BUW_LOGIN` is usually your Electronic Student ID (ELS) number or BUW Library Card number. **It is NOT your PESEL number!**

### Options

* You can specify the page count with the `--page-count` option (e.g., `--page-count 40`). If not specified, the script will download the entire book.
* Use the `-o` or `--output` option to specify the filename. **If you use a `.pdf` extension, the script will automatically render a PDF file.** If you use `.html` or `-` (stdout), it will output raw HTML.
* Add `-v` (verbose) to print detailed progress information to the console.

### Query Book Information

To just fetch metadata about a book without downloading it:

```shell
python -m ibuk_dl.main query "https://han.buw.uw.edu.pl/han/libra/https/libra.ibuk.pl/reader/wspolczesne-wyzwania-prawa-wlasnosci-intelektualnej-jan-olszewski-elzbieta-206614"

```

## Disclaimer

As stated in the license, I am not responsible for damage caused by the use of this program. Please respect the terms of use of the libra.ibuk.pl website and any copyright or licensing agreements for the downloaded content. Downloading and/or sharing copyrighted content may be considered illegal in your country.

