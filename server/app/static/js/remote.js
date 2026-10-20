// Halaman remote (remote/viewer.html): noVNC tersambung ke relay /relay/browser/<sesi>. Jabat tangan RFB
// (keamanan "None") diurus agent; password VNC tidak pernah sampai ke browser. Mode lihat saja ditegakkan di PC
// (password view-only UltraVNC), viewOnly di sini hanya agar input tidak dikirim percuma.
import RFB from './vendor/novnc/core/rfb.js';

const screen = document.getElementById('rv-screen');
const msg = document.getElementById('rv-msg');
const proto = location.protocol === 'https:' ? 'wss' : 'ws';
const rfb = new RFB(screen, `${proto}://${location.host}/relay/browser/${screen.dataset.session}`, { shared: true });
rfb.viewOnly = screen.dataset.viewOnly === '1';
rfb.scaleViewport = true;
rfb.clipViewport = false;
rfb.background = '#0b1120';

const say = (text) => { msg.textContent = text; };
// Status dari server (alasan selesai, galat agent) lebih akurat daripada event noVNC: minta segera
const refreshStatus = () => document.getElementById('rv-status')?.dispatchEvent(new Event('rv-refresh'));

rfb.addEventListener('connect', () => { say(''); refreshStatus(); });
rfb.addEventListener('disconnect', () => { say('Sesi remote selesai. Lihat status di kanan atas.'); refreshStatus(); });
rfb.addEventListener('securityfailure', (e) => say(`Jabat tangan VNC gagal: ${e.detail.reason || e.detail.status}`));

// Sesi diakhiri (tombol Akhiri, batas durasi, atau galat agent): putus juga di sisi browser
document.addEventListener('htmx:afterSwap', (evt) => {
  if (evt.detail.target?.id === 'rv-status' || evt.detail.elt?.id === 'rv-status') {
    if (document.getElementById('rv-status')?.dataset.state === 'ended') rfb.disconnect();
  }
});

document.addEventListener('click', (evt) => {
  const el = evt.target.closest('[data-rv]');
  if (!el) return;
  if (el.dataset.rv === 'cad') rfb.sendCtrlAltDel();
  if (el.dataset.rv === 'fullscreen') screen.requestFullscreen?.();
});
