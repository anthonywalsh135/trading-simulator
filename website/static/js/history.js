/* draws the net worth history on the account page.
 *
 * the numbers themselves are worked out on the server, where pandas lines the
 * trade ledger up against the daily closing prices. this file only asks for
 * them and draws them.
 */

(function () {
  'use strict';

  var host = document.getElementById('history-chart');
  if (!host) return;

  function themeColour(name, fallback) {
    var value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
  }

  fetch('/api/performance', { headers: { 'Accept': 'application/json' } })
    .then(function (response) { return response.json(); })
    .then(function (data) {
      if (!data.ok || !data.dates.length) {
        host.classList.add('hidden');  //nothing to draw yet
        document.getElementById('history-empty').classList.remove('hidden');
        return;
      }

      var chart = LightweightCharts.createChart(host, {
        layout: { background: { color: 'transparent' }, textColor: themeColour('--text-muted', '#888'), fontSize: 11 },
        grid: { vertLines: { color: themeColour('--border', '#eee') }, horzLines: { color: themeColour('--border', '#eee') } },
        rightPriceScale: { borderColor: themeColour('--border', '#eee') },
        timeScale: { borderColor: themeColour('--border', '#eee') },
        crosshair: { mode: 1 },
        height: host.clientHeight || 260,
        autoSize: true
      });

      var netWorth = chart.addAreaSeries({
        lineColor: themeColour('--accent', '#3b82f6'),
        topColor: themeColour('--accent-soft', 'rgba(59,130,246,.25)'),
        bottomColor: 'transparent',
        lineWidth: 2
      });
      netWorth.setData(data.dates.map(function (day, i) {
        return { time: day, value: data.net_worth[i] };
      }));

      //cash is drawn underneath so the split between money held and money
      //invested is visible, not just the total.
      var cash = chart.addLineSeries({
        color: themeColour('--text-muted', '#888'),
        lineWidth: 1,
        lineStyle: 2,
        priceLineVisible: false,
        lastValueVisible: false
      });
      cash.setData(data.dates.map(function (day, i) {
        return { time: day, value: data.cash[i] };
      }));

      chart.timeScale().fitContent();
    })
    .catch(function () {
      host.classList.add('hidden');
      document.getElementById('history-empty').classList.remove('hidden');
    });
})();
