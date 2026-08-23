/* the search box that suggests companies as you type. typing "apple" offers
 * Apple Inc. as AAPL, so a mistyped symbol is caught here rather than after
 * the form has been sent.
 *
 * requests wait 150ms after the last key, and the request already in flight is
 * cancelled, so typing quickly does not leave a queue of old answers arriving
 * in the wrong order. the arrow keys, Enter and Escape all work, and the
 * chosen result is handed back through the onSelect callback.
 */

(function () {
  'use strict';

  function escapeHtml(text) {
    var div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  //mark the part of a suggestion that was actually typed
  function highlight(text, query) {
    var safe = escapeHtml(text);
    if (!query) return safe;
    var index = safe.toLowerCase().indexOf(query.toLowerCase());
    if (index === -1) return safe;
    return safe.slice(0, index) +
      '<mark>' + safe.slice(index, index + query.length) + '</mark>' +
      safe.slice(index + query.length);
  }

  var TYPE_LABEL = {
    EQUITY: 'Stock', ETF: 'ETF', CRYPTOCURRENCY: 'Crypto',
    INDEX: 'Index', MUTUALFUND: 'Fund', FUTURE: 'Future'
  };

  function SymbolSearch(input, options) {
    options = options || {};
    this.input = input;
    this.kind = options.kind || 'all';
    this.onSelect = options.onSelect || function () {};
    this.minChars = options.minChars || 1;

    this.results = [];
    this.activeIndex = -1;
    this.controller = null;
    this.timer = null;

    this.wrap = document.createElement('div');
    this.wrap.className = 'search-wrap';
    input.parentNode.insertBefore(this.wrap, input);
    this.wrap.appendChild(input);

    this.panel = document.createElement('div');
    this.panel.className = 'search-results hidden';
    this.panel.setAttribute('role', 'listbox');
    this.wrap.appendChild(this.panel);

    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-expanded', 'false');
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('autocomplete', 'off');

    input.addEventListener('input', this.onInput.bind(this));
    input.addEventListener('keydown', this.onKeyDown.bind(this));
    input.addEventListener('focus', this.onInput.bind(this));
    document.addEventListener('click', function (event) {
      if (!this.wrap.contains(event.target)) this.close();
    }.bind(this));
  }

  SymbolSearch.prototype.onInput = function () {
    var query = this.input.value.trim();
    clearTimeout(this.timer);
    if (query.length < this.minChars) { this.close(); return; }
    this.timer = setTimeout(this.fetch.bind(this, query), 150);
  };

  SymbolSearch.prototype.fetch = function (query) {
    //cancel the last request so answers cannot arrive in the wrong order
    if (this.controller) this.controller.abort();
    this.controller = new AbortController();

    this.panel.innerHTML = '<div class="search-status"><span class="spinner"></span></div>';
    this.panel.classList.remove('hidden');

    var url = '/api/search?q=' + encodeURIComponent(query) +
              '&kind=' + encodeURIComponent(this.kind);

    fetch(url, { signal: this.controller.signal, headers: { 'Accept': 'application/json' } })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        this.results = (data && data.results) || [];
        this.activeIndex = -1;
        this.render(query);
      }.bind(this))
      .catch(function (error) {
        if (error.name === 'AbortError') return;
        this.panel.innerHTML = '<div class="search-status">Search is unavailable right now.</div>';
      }.bind(this));
  };

  SymbolSearch.prototype.render = function (query) {
    if (!this.results.length) {
      this.panel.innerHTML = '<div class="search-status">No matches for &ldquo;' +
        escapeHtml(query) + '&rdquo;</div>';
      this.panel.classList.remove('hidden');
      return;
    }

    this.panel.innerHTML = this.results.map(function (result, index) {
      return '<div class="search-item" role="option" data-index="' + index + '">' +
        '<span class="sym">' + highlight(result.symbol, query) + '</span>' +
        '<span class="name">' + highlight(result.name, query) + '</span>' +
        '<span class="tag">' + (TYPE_LABEL[result.asset_type] || result.asset_type) + '</span>' +
        '</div>';
    }).join('');

    Array.prototype.forEach.call(this.panel.querySelectorAll('.search-item'), function (item) {
      item.addEventListener('click', function () {
        this.choose(parseInt(item.dataset.index, 10));
      }.bind(this));
      item.addEventListener('mouseenter', function () {
        this.setActive(parseInt(item.dataset.index, 10));
      }.bind(this));
    }.bind(this));

    this.panel.classList.remove('hidden');
    this.input.setAttribute('aria-expanded', 'true');
  };

  SymbolSearch.prototype.setActive = function (index) {
    this.activeIndex = index;
    Array.prototype.forEach.call(this.panel.querySelectorAll('.search-item'), function (item, i) {
      item.classList.toggle('active', i === index);
    });
  };

  SymbolSearch.prototype.onKeyDown = function (event) {
    if (this.panel.classList.contains('hidden')) return;

    if (event.key === 'ArrowDown') {
      event.preventDefault();
      this.setActive(Math.min(this.activeIndex + 1, this.results.length - 1));
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      this.setActive(Math.max(this.activeIndex - 1, 0));
    } else if (event.key === 'Enter') {
      if (this.activeIndex >= 0) {
        event.preventDefault();
        this.choose(this.activeIndex);
      }
    } else if (event.key === 'Escape') {
      this.close();
    }
  };

  SymbolSearch.prototype.choose = function (index) {
    var result = this.results[index];
    if (!result) return;
    this.input.value = result.symbol;
    this.close();
    this.onSelect(result);
  };

  SymbolSearch.prototype.close = function () {
    this.panel.classList.add('hidden');
    this.input.setAttribute('aria-expanded', 'false');
    this.activeIndex = -1;
  };

  window.SymbolSearch = SymbolSearch;
})();
