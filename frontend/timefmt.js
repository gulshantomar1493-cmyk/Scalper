/* MarketScalper — IST (Asia/Kolkata) display formatting.
 *
 * Single source of truth for turning a UTC instant into a user-facing wall
 * clock. Everything INTERNAL stays UTC — the database, the Binance feed, the
 * backend, the scheduler, and the chart's own time model. Times become IST
 * ONLY here, at render time (the owner's rule). The VPS timezone (Canada) is
 * irrelevant: these formatters pin timeZone: "Asia/Kolkata" explicitly, so the
 * browser/OS/server zone never leaks into what the user sees.
 *
 * Pure formatting: no data, no network, no storage, no engine math. Exposed as
 * window.IST and used by app.js (chart axis + crosshair + clocks) and
 * dashboard.js (journal / trade tables).
 */
(function () {
  "use strict";
  var TZ = "Asia/Kolkata";
  function asDate(x) { return (x instanceof Date) ? x : new Date(x); }
  // A UTCTimestamp from Lightweight Charts is seconds; JS Date wants ms.
  function msOf(t) { return (typeof t === "number") ? t * 1000 : asDate(t).getTime(); }

  var fTime = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  var fHM   = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, hour: "2-digit", minute: "2-digit", hour12: false });
  var fDT   = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false });
  var fDay  = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, day: "2-digit", month: "short" });
  var fFull = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });

  var IST = {
    tz: TZ,
    time:     function (x) { return fTime.format(asDate(x)); },            // HH:MM:SS
    hm:       function (x) { return fHM.format(asDate(x)); },              // HH:MM
    dateTime: function (x) { return fDT.format(asDate(x)); },              // DD Mon HH:MM
    full:     function (x) { return fFull.format(asDate(x)) + " IST"; },   // full stamp
    now:      function () { return fTime.format(new Date()) + " IST"; },   // live clock

    // Lightweight Charts axis ticks. time = UTCTimestamp (seconds); tickMarkType
    // 0=Year 1=Month 2=DayOfMonth 3=Time 4=TimeWithSeconds. Underlying time
    // values stay UTC — only the LABEL is IST.
    tick: function (time, tickMarkType) {
      var ms = msOf(time);
      return (tickMarkType >= 3) ? fHM.format(new Date(ms)) : fDay.format(new Date(ms));
    },
    // Lightweight Charts crosshair readout — full IST datetime.
    crosshair: function (time) { return fDT.format(new Date(msOf(time))); },
  };
  window.IST = IST;
})();
