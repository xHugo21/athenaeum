(function () {
  const PER_PAGE = 5;

  function row(b, onUse) {
    const el = document.createElement('div');
    el.className = 'isbn-result';
    if (b.cover) {
      const img = document.createElement('img');
      img.className = 'cover';
      img.alt = '';
      img.loading = 'lazy';
      img.src = b.cover;
      img.addEventListener('error', () => img.remove());
      el.append(img);
    }
    const meta = document.createElement('div');
    meta.className = 'meta';
    const title = document.createElement('div');
    title.className = 't';
    title.textContent = b.title;
    const sub = document.createElement('div');
    sub.className = 's';
    sub.textContent = [b.author, b.publisher, b.year, b.pages ? `${b.pages} pages` : '', b.isbn].filter(Boolean).join(' · ');
    meta.append(title, sub);
    const use = document.createElement('button');
    use.className = 'btn sm';
    use.type = 'button';
    use.textContent = 'Use';
    use.addEventListener('click', () => onUse(b));
    el.append(meta, use);
    return el;
  }

  window.initIsbnSearch = function (root, onUse) {
    const form = root.querySelector('[data-isbn-form]');
    const q = root.querySelector('[data-isbn-q]');
    const lang = root.querySelector('[data-isbn-lang]');
    const source = root.querySelector('[data-isbn-source]');
    const spinner = root.querySelector('[data-isbn-spinner]');
    const results = root.querySelector('[data-isbn-results]');
    const pager = root.querySelector('[data-isbn-pager]');
    const prev = root.querySelector('[data-isbn-prev]');
    const next = root.querySelector('[data-isbn-next]');
    const pagenum = root.querySelector('[data-isbn-pagenum]');
    let items = [];
    let page = 0;

    function render() {
      const pages = Math.max(1, Math.ceil(items.length / PER_PAGE));
      results.replaceChildren(...items.slice(page * PER_PAGE, (page + 1) * PER_PAGE).map(b => row(b, onUse)));
      pager.hidden = pages < 2;
      prev.disabled = page === 0;
      next.disabled = page >= pages - 1;
      pagenum.textContent = `${page + 1} / ${pages}`;
    }

    prev.addEventListener('click', () => { page--; render(); });
    next.addEventListener('click', () => { page++; render(); });

    form.addEventListener('submit', async e => {
      e.preventDefault();
      const query = q.value.trim();
      if (!query) return;
      results.replaceChildren();
      spinner.hidden = false;
      try {
        const r = await fetch(`/api/search?q=${encodeURIComponent(query)}&lang=${encodeURIComponent(lang.value)}&source=${encodeURIComponent(source.value)}`);
        spinner.hidden = true;
        items = await r.json();
        page = 0;
        if (!items.length) {
          pager.hidden = true;
          results.innerHTML = '<p class="muted" style="font-size:13px;margin:12px 0 0">No results. Try fewer words or another language.</p>';
          return;
        }
        render();
      } catch {
        spinner.hidden = true;
        pager.hidden = true;
        results.innerHTML = '<p class="muted" style="font-size:13px;margin:12px 0 0">Search failed. Try again.</p>';
      }
    });
  };
})();
