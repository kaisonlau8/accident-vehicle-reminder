/* cards.js — 表格移动端卡片化（配 antd-lite.css 9.6 节，<=768px 生效）
 * 通用机制，各站点在 base 模板尾部内联引入（{{ ds_cards|safe }} 形式）。
 * - 自动给滚动容器（.tw/.ant-table-wrap/.sheet/.store-table-wrapper/.freeze-wrap）里的表格
 *   加 .ant-table-cards，容器加 .ant-table-wrap-cards；
 * - 从 thead 逻辑列（支持 rowspan/colspan）给 td 补 data-label（已有不覆盖）；
 *   th 内的 <small> 副标题会并进标签（如「首保完成率 已保/应保」）；
 * - 卡片标题：th[data-card-title] 所在列的 td 标 data-card-title；
 *   未显式指定时回退为第一个「非 #/名次/排名/编号 且非空」的列；
 * - 控件列（含 button/input/select/textarea/form 或「纯链接」的单元格）标 data-cards-full，
 *   CSS 里整行宽、触控 >=44px；
 * - colspan 行（空表）、无 thead 的表格原样跳过；table[data-cards="off"] 可退出；
 * - MutationObserver 兜底：JS 后插入的行/表头（NSS、超时机器人等）会补标。
 * 桌面端只添加无害属性，不改任何布局与行为。 */
(function () {
  'use strict';
  var WRAP = '.tw,.ant-table-wrap,.sheet,.store-table-wrapper,.freeze-wrap,.table-scroll';
  var SKIP_TITLES = { '#': 1, '名次': 1, '排名': 1, '编号': 1, '序号': 1 };
  var sigs = new WeakMap();
  var cellSigs = new WeakMap();

  function headCells(t) {
    if (!t.tHead || !t.tHead.rows.length) return [];
    var grid = [];
    [].forEach.call(t.tHead.rows, function (row, y) {
      grid[y] = grid[y] || [];
      var x = 0;
      [].forEach.call(row.cells, function (th) {
        while (grid[y][x]) x++;
        for (var dy = 0; dy < th.rowSpan; dy++) {
          grid[y + dy] = grid[y + dy] || [];
          for (var dx = 0; dx < th.colSpan; dx++) grid[y + dy][x + dx] = th;
        }
        x += th.colSpan;
      });
    });
    return grid[grid.length - 1];
  }

  function labelsOf(t) {
    return headCells(t).map(function (th) {
      return (th.innerText || th.textContent || '').replace(/\s+/g, ' ').trim();
    });
  }

  function markRows(t, force) {
    var labels = labelsOf(t);
    if (!labels.length) return;
    var ths = headCells(t), titleIdx = -1, fallback = -1;
    for (var i = 0; i < ths.length; i++) {
      if (ths[i].hasAttribute('data-card-title')) { titleIdx = i; break; }
      if (fallback < 0 && labels[i] && !SKIP_TITLES[labels[i]]) fallback = i;
    }
    if (titleIdx < 0) titleIdx = fallback;
    [].forEach.call(t.tBodies, function (tb) {
      [].forEach.call(tb.rows, function (tr) {
        var column = 0;
        [].forEach.call(tr.cells, function (td) {
          var i = column; column += td.colSpan;
          if (tr.cells.length === 1 && td.colSpan > 1) return;
          var label = labels.slice(i, i + td.colSpan).join(' / ');
          var cellSig = label + '|' + titleIdx;
          if (!force && cellSigs.get(td) === cellSig) return;
          if (i < labels.length) {
            if (label) {
              if (force || !td.hasAttribute('data-label')) td.setAttribute('data-label', label);
            } else if (force) {
              td.removeAttribute('data-label');
            }
          }
          if (i === titleIdx) td.setAttribute('data-card-title', '');
          if (!td.hasAttribute('data-cards-full')) {
            var ctl = td.querySelector('button,input:not([type="hidden"]),select,textarea,form,.ant-btn,.btn');
            if (!ctl) {
              var probe = td.cloneNode(true);
              [].forEach.call(probe.querySelectorAll('a'), function (a) { a.remove(); });
              if (!(probe.textContent || '').trim() && td.querySelector('a')) ctl = true;
            }
            if (ctl) td.setAttribute('data-cards-full', '');
          }
          cellSigs.set(td, cellSig);
        });
      });
    });
  }

  function upgrade(t) {
    if (t.getAttribute('data-cards') === 'off') return;
    if (t.classList.contains('ant-table-cards')) { markRows(t, false); sigs.set(t, labelsOf(t).join('|')); return; }
    if (!t.tHead || !t.tHead.rows.length || !t.tBodies.length) return;
    var labels = labelsOf(t);
    if (!labels.length || !labels.some(Boolean)) return;
    t.classList.add('ant-table-cards');
    var w = t.closest ? t.closest(WRAP) : null;
    if (w) w.classList.add('ant-table-wrap-cards');
    markRows(t, false);
    sigs.set(t, labels.join('|'));
  }

  function scan() {
    [].forEach.call(document.querySelectorAll('table'), function (t) {
      if (t.classList.contains('ant-table-cards')) {
        var sig = labelsOf(t).join('|');
        markRows(t, sigs.get(t) !== sig);   /* 动态表头及动态行都补标签 */
        sigs.set(t, sig);
        return;
      }
      upgrade(t);
    });
  }

  var pending = null;
  function schedule() {
    if (pending) return;
    pending = setTimeout(function () { pending = null; scan(); }, 50);
  }

  scan();
  if (document.body) {
    new MutationObserver(schedule).observe(document.body, { childList: true, subtree: true });
  } else {
    document.addEventListener('DOMContentLoaded', scan);
  }
})();

/* 统一交互增强。模板指定边界/父路由；长表兜底只包表格，不隐藏统计与操作。 */
(function () {
  'use strict';
  if (window.MheroInteraction) return;
  var serial = 0, timer, mobile = matchMedia('(max-width: 860px)');
  var states = new WeakMap();
  function icon(name, cls) {
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'ai ' + (cls || ''));
    svg.setAttribute('aria-hidden', 'true');
    var use = document.createElementNS(svg.namespaceURI, 'use');
    use.setAttribute('href', '#i-' + name); svg.appendChild(use); return svg;
  }
  function setText(el, text) { if (el.textContent !== text) el.textContent = text; }
  function rows(root) {
    return [].filter.call(root.querySelectorAll('tbody > tr'), function (r) {
      return !(r.cells.length === 1 && r.cells[0].colSpan > 1) && !r.classList.contains('tot');
    }).length;
  }
  function collapse(box, filter) {
    var state = states.get(box);
    if (!state) {
      var panel = document.createElement('div'); panel.className = 'ds-collapse-panel';
      panel.id = 'ds-panel-' + (++serial);
      while (box.firstChild) panel.appendChild(box.firstChild);
      var btn = document.createElement('button'); btn.type = 'button'; btn.className = 'ds-collapse-toggle';
      var label = document.createElement('span'); label.textContent = box.dataset.dsTitle || (filter ? '筛选条件' : '明细列表');
      var count = document.createElement('span'); count.className = filter ? 'ds-filter-summary' : 'ds-collapse-count';
      btn.appendChild(label); btn.appendChild(count); btn.appendChild(icon('down', 'ds-collapse-arrow'));
      btn.setAttribute('aria-controls', panel.id); box.appendChild(btn); box.appendChild(panel);
      state = { panel: panel, btn: btn, count: count, open: false, filter: filter };
      states.set(box, state);
      btn.addEventListener('click', function () { state.open = !state.open; refresh(box); });
    }
    refresh(box);
  }
  function summary(panel) {
    var parts = [];
    [].forEach.call(panel.querySelectorAll('input:not([type="hidden"]):not([type="submit"]),select'), function (el) {
      if (el.type === 'file' || el.type === 'password' || el.disabled) return;
      if ((el.type === 'checkbox' || el.type === 'radio') && !el.checked) return;
      var value = el.tagName === 'SELECT' ? [].filter.call(el.options, function (o) { return o.selected && o.value; }).map(function (o) { return o.textContent; }).join('、') : el.value;
      if (!value) return;
      var group = el.closest('.selector-group,.ant-filter-item,.filter-item');
      var label = (el.labels && el.labels[0]) || (group && group.querySelector('label'));
      parts.push((label ? label.textContent.trim() : el.getAttribute('aria-label') || el.name || '搜索') + '：' + ((el.type === 'checkbox') ? '已启用' : value));
    });
    [].forEach.call(panel.querySelectorAll('.select-trigger'), function (el) { parts.push(el.textContent.trim()); });
    return parts.length ? parts.join(' · ') : '未筛选';
  }
  function refresh(box) {
    var s = states.get(box); if (!s) return;
    var open = s.filter && !mobile.matches ? true : s.open;
    s.panel.hidden = !open; s.btn.setAttribute('aria-expanded', String(open));
    if (s.filter) {
      if (!s.dirty || s.href !== location.href || s.panel.querySelector('[data-ds-filter-live]')) {
        s.applied = summary(s.panel); s.href = location.href; s.dirty = false;
      }
      setText(s.count, s.applied + (s.dirty ? ' · 有未应用修改' : ''));
    } else setText(s.count, '(' + (box.dataset.dsCount || rows(s.panel)) + ')');
  }
  function longTables() {
    [].forEach.call(document.querySelectorAll('table'), function (t) {
      if (t.closest('details,.ds-collapse,[data-ds-summary]') || rows(t) <= 8) return;
      var wrap = t.closest('.tw,.ant-table-wrap,.sheet,.store-table-wrapper,.freeze-wrap,.table-scroll') || t;
      var card = wrap.closest('.card,.ant-card,.stat-block,section');
      var heading = card && card.querySelector('h2,h3');
      var box = document.createElement('div'); box.className = 'ds-collapse';
      box.dataset.dsTitle = t.dataset.dsTitle || (heading && heading.textContent.replace(/\s+/g, ' ').trim()) || '明细列表';
      wrap.parentNode.insertBefore(box, wrap); box.appendChild(wrap); collapse(box, false);
    });
  }
  function filters() {
    [].forEach.call(document.querySelectorAll('form.ant-filter-bar, .timeout-filters, [data-ds-filter]'), function (el) {
      if (el.closest('details,.ds-collapse') || (el.closest('form') && el.closest('form').method.toLowerCase() !== 'get')) return;
      var box = document.createElement('div'); box.className = 'ds-collapse ds-filter';
      el.parentNode.insertBefore(box, el); box.appendChild(el); collapse(box, true);
    });
  }
  function nativeDetails(d) {
    var head = d.querySelector(':scope > summary'); if (!head) return;
    head.classList.add('ds-native-summary');
    head.setAttribute('aria-expanded', String(d.open));
    if (!d.dataset.dsToggleReady) {
      d.addEventListener('toggle', function () { head.setAttribute('aria-expanded', String(d.open)); });
      d.dataset.dsToggleReady = '1';
    }
    if (!head.querySelector(':scope > .ds-collapse-arrow')) head.appendChild(icon('down', 'ds-collapse-arrow'));
    // 已有计数沿用；自动计数只用于有表格且标题没有计数的明细。
    if (d.querySelector('table') && !head.querySelector('.store-count,.ds-collapse-count') && !/\d/.test(head.textContent)) {
      var count = document.createElement('span'); count.className = 'ds-collapse-count';
      head.insertBefore(count, head.lastChild);
    }
    var c = head.querySelector('.ds-collapse-count'); if (c) setText(c, '(' + rows(d) + ')');
    if (d.hasAttribute('data-mobile-collapsed') && !d.dataset.dsMobileReady) {
      d.open = !mobile.matches; d.dataset.dsMobileReady = '1';
    }
  }
  function links() {
    [].forEach.call(document.querySelectorAll('a[href]'), function (a) {
      var raw = a.getAttribute('href'), u;
      if (!raw || raw[0] === '#' || a.closest('.brand') || a.classList.contains('brand') || a.classList.contains('ds-back') || a.hasAttribute('download') || a.dataset.dsLink === 'off') return;
      try { u = new URL(a.href); } catch (e) { return; }
      if (!/^https?:$/.test(u.protocol) || /logout|\/_auth\/|\/auth\//.test(u.pathname)) return;
      if (/\.(xlsx?|csv|zip|pdf|png)$/.test(u.pathname) || /\/export|\/download|\/api\/report\/latest/.test(u.pathname)) return;
      var external = u.origin !== location.origin || a.target === '_blank';
      if (!external && u.pathname === location.pathname && !a.closest('.ant-pagination')) return;
      var name = external ? 'export' : 'right';
      if (external) { a.target = '_blank'; a.rel = (a.rel + ' noopener').trim().split(/\s+/).filter(function (v,i,x) { return x.indexOf(v) === i; }).join(' '); }
      if (!a.querySelector('use[href="#i-' + name + '"]') && !a.querySelector('use[href="#i-arrow-left"]')) a.appendChild(icon(name, 'ds-link-icon'));
      a.classList.add('ds-page-link');
    });
  }
  function closes() {
    [].forEach.call(document.querySelectorAll('button,a[role="button"],.ant-notification-notice-close'), function (b) {
      var text = b.textContent.trim();
      if (!b.matches('.ds-close,.sd-x,.rule-x,.nav-drawer-close,.ant-modal-close,.ant-drawer-close,.ant-message-notice-close,.ant-notification-notice-close,.ant-alert-close-icon,.dual-slot-rm') && !/^(关闭|×|✕)$/.test(text)) return;
      b.classList.add('ds-close');
      if (!b.classList.contains('dual-slot-rm')) b.setAttribute('aria-label', '关闭');
      if (!b.querySelector('svg')) { b.textContent = ''; b.appendChild(icon('close')); }
    });
  }
  function cells() {
    [].forEach.call(document.querySelectorAll('th[onclick],table.sortable th:not(.nosort)'), function (th) {
      if (th.querySelector('a,button') || th.dataset.dsKeyboard) return;
      th.classList.add('ds-cell-action'); th.tabIndex = 0; th.setAttribute('role', 'button');
      th.title = th.title || '点击或按 Enter 排序'; th.dataset.dsKeyboard = '1';
      th.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); th.click(); }
      });
    });
  }
  function back() {
    var config = document.querySelector('[data-ds-back]'); if (!config || config.querySelector('.ds-title-row')) return;
    var heading = config.querySelector('.ant-page-header-heading > h1, :scope > h1, :scope > h2');
    if (!heading) return;
    var a = config.querySelector('a.ds-back') || [].find.call(config.querySelectorAll('.ant-page-header a'), function (link) { return /^返回/.test(link.textContent.trim()); });
    if (!a) { a = document.createElement('a'); a.className = 'ds-back'; a.href = config.dataset.dsBack; a.appendChild(icon('arrow-left')); a.appendChild(document.createTextNode('返回')); }
    a.classList.add('ds-back');
    if (!a.querySelector('use[href="#i-arrow-left"]')) a.insertBefore(icon('arrow-left'), a.firstChild);
    var row = document.createElement('div'); row.className = 'ds-title-row';
    heading.parentNode.insertBefore(row, heading); row.appendChild(a); row.appendChild(heading);
  }
  function scan() {
    back();
    filters();
    [].forEach.call(document.querySelectorAll('.ds-collapse'), function (b) { collapse(b, b.classList.contains('ds-filter')); });
    longTables();
    [].forEach.call(document.querySelectorAll('details'), nativeDetails);
    links(); closes(); cells();
  }
  function schedule() { if (!timer) timer = setTimeout(function () { timer = null; scan(); }, 80); }
  window.MheroInteraction = { scan: scan, icon: icon };
  function boot() {
    scan(); new MutationObserver(schedule).observe(document.body, { childList: true, subtree: true, characterData: true });
    function filterInput(e) {
      var box = e.target.closest && e.target.closest('.ds-filter');
      var s = box && states.get(box);
      if (s) s.dirty = summary(s.panel) !== s.applied;
      schedule();
    }
    document.addEventListener('input', filterInput); document.addEventListener('change', filterInput);
    mobile.addEventListener('change', function () { document.querySelectorAll('.ds-filter').forEach(refresh); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();

/* 移动端导航（<=860px，与 antd-lite.css 4.5–4.7 同一断点）。
 * 统一抽屉契约（配套 antd-lite.css 4.6）：
 * - 断点唯一：max-width 860px；桌面端不改顶栏。
 * - 自动汉堡：仅当顶栏（.topbar/.ant-header）里没有 .burger、没有 [data-burger]、
 *   没有任何 #burger / [id$="-burger"] / [class$="-burger"]（页面自带汉堡的常见命名），
 *   且顶栏及其祖先都没有 data-no-auto-burger 时，才插入一只按钮（data-mhero-burger
 *   标记）并把顶栏里的 .menu/.ant-menu/裸 nav 收成右侧抽屉。
 * - 页面自带按钮（class="burger" 或 data-burger 属性）：不插第二只、不接管开合
 *   （开合仍由页面 JS 切 .topbar.open / .ant-header.open / body.nav-open 驱动），
 *   这里只补四种关闭行为：遮罩点击、Escape、点击菜单链接、断点切回桌面。
 * - 幂等：脚本被引入两次时 window.__mheroMobileNav 保证只初始化一次
 *   （按钮只插一只、document 级事件只绑一次）。
 * - 黄页 .hub-user 仍用页面自己的点击切换，这里补姓名行、aria 与遮罩关闭。 */
(function () {
  'use strict';
  if (window.__mheroMobileNav) return;
  window.__mheroMobileNav = true;

  var mq = window.matchMedia('(max-width: 860px)');
  var OWN = '.burger, [data-burger]';

  function setBurger(btn, open) {
    if (!btn) return;
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    var label = btn.getAttribute('aria-label') || '';
    if (btn.getAttribute('data-mhero-burger') === '1' || label === '打开菜单' || label === '关闭菜单') {
      btn.setAttribute('aria-label', open ? '关闭菜单' : '打开菜单');
    }
  }

  function closeBar(bar) {
    bar.classList.remove('open');
    setBurger(bar.querySelector(OWN), false);
  }

  function navCandidate(header) {
    var box = header.querySelector(':scope > .in') || header;
    return box.querySelector(':scope > .menu, :scope > .ant-menu') || box.querySelector(':scope > nav');
  }

  function ensureAutoBurger(header) {
    if (header.querySelector(OWN)) return;                          /* 已有 .burger / [data-burger] */
    if (header.querySelector('#burger, [id$="-burger"], [class$="-burger"]')) return; /* 自带命名的汉堡 */
    if (header.closest('[data-no-auto-burger]')) return;            /* 顶栏或祖先显式退出 */
    if (header.querySelector('.burger[data-mhero-burger]')) return; /* 幂等：这只顶栏已插过 */
    var nav = navCandidate(header);
    if (!nav) return;
    if (!nav.id) nav.id = 'mainmenu';
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'burger';
    btn.setAttribute('data-mhero-burger', '1');
    btn.setAttribute('aria-controls', nav.id);
    btn.setAttribute('aria-expanded', 'false');
    btn.setAttribute('aria-label', '打开菜单');
    /* 插到 .in 末尾，配合 CSS order:999 / margin-left:auto，视觉与 DOM 都在最右 */
    nav.parentNode.appendChild(btn);
    if (!nav.classList.contains('menu') && !nav.classList.contains('ant-menu')) {
      /* 无 ds 菜单 class 的裸 nav：加 .has-burger 才有抽屉样式；
         .nav-ready 两帧后再上，首次加载才不会从水平菜单位置滑出 */
      header.classList.add('has-burger');
      requestAnimationFrame(function () {
        requestAnimationFrame(function () { header.classList.add('nav-ready'); });
      });
    }
  }

  function ensureHubWho(u) {
    var dd = u.querySelector('.dd');
    if (!dd || dd.querySelector('.hub-drawer-who')) return;
    var who = document.createElement('div');
    who.className = 'hub-drawer-who';
    var name = u.querySelector('.n');
    var tag = u.querySelector('.ant-tag');
    who.textContent = name ? name.textContent.trim() : '';
    if (tag && tag.textContent.trim()) {
      who.appendChild(document.createTextNode(' '));
      var pill = document.createElement('span');
      pill.className = tag.className;
      pill.textContent = tag.textContent.trim();
      who.appendChild(pill);
    }
    dd.insertBefore(who, dd.firstChild);
  }

  function syncHubLabel() {
    var u = document.querySelector('.hub-user');
    if (!u) return;
    var btn = u.querySelector(':scope > button');
    if (!btn) return;
    if (!mq.matches) {
      if (btn.hasAttribute('data-hub-aria')) {
        btn.removeAttribute('aria-label');
        btn.removeAttribute('data-hub-aria');
      }
      return;
    }
    ensureHubWho(u);
    if (!u.classList.contains('open')) btn.setAttribute('aria-label', '打开菜单');
    btn.setAttribute('data-hub-aria', '1');
  }

  function boot() {
    [].forEach.call(document.querySelectorAll('.topbar, .ant-header'), ensureAutoBurger);
    syncHubLabel();
  }

  document.addEventListener('click', function (e) {
    var t = e.target;
    if (!t || !t.closest) return;

    /* 黄页 .hub-user 打开时点自身（遮罩区域）关闭 */
    if (t.classList && t.classList.contains('hub-user') && t.classList.contains('open')) {
      t.classList.remove('open');
      var hb = t.querySelector(':scope > button');
      if (hb) {
        hb.setAttribute('aria-expanded', 'false');
        hb.setAttribute('aria-label', '打开菜单');
      }
      return;
    }

    /* 本脚本插入的汉堡：开合在这里驱动（页面自带按钮的开合由页面自己驱动） */
    var own = t.closest('.burger[data-mhero-burger], [data-burger][data-mhero-burger]');
    if (own) {
      e.stopPropagation();
      var hdr = own.closest('.topbar, .ant-header');
      if (hdr) {
        var open = !hdr.classList.contains('open');
        hdr.classList.toggle('open', open);
        setBurger(own, open);
      }
      return;
    }

    /* 无页面脚本的顶栏：抽屉里的下拉分组按钮也能展开/收起
       （reportdb 这类自带处理的页面在自己的 handler 里 stopPropagation，不会走到这） */
    var tog = t.closest('.mi > button, .ant-menu-item > button');
    if (tog) {
      var mi = tog.closest('.mi, .ant-menu-item');
      if (mi) {
        var was = mi.classList.contains('open');
        [].forEach.call(mi.parentNode ? mi.parentNode.children : [], function (sib) {
          if (sib !== mi) sib.classList.remove('open');
        });
        mi.classList.toggle('open', !was);
        tog.setAttribute('aria-expanded', !was ? 'true' : 'false');
      }
      return;
    }

    if (!mq.matches) return;
    var bar = t.closest('.topbar.open, .ant-header.open');
    if (!bar) return;
    /* 点抽屉里的链接（含下拉项 / 表单提交按钮）即导航：收起抽屉 */
    if (t.closest('a, button.lnk')) { closeBar(bar); return; }
    /* 点到遮罩（伪元素遮罩的点击落在顶栏本体）或顶栏空白处：关闭 */
    if (t.closest('.menu, .ant-menu, nav, .burger, [data-burger], button')) return;
    closeBar(bar);
  });

  /* 黄页按钮自己的脚本会 stopPropagation，捕获阶段才能在切换之后读到 .open */
  document.addEventListener('click', function (e) {
    var btn = e.target && e.target.closest && e.target.closest('.hub-user > button');
    if (!btn || !mq.matches) return;
    var u = btn.parentNode;
    setTimeout(function () {
      var open = u.classList.contains('open');
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
      btn.setAttribute('aria-label', open ? '关闭菜单' : '打开菜单');
    }, 0);
  }, true);

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    [].forEach.call(document.querySelectorAll('.topbar.open, .ant-header.open'), closeBar);
    var u = document.querySelector('.hub-user.open');
    if (!u) return;
    u.classList.remove('open');
    var hb = u.querySelector(':scope > button');
    if (hb) {
      hb.setAttribute('aria-expanded', 'false');
      hb.setAttribute('aria-label', '打开菜单');
    }
  });

  if (mq.addEventListener) {
    mq.addEventListener('change', function () {
      if (mq.matches) return;
      [].forEach.call(document.querySelectorAll('.topbar.open, .ant-header.open'), closeBar);
      var u = document.querySelector('.hub-user.open');
      if (u) u.classList.remove('open');
      syncHubLabel();
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
