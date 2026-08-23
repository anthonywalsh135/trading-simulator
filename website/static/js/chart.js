/* the price chart, and the animation that plays the market forwards.
 *
 * the chart is built once and then fed small pieces of json, rather than being
 * drawn on the server and sent as a picture that can only change by reloading
 * the whole page:
 *   - the bars come from /api/candles, ending on the account's simulation date
 *   - the live price comes from /api/quote about once a second. the server
 *     answers that from a shared cache, so it does not become one request to
 *     yahoo per second per open tab
 *   - the fast forward pulls a range of history from /api/replay and animates
 *     through it
 *
 * the price is only asked for while the simulation is up to date. on a past
 * date there is nothing live to show, and writing today's price onto the last
 * bar would leave a chart from 2016 ending on a candle from this year.
 */

(function () {
  'use strict';

  var POLL_MS = 1000;

  function themeColors() {
    var styles = getComputedStyle(document.documentElement);
    return {
      text: styles.getPropertyValue('--text-muted').trim(),
      border: styles.getPropertyValue('--border').trim(),
      up: styles.getPropertyValue('--up').trim(),
      down: styles.getPropertyValue('--down').trim(),
      accent: styles.getPropertyValue('--accent').trim()
    };
  }

  function PriceChart(container, options) {
    this.container = container;
    this.kind = (options && options.kind) || 'stocks';
    this.symbol = null;
    this.interval = '5m';
    this.range = '1d';
    this.pollTimer = null;
    this.replaying = false;
    this.live = true;  //is the simulation date current?
    this.lastLoad = null;  //the most recent /api/candles payload
    this.onQuote = (options && options.onQuote) || function () {};

    var colors = themeColors();
    this.chart = LightweightCharts.createChart(container, {
      layout: { background: { color: 'transparent' }, textColor: colors.text, fontSize: 11 },
      grid: { vertLines: { color: colors.border }, horzLines: { color: colors.border } },
      rightPriceScale: { borderColor: colors.border },
      timeScale: { borderColor: colors.border, timeVisible: true, secondsVisible: false },
      crosshair: { mode: 1 },
      height: container.clientHeight || 380,
      autoSize: true
    });

    this.series = this.chart.addCandlestickSeries({
      upColor: colors.up, downColor: colors.down,
      borderUpColor: colors.up, borderDownColor: colors.down,
      wickUpColor: colors.up, wickDownColor: colors.down
    });

    this.predictionSeries = null;
  }

  PriceChart.prototype.load = function (symbol, interval, range) {
    this.symbol = symbol;
    this.interval = interval || this.interval;
    this.range = range || this.range;
    this.stopPolling();

    var url = '/api/candles/' + encodeURIComponent(symbol) +
      '?interval=' + this.interval + '&range=' + this.range + '&kind=' + this.kind;

    return fetch(url, { headers: { 'Accept': 'application/json' } })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        if (!data.ok) throw new Error(data.error || 'No data');
        this.series.setData(data.candles.map(function (candle) {
          return {
            time: candle.ts, open: candle.open, high: candle.high,
            low: candle.low, close: candle.close
          };
        }));
        this.chart.timeScale().fitContent();
        //the server may have drawn a larger bar than the one asked for,
        //because intraday history does not reach back that far.
        this.interval = data.interval || this.interval;
        this.live = data.live !== false;
        this.lastLoad = data;
        if (this.live) this.startPolling();
        return data;
      }.bind(this));
  };

  /* load the current symbol again at the current interval. used after the
   * simulation date moves, so that the chart and the figures under it describe
   * the date now being traded. */
  PriceChart.prototype.reload = function () {
    if (!this.symbol) return Promise.resolve(null);
    return this.load(this.symbol, this.interval, this.range);
  };

  PriceChart.prototype.showPrediction = function (prediction) {
    if (this.predictionSeries) {
      this.chart.removeSeries(this.predictionSeries);
      this.predictionSeries = null;
    }
    if (!prediction) return;

    var colors = themeColors();
    this.predictionSeries = this.chart.addLineSeries({
      color: colors.accent, lineWidth: 2, lineStyle: 2,
      lastValueVisible: false, priceLineVisible: false
    });
    this.predictionSeries.setData(prediction.timestamps.map(function (ts, index) {
      return { time: ts, value: prediction.prices[index] };
    }));
  };

  PriceChart.prototype.clearPrediction = function () {
    if (this.predictionSeries) {
      this.chart.removeSeries(this.predictionSeries);
      this.predictionSeries = null;
    }
  };

  /* asking for the live price */
  PriceChart.prototype.startPolling = function () {
    this.stopPolling();

    //skip a tab in the background, so a window left open overnight is not
    //asking for a price that nobody is looking at.
    var tick = function () {
      if (this.replaying || !this.live || document.hidden || !this.symbol) return;
      fetch('/api/quote/' + encodeURIComponent(this.symbol), { headers: { 'Accept': 'application/json' } })
        .then(function (response) { return response.json(); })
        .then(function (data) {
          if (!data.ok) return;
          this.applyQuote(data.quote);
          //execution is what this account would actually fill at, which is
          //not the live price whenever the simulation is behind.
          this.onQuote(data.quote, data.execution);
        }.bind(this))
        .catch(function () { /* a dropped request is not worth reporting */ });
    }.bind(this);

    //refresh as soon as the tab is looked at again, otherwise the price on
    //screen is frozen at whatever it was when the user switched away.
    if (this.visibilityHandler) {
      document.removeEventListener('visibilitychange', this.visibilityHandler);
    }
    this.visibilityHandler = function () { if (!document.hidden) tick(); };
    document.addEventListener('visibilitychange', this.visibilityHandler);

    tick();
    this.pollTimer = setInterval(tick, POLL_MS);
  };

  PriceChart.prototype.applyQuote = function (quote) {
    //stretch the most recent bar with the live price, so the chart moves
    //between bars rather than only when a new one closes.
    var data = this.series.data ? this.series.data() : null;
    if (!data || !data.length) return;
    var last = data[data.length - 1];
    this.series.update({
      time: last.time,
      open: last.open,
      high: Math.max(last.high, quote.price),
      low: Math.min(last.low, quote.price),
      close: quote.price
    });
  };

  PriceChart.prototype.stopPolling = function () {
    if (this.pollTimer) { clearInterval(this.pollTimer); this.pollTimer = null; }
  };

  /* fast forward. animates the chart from the simulation date up to the
   * present, then saves the new date on the server. the bot is run over every
   * bar passed, so skipping time makes the trades it would have made. */
  PriceChart.prototype.fastForward = function (fromDate, toDate, speed, callbacks) {
    callbacks = callbacks || {};
    var self = this;
    this.replaying = true;
    this.stopPolling();

    var url = '/api/replay/' + encodeURIComponent(this.symbol) +
      '?from=' + encodeURIComponent(fromDate) + '&to=' + encodeURIComponent(toDate);

    return fetch(url, { headers: { 'Accept': 'application/json' } })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        if (!data.ok) throw new Error(data.error || 'Replay failed');
        if (!data.candles.length) {
          self.replaying = false;
          return { skipped: true };
        }

        return new Promise(function (resolve) {
          var bars = data.candles;
          var index = 0;
          var frameDelay = Math.max(8, 260 / speed);
          self.series.setData([]);

          var step = function () {
            if (!self.replaying) { resolve({ cancelled: true }); return; }

            //draw several bars per frame at high speed, so the animation is
            //smooth rather than limited by how often a timer can fire.
            var batch = Math.max(1, Math.round(speed / 12));
            for (var n = 0; n < batch && index < bars.length; n++, index++) {
              var bar = bars[index];
              self.series.update({
                time: bar.ts, open: bar.open, high: bar.high,
                low: bar.low, close: bar.close
              });
            }

            var current = bars[Math.min(index, bars.length - 1)];
            if (callbacks.onProgress) {
              callbacks.onProgress({
                percent: Math.round((index / bars.length) * 100),
                date: new Date(current.ts * 1000).toISOString().slice(0, 10),
                price: current.close
              });
            }

            if (index < bars.length) {
              setTimeout(step, frameDelay);
            } else {
              self.replaying = false;
              self.chart.timeScale().fitContent();
              resolve({ completed: true, bars: bars.length });
            }
          };
          step();
        });
      });
  };

  PriceChart.prototype.cancelReplay = function () { this.replaying = false; };

  window.PriceChart = PriceChart;
})();
