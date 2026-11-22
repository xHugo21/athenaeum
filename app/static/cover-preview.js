(function () {
  window.initCoverPreview = function (root, opts) {
    opts = opts || {};
    const img = root.querySelector('img');
    const titleEl = root.querySelector('[data-cover-title]');
    const authorEl = root.querySelector('[data-cover-author]');
    const fixed = root.dataset.coverSrc || '';
    const isbnInput = opts.isbnInput;
    const titleInput = opts.titleInput;
    const authorInput = opts.authorInput;
    let override = '';

    function paint() {
      titleEl.textContent = (titleInput?.value || '').trim() || 'Untitled';
      authorEl.textContent = (authorInput?.value || '').trim();
      const isbn = (isbnInput?.value || '').replace(/[^0-9Xx]/g, '');
      const hasIsbn = isbn.length === 10 || isbn.length === 13;
      const src = fixed || override || (hasIsbn
        ? `https://covers.openlibrary.org/b/isbn/${isbn}-M.jpg?default=false`
        : '');
      if (!src) {
        img.hidden = true;
        img.removeAttribute('src');
      } else if (img.getAttribute('src') !== src) {
        img.hidden = false;
        img.src = src;
      }
    }

    img.addEventListener('error', () => { img.hidden = true; });
    img.addEventListener('load', () => { img.hidden = false; });
    isbnInput?.addEventListener('input', () => { override = ''; paint(); });
    titleInput?.addEventListener('input', paint);
    authorInput?.addEventListener('input', paint);
    paint();
    return {
      refresh: paint,
      setCover(url) {
        override = url || '';
        paint();
      },
    };
  };
})();
