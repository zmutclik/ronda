// Dimuat dari file (bukan <script> inline) agar lolos CSP script-src 'self'.
// `document`, bukan `document.body`: script di <head> berjalan sebelum <body> ada.
document.addEventListener('htmx:beforeSwap', (evt) => {
  const xhr = evt.detail.xhr;
  // 400/422: form dirender ulang dengan pesan error.
  // HX-Retarget: server sengaja mengirim fragment error (toast) untuk status lain (403/404/429/5xx).
  if (xhr.status === 400 || xhr.status === 422 || xhr.getResponseHeader('HX-Retarget')) {
    evt.detail.shouldSwap = true;
    evt.detail.isError = false;
  }
});

// Pesan singkat dari server lewat header HX-Trigger: {"toast": "teks"}. textContent, bukan innerHTML.
function showToast(message, isError) {
  const box = document.getElementById('toast');
  if (!box || !message) return;
  const p = document.createElement('p');
  p.className = isError ? 'toast err' : 'toast';
  p.setAttribute('role', 'status');
  p.textContent = message;
  box.appendChild(p);
  setTimeout(() => p.remove(), 5000);
}
document.addEventListener('toast', (evt) => showToast(evt.detail.value));

// Toast error dari server (components/_toast.html) juga hilang sendiri
document.addEventListener('htmx:afterSwap', (evt) => {
  if (evt.detail.target && evt.detail.target.id === 'toast') {
    const items = evt.detail.target.querySelectorAll('.toast');
    items.forEach((el) => setTimeout(() => el.remove(), 6000));
  }
});

// Elemen dengan data-confirm: minta konfirmasi sebelum submit (pengganti hx-confirm untuk form biasa)
document.addEventListener('submit', (evt) => {
  const msg = evt.target.getAttribute('data-confirm');
  if (msg && !window.confirm(msg)) evt.preventDefault();
}, true);

// Tombol cetak (laporan PDF lewat dialog cetak browser) & tombol tutup <dialog>
document.addEventListener('click', (evt) => {
  const el = evt.target.closest('[data-action]');
  if (!el) return;
  const action = el.getAttribute('data-action');
  if (action === 'print') window.print();
  if (action === 'open-dialog') document.getElementById(el.getAttribute('data-target'))?.showModal();
  if (action === 'close-dialog') el.closest('dialog')?.close();
  if (action === 'copy') {
    const text = document.getElementById(el.getAttribute('data-target'))?.textContent || '';
    copyText(text.trim()).then(() => showToast('Disalin.'), () => showToast('Gagal menyalin; salin manual.', true));
  }
});

// navigator.clipboard hanya ada di HTTPS/localhost; di HTTP pakai execCommand lewat textarea sementara.
function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  return new Promise((resolve, reject) => {
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.className = 'offscreen';
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand('copy');
    area.remove();
    ok ? resolve() : reject(new Error('copy gagal'));
  });
}

// Server minta browser menyalin teks (tombol Salin key sekali pakai): HX-Trigger {"copy-text": "..."}.
// Kalau gagal (aktivasi pengguna kedaluwarsa), perintah tetap tampil di kotaknya untuk disalin manual.
document.addEventListener('copy-text', (evt) => {
  copyText(evt.detail.value).then(
    () => showToast('Perintah disalin. Key sekali pakai: 1 PC, berlaku 2 jam.'),
    () => showToast('Gagal menyalin otomatis; salin perintah di kotak.', true));
});

// Fragment yang meminta dialog ditutup setelah sukses: HX-Trigger {"close-dialog": true}
document.addEventListener('close-dialog', () => document.querySelectorAll('dialog[open]').forEach((d) => d.close()));
