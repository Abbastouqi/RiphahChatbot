/* Riphah chatbot launcher widget.
 *
 * Embed on any website with one line:
 *   <script src="https://YOUR-BOT-HOST/widget.js" defer></script>
 *
 * Renders a floating chat icon (bottom-right). Clicking it slides open the
 * chatbot in a panel; clicking again (or the panel's ✕) closes it. The bot
 * itself loads in an iframe from the same host this script was served from,
 * so there is nothing to configure.
 */
(function () {
  'use strict';
  if (window.__riphahWidget) return;   // don't double-install
  window.__riphahWidget = true;

  // Where the bot lives = where this script came from.
  var script = document.currentScript;
  var origin = script && script.src ? new URL(script.src).origin : '';
  if (!origin) return;

  var css = [
    '.rph-btn{position:fixed;right:22px;bottom:22px;z-index:2147483000;',
    'width:62px;height:62px;border-radius:50%;border:0;cursor:pointer;',
    'background:linear-gradient(135deg,#6366f1 0%,#8b5cf6 55%,#c084fc 100%);',
    'color:#fff;box-shadow:0 6px 22px rgba(99,102,241,.45),0 2px 8px rgba(0,0,0,.25);',
    'display:flex;align-items:center;justify-content:center;',
    'transition:transform .18s ease,box-shadow .18s;animation:rph-glow 3s ease-in-out infinite;}',
    '.rph-btn:hover{transform:scale(1.08);box-shadow:0 8px 30px rgba(139,92,246,.6),0 2px 10px rgba(0,0,0,.3);}',
    '.rph-btn svg{width:30px;height:30px;pointer-events:none;}',
    '@keyframes rph-glow{0%,100%{box-shadow:0 6px 22px rgba(99,102,241,.45),0 2px 8px rgba(0,0,0,.25)}',
    '50%{box-shadow:0 6px 30px rgba(139,92,246,.65),0 2px 8px rgba(0,0,0,.25)}}',
    '@media (prefers-reduced-motion:reduce){.rph-btn{animation:none}}',
    '.rph-panel{position:fixed;right:22px;bottom:94px;z-index:2147483000;',
    'width:min(400px,calc(100vw - 32px));height:min(620px,calc(100vh - 120px));',
    'border-radius:18px;overflow:hidden;box-shadow:0 12px 48px rgba(0,0,0,.4);',
    'background:#000;opacity:0;transform:translateY(14px) scale(.98);',
    'pointer-events:none;transition:opacity .22s ease,transform .22s ease;}',
    '.rph-panel.open{opacity:1;transform:none;pointer-events:auto;}',
    '.rph-panel iframe{width:100%;height:100%;border:0;display:block;}',
    '@media (max-width:480px){.rph-panel{right:8px;bottom:86px;',
    'width:calc(100vw - 16px);height:calc(100vh - 104px);}}'
  ].join('');

  var style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);

  // Friendly robot face: antenna, headphone band with ear cups, round eyes
  // and a smile — white on the gradient button.
  var chatIcon =
    '<svg viewBox="0 0 24 24" fill="none">' +
    '<path d="M12 2.4v2.2" stroke="#fff" stroke-width="1.6" stroke-linecap="round"/>' +
    '<circle cx="12" cy="2.4" r="1.05" fill="#fff"/>' +
    '<path d="M4.6 12.2a7.4 7.4 0 0 1 14.8 0" stroke="#fff" stroke-width="1.7" stroke-linecap="round"/>' +
    '<rect x="5.6" y="6.6" width="12.8" height="11" rx="3.4" fill="#fff"/>' +
    '<rect x="2.7" y="10.6" width="2.5" height="5" rx="1.25" fill="#fff"/>' +
    '<rect x="18.8" y="10.6" width="2.5" height="5" rx="1.25" fill="#fff"/>' +
    '<circle cx="9.4" cy="11.3" r="1.45" stroke="#7255e0" stroke-width="1.5"/>' +
    '<circle cx="14.6" cy="11.3" r="1.45" stroke="#7255e0" stroke-width="1.5"/>' +
    '<path d="M9.6 14.9c.7.75 1.6 1.15 2.4 1.15s1.7-.4 2.4-1.15" stroke="#7255e0" stroke-width="1.5" stroke-linecap="round"/>' +
    '</svg>';
  var closeIcon =
    '<svg viewBox="0 0 24 24" fill="none"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></svg>';

  var panel = document.createElement('div');
  panel.className = 'rph-panel';

  var btn = document.createElement('button');
  btn.className = 'rph-btn';
  btn.type = 'button';
  btn.title = 'Ask AI';
  btn.setAttribute('aria-label', 'Ask AI — Riphah assistant');
  btn.innerHTML = chatIcon;

  var loaded = false;
  function toggle(open) {
    var willOpen = open !== undefined ? open : !panel.classList.contains('open');
    if (willOpen && !loaded) {
      // The iframe is created lazily so the widget adds zero weight to the
      // host page until a visitor actually opens the chat.
      // If the host site knows who the visitor is (a logged-in portal user),
      // it can set window.RIPHAH_USER_ID before this script loads — chat
      // history then follows the person, not the browser (shared lab PCs).
      var uid = (window.RIPHAH_USER_ID || (script.dataset && script.dataset.userId) || '')
        .toString().trim().slice(0, 64);
      var frame = document.createElement('iframe');
      frame.src = origin + '/' + (uid ? '?uid=' + encodeURIComponent(uid) : '');
      frame.allow = 'microphone';        // required for voice mode
      frame.title = 'Riphah International University assistant';
      panel.appendChild(frame);
      loaded = true;
    }
    panel.classList.toggle('open', willOpen);
    btn.innerHTML = willOpen ? closeIcon : chatIcon;
    btn.setAttribute('aria-expanded', String(willOpen));
  }

  btn.addEventListener('click', function () { toggle(); });
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape' && panel.classList.contains('open')) toggle(false);
  });

  document.body.appendChild(panel);
  document.body.appendChild(btn);
})();
