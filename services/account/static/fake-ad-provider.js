/* Fake ad-provider tag served by the Account Service (ad_sources.fake_script) — dev/test only.
 * Mimics a third-party ad script: finds its ad containers, renders an unmistakable "FAKE" box,
 * and (for AdSense) honours the `adsbygoogle.push({})` contract. No request leaves the machine.
 * Placeholders (__NAME__ …) are substituted by the server. */
(function () {
  var NAME = __NAME__, COLOR = __COLOR__, SELECTOR = __SELECTOR__, ATTRS = __ATTRS__, KIND = __KIND__;
  function params() {
    var s = document.currentScript;
    if (!s) {
      var all = Array.prototype.slice.call(document.scripts);
      s = all.filter(function (x) { return x.src && x.src.indexOf(__MARKER__) !== -1; }).pop();
    }
    var out = {};
    if (s && s.src.indexOf('?') !== -1) {
      s.src.split('?')[1].split('&').forEach(function (kv) {
        var p = kv.split('=');
        out[decodeURIComponent(p[0])] = decodeURIComponent(p[1] || '');
      });
    }
    return out;
  }
  var query = params();
  function render(el) {
    if (!el || el.getAttribute('data-fake-rendered')) return;
    el.setAttribute('data-fake-rendered', '1');
    var lines = [];
    ATTRS.forEach(function (a) {
      if (el.getAttribute(a)) lines.push(a.replace(/^data-/, '') + ': ' + el.getAttribute(a));
    });
    Object.keys(query).forEach(function (k) { lines.push(k + ': ' + query[k]); });
    var slug = NAME.toLowerCase().replace(/[^a-z]+/g, '-');
    el.innerHTML =
      '<div class="fake-ad-box" style="font:14px/1.45 system-ui,sans-serif;text-decoration:none;border:2px dashed ' +
      COLOR + ';border-radius:10px;padding:12px;background:#fff;color:#111">' +
      '<div style="font-size:11px;font-weight:800;letter-spacing:.08em;color:' + COLOR + '">' +
      NAME + ' \u2014 FAKE</div>' +
      '<div style="font-weight:700;font-size:16px;margin:6px 0 4px">Example responsive display ad</div>' +
      '<div style="color:#555">Rendered by the fake provider script served from this API. ' +
      'No request left this machine.</div>' +
      '<div style="font-size:12px;color:#777;margin-top:6px">' + lines.join(' \u00b7 ') + '</div>' +
      '<a href="https://example.com/' + slug + '?utm_source=fake" target="_blank" ' +
      'rel="noopener noreferrer nofollow sponsored" style="display:inline-block;margin-top:8px;color:' +
      COLOR + ';font-weight:700">Visit advertiser \u2197</a></div>';
    if (window.__paFakeAdRendered) window.__paFakeAdRendered(KIND, el);
  }
  function renderAll() {
    Array.prototype.forEach.call(document.querySelectorAll(SELECTOR), render);
  }
  window.__paFakeProviders = window.__paFakeProviders || {};
  window.__paFakeProviders[KIND] = { renderAll: renderAll };
  if (KIND === 'adsense') {
    // Same contract as the real tag: (window.adsbygoogle = window.adsbygoogle || []).push({}) renders the next <ins>.
    window.adsbygoogle = { push: function () { renderAll(); return 0; }, loaded: true };
  }
  renderAll();
  if (window.MutationObserver) {
    new MutationObserver(renderAll).observe(document.documentElement, { childList: true, subtree: true });
  }
})();
