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
    'width:60px;height:60px;border-radius:50%;border:0;cursor:pointer;',
    'background:#4f46e5;color:#fff;box-shadow:0 6px 24px rgba(0,0,0,.28);',
    'display:flex;align-items:center;justify-content:center;',
    'transition:transform .18s ease,background .18s;}',
    '.rph-btn:hover{transform:scale(1.07);background:#4338ca;}',
    '.rph-btn svg{width:28px;height:28px;pointer-events:none;}',
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

  var chatIcon =
    '<svg viewBox="0 0 24 24" fill="none"><path d="M21 12c0 4.4-4 8-9 8-1.1 0-2.2-.17-3.2-.5L4 21l1.3-3.9C4.5 15.7 3 14 3 12c0-4.4 4-8 9-8s9 3.6 9 8Z" fill="currentColor"/><circle cx="8.5" cy="12" r="1.15" fill="#4f46e5"/><circle cx="12" cy="12" r="1.15" fill="#4f46e5"/><circle cx="15.5" cy="12" r="1.15" fill="#4f46e5"/></svg>';
  var closeIcon =
    '<svg viewBox="0 0 24 24" fill="none"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></svg>';

  var panel = document.createElement('div');
  panel.className = 'rph-panel';

  var btn = document.createElement('button');
  btn.className = 'rph-btn';
  btn.type = 'button';
  btn.setAttribute('aria-label', 'Riphah assistant');
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
