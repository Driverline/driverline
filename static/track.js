/* Remembers which ad or link brought this visitor (30 days) so TradeLens can tell which campaigns bring real members.
   Stores only the campaign tags from the link, the referring site's host name and the landing page. No personal data. */
(function () {
  var K = 'dl_attr', LIFE = 30 * 864e5, q = new URLSearchParams(location.search), a = {}, tagged = false;
  [['utm_source', 'source'], ['utm_medium', 'medium'], ['utm_campaign', 'campaign'], ['utm_content', 'content'],
   ['utm_term', 'term'], ['ref', 'ref']].forEach(function (p) {
    var v = q.get(p[0]); if (v) { a[p[1]] = v.slice(0, 60); tagged = true; }
  });
  var cur = null; try { cur = JSON.parse(localStorage.getItem(K) || 'null'); } catch (e) {}
  var host = ''; try { host = document.referrer ? new URL(document.referrer).hostname : ''; } catch (e) {}
  if (host === location.hostname) host = '';
  if (tagged || !cur || Date.now() - (cur.t || 0) > LIFE) {       // a new tagged link wins; otherwise keep the first touch
    a.referrer = host.slice(0, 60); a.landing = location.pathname.slice(0, 80); a.t = Date.now(); cur = a;
    try { localStorage.setItem(K, JSON.stringify(cur)); } catch (e) {}
  }
  try {
    document.cookie = K + '=' + encodeURIComponent(JSON.stringify(cur)) + ';path=/;max-age=' + (LIFE / 1000) + ';SameSite=Lax' +
      (location.protocol === 'https:' ? ';Secure' : '');
  } catch (e) {}
  try {
    if (!sessionStorage.getItem('dl_v')) {                          // count the visit once per browser session
      sessionStorage.setItem('dl_v', '1');
      fetch('/api/track/visit', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(cur), keepalive: true}).catch(function () {});
    }
  } catch (e) {}
})();
