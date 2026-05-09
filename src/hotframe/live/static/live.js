/* SPDX-License-Identifier: Apache-2.0
 * hotframe live.js — WebSocket client + morphdom integration.
 *
 * Loaded once per page. On DOMContentLoaded:
 *   1. Opens a single WebSocket to /ws/_live.
 *   2. Walks the DOM for [data-hf-cid] elements and sends ``attach`` for
 *      each one.
 *   3. Captures clicks/submits/inputs that carry a ``data-on:*`` or
 *      ``data-bind`` attribute, forwards them to the server.
 *   4. Receives ``patch`` envelopes and applies them with morphdom.
 *
 * Hotframe ships morphdom alongside this file. Both must be served
 * from the same /static/hotframe/ mount; live.js exposes the global
 * ``window.hotframeLive`` for debugging and exports an ``hf:ready``
 * CustomEvent on the document.
 *
 * The protocol is defined in hotframe/live/protocol.py. Keep the field
 * names in sync.
 */
(function () {
  "use strict";

  if (window.hotframeLive) {
    // Loaded twice (likely a build mistake). Bail without overwriting
    // the live instance.
    console.warn("[hotframe-live] already initialised; skipping");
    return;
  }

  // ---------------------------------------------------------------
  // Config
  // ---------------------------------------------------------------

  // The runtime mounts the WS at /ws/_live regardless of project. If a
  // page wants to override (test fixtures, alternate auth) it can set
  // window.HOTFRAME_LIVE_URL before this script loads.
  var LIVE_URL = window.HOTFRAME_LIVE_URL ||
    (location.protocol === "https:" ? "wss://" : "ws://") +
      location.host + "/ws/_live";

  var BIND_DEBOUNCE_MS = 250;
  var RECONNECT_BACKOFF = [250, 500, 1000, 2000, 5000, 10000];

  // ---------------------------------------------------------------
  // LiveClient
  // ---------------------------------------------------------------

  function LiveClient(url) {
    this.url = url;
    this.ws = null;
    this.queue = [];          // outbound while disconnected
    this.bindTimers = {};     // ``${cid}:${field}`` -> timeout id
    this.reconnectAttempt = 0;
    this.connect();
  }

  LiveClient.prototype.connect = function () {
    var self = this;
    try {
      this.ws = new WebSocket(this.url);
    } catch (e) {
      console.error("[hotframe-live] WS construct failed", e);
      this._scheduleReconnect();
      return;
    }
    this.ws.onopen = function () {
      self.reconnectAttempt = 0;
      // Re-attach every component currently in the DOM. After a
      // reconnect, the server has no memory of us — they start clean.
      self.attachAll();
      // Drain anything queued while we were offline.
      while (self.queue.length) {
        try {
          self.ws.send(JSON.stringify(self.queue.shift()));
        } catch (e) {
          // If sending fails mid-drain, push back and break.
          self.queue.unshift(self.queue[0]);
          break;
        }
      }
      document.dispatchEvent(new CustomEvent("hf:ws-open"));
    };
    this.ws.onmessage = function (ev) {
      var msg;
      try {
        msg = JSON.parse(ev.data);
      } catch (e) {
        console.warn("[hotframe-live] dropped non-JSON frame", ev.data);
        return;
      }
      self.handle(msg);
    };
    this.ws.onclose = function () {
      self._scheduleReconnect();
    };
    this.ws.onerror = function (e) {
      // ``onclose`` fires after onerror; let it handle reconnect.
      console.warn("[hotframe-live] WS error", e);
    };
  };

  LiveClient.prototype._scheduleReconnect = function () {
    var idx = Math.min(this.reconnectAttempt, RECONNECT_BACKOFF.length - 1);
    var delay = RECONNECT_BACKOFF[idx];
    this.reconnectAttempt += 1;
    var self = this;
    setTimeout(function () { self.connect(); }, delay);
  };

  LiveClient.prototype.send = function (envelope) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      this.queue.push(envelope);
      return;
    }
    try {
      this.ws.send(JSON.stringify(envelope));
    } catch (e) {
      this.queue.push(envelope);
    }
  };

  // ---------------------------------------------------------------
  // Attach / detach
  // ---------------------------------------------------------------

  LiveClient.prototype.attachAll = function () {
    var roots = document.querySelectorAll("[data-hf-cid]");
    for (var i = 0; i < roots.length; i++) {
      this.attach(roots[i]);
    }
  };

  LiveClient.prototype.attach = function (el) {
    var cid = el.getAttribute("data-hf-cid");
    var name = el.getAttribute("data-hf-component");
    var propsJson = el.getAttribute("data-hf-props") || "{}";
    var props;
    try {
      props = JSON.parse(propsJson);
    } catch (e) {
      console.error("[hotframe-live] bad data-hf-props on", el, e);
      props = {};
    }
    this.send({ t: "attach", cid: cid, name: name, props: props });
  };

  LiveClient.prototype.detach = function (cid) {
    this.send({ t: "detach", cid: cid });
  };

  // ---------------------------------------------------------------
  // Inbound handling
  // ---------------------------------------------------------------

  LiveClient.prototype.handle = function (msg) {
    switch (msg.t) {
      case "patch":
        this._applyPatch(msg);
        break;
      case "nav":
        window.location.href = msg.url;
        break;
      case "err":
        console.error("[hotframe-live] server error", msg);
        document.dispatchEvent(new CustomEvent("hf:error", { detail: msg }));
        break;
      case "toast":
        document.dispatchEvent(new CustomEvent("hf:toast", { detail: msg }));
        break;
      default:
        console.warn("[hotframe-live] unknown server msg", msg);
    }
  };

  LiveClient.prototype._applyPatch = function (msg) {
    var root = document.querySelector('[data-hf-cid="' + cssEscape(msg.cid) + '"]');
    if (!root) {
      // Component was removed from the DOM but server didn't know yet.
      // Tell it to detach so memory is freed.
      this.detach(msg.cid);
      return;
    }
    if (typeof window.morphdom !== "function") {
      // No morphdom — fall back to innerHTML. Loses focus, but works.
      root.innerHTML = msg.html;
      return;
    }
    // Wrap the new HTML so morphdom sees a single root with the same
    // tag as ``root``. The server sends only the inner HTML; we keep
    // the envelope (data-hf-cid, etc.) intact by morphing children.
    var tmp = document.createElement(root.tagName);
    tmp.innerHTML = msg.html;
    window.morphdom(root, tmp, {
      childrenOnly: true,
      onBeforeElUpdated: function (fromEl, toEl) {
        // Preserve focus and selection on inputs that the user is
        // currently interacting with. Without this, morphdom blurs the
        // active element on every patch.
        if (fromEl === document.activeElement && fromEl.tagName === "INPUT") {
          return false; // skip update of the focused field
        }
        return true;
      },
    });
  };

  // CSS.escape polyfill for selector building (Edge legacy).
  function cssEscape(s) {
    if (typeof CSS !== "undefined" && typeof CSS.escape === "function") {
      return CSS.escape(s);
    }
    return String(s).replace(/[^a-zA-Z0-9_-]/g, function (m) {
      return "\\" + m;
    });
  }

  // ---------------------------------------------------------------
  // Outbound DOM -> WS bridge
  // ---------------------------------------------------------------

  function findCid(el) {
    var node = el;
    while (node && node !== document.body) {
      if (node.hasAttribute && node.hasAttribute("data-hf-cid")) {
        return node.getAttribute("data-hf-cid");
      }
      node = node.parentNode;
    }
    return null;
  }

  function parseEventValue(raw) {
    // ``"name"`` or ``"name:payload"``. The payload, if present, is
    // passed through as a string; the server side coerces / validates
    // via Pydantic.
    var idx = raw.indexOf(":");
    if (idx === -1) {
      return { name: raw, payload: null };
    }
    return { name: raw.slice(0, idx), payload: raw.slice(idx + 1) };
  }

  LiveClient.prototype.bindEvents = function () {
    var self = this;

    document.addEventListener("click", function (e) {
      var target = e.target.closest("[data-on\\:click]");
      if (!target) return;
      var cid = findCid(target);
      if (!cid) return;
      // Don't preempt anchors with an href; let them navigate normally.
      // Buttons inside forms with type="submit" stay default too.
      var ev = parseEventValue(target.getAttribute("data-on:click"));
      self.send({ t: "event", cid: cid, n: ev.name, p: ev.payload });
    }, true);

    document.addEventListener("submit", function (e) {
      var form = e.target.closest("form[data-on\\:submit]");
      if (!form) return;
      e.preventDefault();
      var cid = findCid(form);
      if (!cid) return;
      var ev = parseEventValue(form.getAttribute("data-on:submit"));
      var data = {};
      var fd = new FormData(form);
      fd.forEach(function (value, key) {
        // Repeated keys collapse to last value; live forms with
        // ``<select multiple>`` should switch to JSON via data-bind.
        data[key] = value;
      });
      self.send({ t: "event", cid: cid, n: ev.name, p: data });
    }, true);

    // Input bind — debounced so fast typing does not flood the WS.
    document.addEventListener("input", function (e) {
      var el = e.target;
      if (!el.hasAttribute || !el.hasAttribute("data-bind")) return;
      var field = el.getAttribute("data-bind");
      var cid = findCid(el);
      if (!cid || !field) return;
      var key = cid + ":" + field;
      if (self.bindTimers[key]) {
        clearTimeout(self.bindTimers[key]);
      }
      self.bindTimers[key] = setTimeout(function () {
        delete self.bindTimers[key];
        self.send({
          t: "bind",
          cid: cid,
          f: field,
          v: el.type === "checkbox" ? el.checked : el.value,
        });
      }, BIND_DEBOUNCE_MS);
    }, true);

    // ``change`` is the immediate-mode counterpart for selects and
    // checkboxes. Send a bind without debounce.
    document.addEventListener("change", function (e) {
      var el = e.target;
      if (!el.hasAttribute || !el.hasAttribute("data-bind")) return;
      var field = el.getAttribute("data-bind");
      var cid = findCid(el);
      if (!cid || !field) return;
      var key = cid + ":" + field;
      if (self.bindTimers[key]) {
        clearTimeout(self.bindTimers[key]);
        delete self.bindTimers[key];
      }
      self.send({
        t: "bind",
        cid: cid,
        f: field,
        v: el.type === "checkbox" ? el.checked : el.value,
      });
    }, true);
  };

  // ---------------------------------------------------------------
  // Boot
  // ---------------------------------------------------------------

  function start() {
    var client = new LiveClient(LIVE_URL);
    client.bindEvents();
    window.hotframeLive = client;
    document.dispatchEvent(new CustomEvent("hf:ready"));
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
})();
