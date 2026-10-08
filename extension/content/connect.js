'use strict';
/* connect.js — Dashboard「一键连接插件」桥接
 *
 * 在 applai 后端页面（/dashboard 等）里监听 window.postMessage：
 *   页面 -> APPLAI_PING                    插件 -> APPLAI_PONG（宣告存在）
 *   页面 -> APPLAI_CONNECT {token}         插件 -> 后台保存 Token -> APPLAI_CONNECT_RESULT {ok}
 * 这样网页点一下就能把 API Token 传进插件，无需复制粘贴，也无需预知插件 ID。
 */
(function () {
  // 只在 Applai 后端页面上激活，避免在招聘网站上多余监听
  const host = location.hostname;
  const isApplaiPage = host === 'applai-backend.fly.dev' ||
    host === 'localhost' || host === '127.0.0.1';
  if (!isApplaiPage) return;

  window.addEventListener('message', (e) => {
    if (e.source !== window || !e.data || typeof e.data.type !== 'string') return;
    if (e.data.type === 'APPLAI_PING') {
      window.postMessage({ type: 'APPLAI_PONG' }, '*');
      return;
    }
    if (e.data.type === 'APPLAI_CONNECT' && typeof e.data.token === 'string') {
      // 插件刚被重新加载时，旧 tab 里的 content script 会失效
      if (!chrome.runtime || !chrome.runtime.id) {
        window.postMessage({ type: 'APPLAI_CONNECT_RESULT', ok: false, error: 'context_invalidated' }, '*');
        return;
      }
      try {
        chrome.runtime.sendMessage(
          { type: 'AUTOAPPLY_SAVE_TOKEN', token: e.data.token },
          (resp) => {
            if (chrome.runtime.lastError) {
              window.postMessage({ type: 'APPLAI_CONNECT_RESULT', ok: false, error: 'context_invalidated' }, '*');
              return;
            }
            const ok = !!(resp && resp.ok);
            window.postMessage({ type: 'APPLAI_CONNECT_RESULT', ok, error: resp && resp.error }, '*');
          }
        );
      } catch (err) {
        window.postMessage({ type: 'APPLAI_CONNECT_RESULT', ok: false, error: 'context_invalidated' }, '*');
      }
    }
  });
})();
