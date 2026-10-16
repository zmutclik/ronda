// Build CSP Alpine: atribut hanya mereferensikan properti/method di sini, tanpa ekspresi JS inline.
// Pilihan massal (checkbox) di daftar Komputer dan Alert; dipasang di elemen pembungkus tabel.
document.addEventListener('alpine:init', () => {
  Alpine.data('selection', () => ({
    count: 0,
    get has() { return this.count > 0; },
    get label() { return `${this.count} dipilih`; },
    update() {
      this.count = this.$root.querySelectorAll('input[data-select]:checked').length;
    },
    toggleAll(event) {
      const table = event.target.closest('table');
      table.querySelectorAll('tbody input[data-select]').forEach((box) => { box.checked = event.target.checked; });
      this.update();
    },
  }));
});
