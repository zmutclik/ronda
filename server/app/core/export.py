"""Export ke spreadsheet (CSV/XLSX). Sebagian isi berasal dari agent (hostname, merek, model): teks yang diawali
= + - @ dibaca Excel/LibreOffice sebagai formula (CSV/formula injection), mis. =HYPERLINK(...) yang membocorkan isi
sel lain saat diklik."""

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value):
    """Teks yang bisa dibaca sebagai formula diawali apostrof (tampil sebagai teks di Excel); nilai lain apa adanya."""
    if isinstance(value, str) and value.startswith(FORMULA_PREFIXES):
        return "'" + value
    return value
