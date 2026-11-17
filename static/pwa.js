/*
 * PWA 注册 + 更新提示。
 * 只在 HTTPS 或 localhost 下生效；HTTP 公网地址（如开发机 IP）浏览器会直接跳过。
 */
(function () {
  if (!('serviceWorker' in navigator)) return;

  let hadController = !!navigator.serviceWorker.controller;
  let reloading = false;

  navigator.serviceWorker.addEventListener('controllerchange', function () {
    if (!hadController) {
      // 首次安装：clients.claim() 也会触发 controllerchange，不要刷新页面
      hadController = true;
      return;
    }
    if (reloading) return;
    reloading = true;
    window.location.reload();
  });

  function showUpdate(reg) {
    if (document.getElementById('gcl-pwa-update')) return;

    const bar = document.createElement('div');
    bar.id = 'gcl-pwa-update';
    bar.setAttribute('role', 'status');
    bar.style.cssText = [
      'position:fixed', 'left:12px', 'right:12px', 'bottom:12px', 'z-index:2147483000',
      'display:flex', 'align-items:center', 'gap:12px', 'flex-wrap:wrap',
      'padding:14px 16px', 'border-radius:14px',
      'background:#1e293b', 'color:#f8fafc',
      'box-shadow:0 10px 35px rgba(15,23,42,.28)',
      'font:14px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif'
    ].join(';');

    const text = document.createElement('span');
    text.style.cssText = 'flex:1 1 180px;min-width:0';
    text.textContent = '新版本已准备好';

    const later = document.createElement('button');
    later.type = 'button';
    later.textContent = '稍后';
    later.style.cssText = [
      'border:0', 'background:transparent', 'color:#cbd5e1',
      'font:inherit', 'padding:8px 10px', 'cursor:pointer'
    ].join(';');
    later.addEventListener('click', function () { bar.remove(); });

    const refresh = document.createElement('button');
    refresh.type = 'button';
    refresh.textContent = '刷新';
    refresh.style.cssText = [
      'border:0', 'border-radius:9px', 'background:#6366f1', 'color:#fff',
      'font:inherit', 'font-weight:600', 'padding:8px 16px', 'cursor:pointer'
    ].join(';');
    refresh.addEventListener('click', function () {
      bar.remove();
      if (reg && reg.waiting) {
        reg.waiting.postMessage({ type: 'SKIP_WAITING' });
      } else {
        window.location.reload();
      }
    });

    bar.appendChild(text);
    bar.appendChild(later);
    bar.appendChild(refresh);
    document.body.appendChild(bar);
  }

  window.addEventListener('load', function () {
    navigator.serviceWorker.register('/sw.js', { scope: '/' }).then(function (reg) {
      // 每次打开都检查一次新版本；失败（离线）不提示
      reg.update().catch(function () {});

      if (reg.waiting) showUpdate(reg);

      reg.addEventListener('updatefound', function () {
        const installing = reg.installing;
        if (!installing) return;
        installing.addEventListener('statechange', function () {
          if (installing.state === 'installed' && navigator.serviceWorker.controller) {
            showUpdate(reg);
          }
        });
      });
    }).catch(function () {
      // 不支持 / 非安全上下文 / 被策略禁用：静默跳过，不影响正常使用
    });
  });
})();
