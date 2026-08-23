/* the trading page: the search box, the chart and the trade panel.
 *
 * trades are sent over the json api and the page updates itself, rather than
 * posting a form and reloading everything, which loses the chart and the
 * scroll position on every action.
 *
 * two rules hold this page together.
 *
 * first, everything on screen is priced at what a trade would actually fill
 * at, not at the live market price. on a past simulation date those are two
 * different numbers, and sizing a trade from the live price while the engine
 * charges the historical one makes the percentage buttons ask for the wrong
 * quantity and the estimated cost wrong by the same factor.
 *
 * second, the chosen asset survives a page reload. the fast forward ends in a
 * reload, and without this the symbol, the chart and the figures underneath it
 * all disappear the moment the user presses the button.
 */

/* global PriceChart, SymbolSearch */

function initTradePage() {
  'use strict';

  var config = window.TRADE_CONFIG;
  var chart = null;
  var current = null;  //{ symbol, name }
  var livePrice = null;  //the real market price right now
  var execPrice = null;  //what THIS account would trade at
  var execAsOf = config.simDate;
  var isLive = config.isToday;
  var balance = config.balance;
  var submitting = false;  //a trade is in flight; leave the buttons alone

  var $ = function (id) { return document.getElementById(id); };

  //remembering the choice per market keeps the stocks and crypto pages
  //separate, and per tab so two open tabs do not fight over it.
  var MEMORY_KEY = 'tradingsim:symbol:' + config.kind;

  function remember(result) {
    try {
      sessionStorage.setItem(MEMORY_KEY, JSON.stringify({
        symbol: result.symbol, name: result.name || ''
      }));
    } catch (e) { /* private browsing. the page still works, it just forgets */ }
  }

  function recall() {
    //a symbol in the address bar wins, so a link to one asset opens on it
    var asked = new URLSearchParams(window.location.search).get('symbol');
    if (asked) return { symbol: asked.toUpperCase(), name: '' };
    try {
      return JSON.parse(sessionStorage.getItem(MEMORY_KEY) || 'null');
    } catch (e) { return null; }
  }

  function money(value, dp) {
    if (value === null || value === undefined || isNaN(value)) return '—';
    var places = dp === undefined ? 2 : dp;
    return '$' + Number(value).toLocaleString(undefined, {
      minimumFractionDigits: places, maximumFractionDigits: places
    });
  }

  //anything worth less than a pound needs more than two decimal places
  function price(value) {
    if (value === null || value === undefined) return '—';
    return money(value, Math.abs(value) < 1 ? 6 : 2);
  }

  function post(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': window.CSRF_TOKEN },
      body: JSON.stringify(body || {})
    }).then(function (response) { return response.json(); });
  }

  function toast(message, kind) {
    var box = document.getElementById('flashes');
    var el = document.createElement('div');
    el.className = 'flash flash-' + (kind || 'info');
    el.innerHTML = '<span></span><button onclick="this.parentElement.remove()">&times;</button>';
    el.querySelector('span').textContent = message;
    box.appendChild(el);
    setTimeout(function () { el.remove(); }, 6000);
  }

  /* the chart */
  chart = new PriceChart($('chart'), {
    kind: config.kind,
    onQuote: function (quote, execution) {
      livePrice = quote.price;
      $('c-price').textContent = price(quote.price);

      var delta = $('c-delta');
      var sign = quote.change >= 0 ? '+' : '';
      delta.textContent = sign + quote.change.toFixed(2) + ' (' + sign + quote.change_percent.toFixed(2) + '%)';
      delta.className = 'delta ' + (quote.change >= 0 ? 'up' : 'down');

      var state = $('c-state');
      var open = quote.market_state === 'REGULAR';
      state.textContent = open ? 'Market open' : (quote.market_state === 'PRE' ? 'Pre-market' : 'Market closed');
      state.className = 'badge ' + (open ? 'badge-open' : 'badge-closed');

      $('c-stale').classList.toggle('hidden', !quote.stale);

      if (execution) applyExecution(execution);
      updateEstimate();
    }
  });

  /* the price this account trades at, and the date it belongs to. once the
   * simulation has caught up this is just the live price. before then it is
   * that date's close, and the panel says so rather than quietly showing a
   * number the trade is not going to use. */
  function applyExecution(execution) {
    execPrice = execution.price;
    execAsOf = execution.as_of;
    isLive = execution.live !== false;

    var row = $('c-exec');
    if (row) {
      row.classList.toggle('hidden', isLive);
      if (!isLive) {
        $('c-exec-date').textContent = execAsOf;
        $('c-exec-price').textContent = execPrice === null || execPrice === undefined
          ? 'not traded' : price(execPrice);
      }
    }

    var label = $('est-label');
    if (label) {
      label.textContent = isLive ? 'Estimated cost' : 'Estimated cost on ' + execAsOf;
    }

    //an asset that had not listed yet on the simulation date has no price to
    //trade at, and saying so is better than the buttons failing on submit.
    //submitting is checked because this also runs on every price update,
    //which would otherwise re-enable the buttons under a trade in progress.
    var tradeable = execPrice !== null && execPrice !== undefined;
    if (!submitting) {
      ['btn-buy', 'btn-sell'].forEach(function (id) { $(id).disabled = !tradeable; });
    }
    var warning = $('no-price');
    if (warning) {
      warning.classList.toggle('hidden', tradeable);
      warning.textContent = tradeable ? '' :
        (current ? current.symbol : 'That asset') + ' was not trading on ' + execAsOf +
        '. Move your simulation date forward to trade it.';
    }
  }

  /* on a past simulation date nothing is polled for, so the price is asked
   * for once per symbol. it cannot change until the date does. */
  function loadPricing(symbol) {
    return fetch('/api/pricing/' + encodeURIComponent(symbol), {
      headers: { 'Accept': 'application/json' }
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.ok) return;
        applyExecution(data);
        if (!isLive) {
          //no live price is coming, so the big price is the simulated one.
          $('c-price').textContent = price(data.price);
          $('c-delta').textContent = '';
          var state = $('c-state');
          state.textContent = 'Simulated · ' + data.as_of;
          state.className = 'badge badge-sim';
        }
        updateEstimate();
      })
      .catch(function () { /* the estimate just stays blank */ });
  }

  function loadSymbol(result) {
    current = result;
    remember(result);
    //put the symbol in the address bar so the page can be linked to or
    //refreshed without losing it. replaceState rather than pushState, so the
    //back button still leaves the page instead of stepping through symbols.
    if (window.history && window.history.replaceState) {
      window.history.replaceState({}, '', window.location.pathname + '?symbol=' +
                                  encodeURIComponent(result.symbol));
    }
    $('symbol-input').value = result.symbol;
    $('c-symbol').textContent = result.symbol;
    $('c-name').textContent = result.name || '';
    $('chart-panel').classList.remove('hidden');
    $('chart-empty').classList.add('hidden');
    $('trade-form').classList.remove('hidden');
    $('trade-disabled').classList.add('hidden');
    if ($('btn-ff')) $('btn-ff').disabled = false;

    var active = document.querySelector('#interval-picker button.active') ||
                 document.querySelector('#interval-picker button');
    loadPricing(result.symbol);
    return chart.load(result.symbol, active.dataset.interval, active.dataset.range)
      .then(function (data) { renderStats(data.stats); noteInterval(data); })
      .catch(function (error) {
        renderStats(null);
        toast(error.message || 'Could not load that asset.', 'error');
      });
  }

  function renderStats(stats) {
    var host = $('c-stats');
    if (!stats || stats.bars === undefined) {
      host.innerHTML = '<p class="muted small">No market statistics for this period.</p>';
      return;
    }
    var items = [
      ['Open', price(stats.open)], ['High', price(stats.high)],
      ['Low', price(stats.low)], ['Average', price(stats.mean)],
      ['Volatility', Number(stats.volatility).toFixed(2)],
      ['Bars', stats.bars]
    ];
    host.innerHTML = items.map(function (pair) {
      return '<div class="chart-stat"><div class="label">' + pair[0] +
             '</div><div class="value">' + pair[1] + '</div></div>';
    }).join('');
  }

  /* say so when the server had to draw a larger bar than the button asked
   * for, rather than leaving the buttons looking broken. */
  function noteInterval(data) {
    var note = $('interval-note');
    if (!note) return;
    if (data && data.interval_adjusted) {
      note.textContent = data.requested_interval + ' bars are not kept back to ' +
                         data.as_of + ', so showing ' + data.interval + '.';
    } else {
      note.textContent = '';
    }
  }

  new SymbolSearch($('symbol-input'), { kind: config.kind, onSelect: loadSymbol });

  document.querySelectorAll('#interval-picker button').forEach(function (button) {
    button.addEventListener('click', function () {
      document.querySelectorAll('#interval-picker button').forEach(function (b) { b.classList.remove('active'); });
      button.classList.add('active');
      if (!current) return;
      chart.clearPrediction();
      $('predict-note').textContent = '';
      chart.load(current.symbol, button.dataset.interval, button.dataset.range)
        .then(function (data) { renderStats(data.stats); noteInterval(data); })
        .catch(function (error) {
          //without this the failure goes unhandled and the figures are left
          //describing the period that was on screen before.
          renderStats(null);
          noteInterval(null);
          toast(error.message || 'No data for that interval.', 'error');
        });
    });
  });

  /* the trend projection */
  $('btn-predict').addEventListener('click', function () {
    if (!current) return;
    var active = document.querySelector('#interval-picker button.active');
    fetch('/api/candles/' + encodeURIComponent(current.symbol) +
          '?interval=' + active.dataset.interval + '&range=' + active.dataset.range +
          '&kind=' + config.kind + '&predict=1')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.ok || !data.prediction) {
          $('predict-note').textContent = 'Not enough data to fit a trend.';
          return;
        }
        chart.showPrediction(data.prediction);
        var p = data.prediction;
        $('predict-note').textContent =
          'R² ' + p.r_squared.toFixed(2) + ', ' + p.confidence + ' confidence. ' + (p.warning || '');
      })
      .catch(function () { $('predict-note').textContent = 'Could not fit a trend.'; });
  });

  /* the trade panel */
  function updateEstimate() {
    var qty = parseFloat($('qty').value) || 0;
    if (!execPrice || !qty) {
      $('est-cost').textContent = '$0.00';
      $('est-after').textContent = '—';
      return;
    }
    var cost = qty * execPrice;
    $('est-cost').textContent = money(cost);
    $('est-after').textContent = money(balance - cost);
    $('est-after').className = 'num ' + (balance - cost < 0 ? 'down' : '');
  }

  $('qty').addEventListener('input', updateEstimate);

  document.querySelectorAll('.pct-btn').forEach(function (button) {
    button.addEventListener('click', function () {
      //worked out from the fill price, so 100% really is the whole balance
      if (!execPrice) { toast('Still loading the price for this asset.', 'info'); return; }
      var affordable = (balance * (parseInt(button.dataset.pct, 10) / 100)) / execPrice;
      $('qty').value = config.allowsFractional
        ? affordable.toFixed(8).replace(/0+$/, '').replace(/\.$/, '')
        : Math.floor(affordable);
      updateEstimate();
    });
  });

  function trade(action) {
    if (!current) return;
    var qty = $('qty').value;
    var buttons = [$('btn-buy'), $('btn-sell')];
    submitting = true;
    buttons.forEach(function (b) { b.disabled = true; });

    post('/api/trade', { symbol: current.symbol, action: action, shares: qty })
      .then(function (data) {
        if (!data.ok) { toast(data.error, 'error'); return; }
        toast(data.trade.message, 'success');
        $('qty').value = '';
        applyAccountUpdate(data);
      })
      .catch(function () { toast('That trade could not be completed.', 'error'); })
      .finally(function () {
        submitting = false;
        buttons.forEach(function (b) { b.disabled = false; });
      });
  }

  $('btn-buy').addEventListener('click', function () { trade('buy'); });
  $('btn-sell').addEventListener('click', function () { trade('sell'); });

  $('btn-undo').addEventListener('click', function () {
    post('/api/undo', {}).then(function (data) {
      if (!data.ok) { toast(data.error, 'error'); return; }
      toast(data.trade.message, 'success');
      applyAccountUpdate(data);
    });
  });

  function applyAccountUpdate(data) {
    balance = data.balance;
    document.getElementById('hdr-balance').textContent = money(data.balance);
    document.getElementById('hdr-networth').textContent = money(data.net_worth);
    //sent by the server, valued on the simulation date. working it out as
    //net worth minus balance happens to agree, but only by accident.
    $('pf-total').textContent = money(data.total_value);

    var undo = $('btn-undo');
    var depth = data.undo_depth || 0;
    undo.disabled = !depth;
    undo.textContent = 'Undo last trade' + (depth ? ' (' + depth + ')' : '');

    renderPositions(data.positions);
    updateEstimate();
  }

  function renderPositions(positions) {
    var host = $('positions');
    if (!positions || !positions.length) {
      host.innerHTML = '<p class="muted small">You do not own anything yet.</p>';
      return;
    }
    host.innerHTML =
      '<table class="data"><thead><tr><th>Asset</th><th class="num">Qty</th>' +
      '<th class="num">Value</th><th class="num">P/L</th></tr></thead><tbody>' +
      positions.map(function (p) {
        var pnl = p.pnl === null ? '—' : (p.pnl >= 0 ? '+' : '') + p.pnl.toFixed(2);
        return '<tr><td><strong>' + p.symbol + '</strong></td>' +
          '<td class="num">' + Number(p.shares).toLocaleString(undefined, { maximumFractionDigits: 8 }) + '</td>' +
          '<td class="num">' + (p.value === null ? '—' : money(p.value)) + '</td>' +
          '<td class="num ' + ((p.pnl || 0) >= 0 ? 'up' : 'down') + '">' + pnl + '</td></tr>';
      }).join('') + '</tbody></table>';
  }

  /* the fast forward */
  var ffButton = $('btn-ff');
  if (ffButton) {
    var cancelButton = $('btn-ff-cancel');

    ffButton.addEventListener('click', function () {
      if (!current) { toast('Choose an asset first.', 'error'); return; }

      var speed = parseInt(document.querySelector('#speed-picker button.active').dataset.speed, 10);
      var today = new Date().toISOString().slice(0, 10);

      ffButton.disabled = true;
      cancelButton.classList.remove('hidden');
      $('ff-panel').classList.remove('hidden');
      document.querySelectorAll('#btn-buy, #btn-sell').forEach(function (b) { b.disabled = true; });

      chart.fastForward(config.simDate, today, speed, {
        onProgress: function (progress) {
          $('ff-fill').style.width = progress.percent + '%';
          $('ff-date').textContent = progress.date;
        }
      }).then(function (outcome) {
        if (outcome.cancelled) {
          toast('Fast-forward stopped.', 'info');
          //the replay overwrote the chart, so put back the one for the date
          //the account is still on, figures included.
          return chart.reload().then(function (data) {
            if (data) { renderStats(data.stats); noteInterval(data); }
            return null;
          });
        }
        if (outcome.skipped) { toast('Already up to date.', 'info'); return null; }
        //save the new simulation date and run the bot over the interval
        return post('/api/fast-forward', { to: today });
      }).then(function (data) {
        if (!data) return;
        if (!data.ok) { toast(data.error, 'error'); return; }
        var message = 'Advanced ' + data.days_advanced + ' days to ' + data.to + '.';
        if (data.bot && data.bot.trades) {
          message += ' Your bot made ' + data.bot.trades + ' trade(s).';
        }
        toast(message, 'success');
        applyAccountUpdate(data);
        //the simulation date has moved, so reload to pick the new one up
        //everywhere. the chosen asset is remembered, so the chart and its
        //figures come back rather than the panel emptying itself.
        setTimeout(function () { window.location.reload(); }, 1800);
      }).catch(function (error) {
        toast(error.message || 'Fast-forward failed.', 'error');
      }).finally(function () {
        ffButton.disabled = false;
        cancelButton.classList.add('hidden');
        document.querySelectorAll('#btn-buy, #btn-sell').forEach(function (b) { b.disabled = false; });
      });
    });

    cancelButton.addEventListener('click', function () { chart.cancelReplay(); });

    document.querySelectorAll('#speed-picker button').forEach(function (button) {
      button.addEventListener('click', function () {
        document.querySelectorAll('#speed-picker button').forEach(function (b) { b.classList.remove('active'); });
        button.classList.add('active');
      });
    });
  }

  /* put the last chosen asset back */
  var previous = recall();
  if (previous && previous.symbol) loadSymbol(previous);
}
