# Third-party notices

File pihak ketiga yang ikut di-commit di repo ini (dependency Python lain dipasang dari PyPI lewat `uv.lock`
dan tetap memakai lisensinya masing-masing).

Installer dan paket rilis agent membawa Python dan dependency-nya dalam bentuk biner; teks lisensi semuanya ikut di
`THIRD_PARTY_LICENSES.txt` di tiap folder program, dibuat saat build oleh `agent/packaging/licenses.py`.

| File | Proyek | Versi | Lisensi |
|---|---|---|---|
| `server/app/static/js/vendor/htmx.min.js` | [htmx](https://github.com/bigskysoftware/htmx) | 2.0.4 | Zero-Clause BSD (0BSD) |
| `server/app/static/js/vendor/alpine-csp.min.js` | [Alpine.js](https://github.com/alpinejs/alpine) (build CSP `@alpinejs/csp`) — Copyright © 2019-2021 Caleb Porzio and contributors | 3.14.8 | MIT |

Versi dan checksum SHA-256 di-pin di `server/scripts/fetch_vendor.sh`.

## MIT License (Alpine.js)

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
documentation files (the "Software"), to deal in the Software without restriction, including without limitation the
rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit
persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the
Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
