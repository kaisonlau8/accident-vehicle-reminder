/* feedback.js — Ant Design 交互反馈（Button loading / Spin 由页面标记，message 轻提示）。
 * 无第三方动效库。页面约定：
 * - <button data-loading-text="正在保存"> 所在表单提交后进入 loading，并弹出 message.loading
 * - <a data-nav data-loading-text="正在打开账号设置"> 同页跳转
 * - <a data-nav="blank" data-success-text="已在新标签页打开「…」" data-error-text="…"> 新标签
 * - 点击被禁用控件的包裹元素 [data-blocked-text] 时弹出错误提示
 * - #flash-ok / #flash-err 的文案在加载后用 message 再提示一次
 * 暴露 window.MheroFeedback.toast(type, text) ，type = success | error | info | warning | loading
 */
(function () {
  var ICONS = { success: "check-circle", error: "close-circle", warning: "exclamation-circle", info: "info-circle" };

  function loadingSvg() {
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "ai anticon-spin");
    svg.setAttribute("viewBox", "0 0 1024 1024");
    svg.setAttribute("aria-hidden", "true");
    var use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", "#i-loading");
    svg.appendChild(use);
    return svg;
  }

  function root() {
    var el = document.querySelector(".ant-message");
    if (!el) {
      el = document.createElement("div");
      el.className = "ant-message";
      el.setAttribute("aria-live", "polite");
      document.body.appendChild(el);
    }
    return el;
  }

  function toast(type, text) {
    text = (text || "").trim();
    if (!text) return null;
    var notice = document.createElement("div");
    notice.className = "ant-message-notice";
    var content = document.createElement("div");
    content.className = "ant-message-notice-content";
    var row = document.createElement("div");
    row.className = "ant-message-custom-content ant-message-" + type;
    if (type === "loading") {
      var icon = document.createElement("span");
      icon.className = "ant-message-icon";
      icon.appendChild(loadingSvg());
      row.appendChild(icon);
    } else if (ICONS[type]) {
      var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("class", "ai");
      svg.setAttribute("aria-hidden", "true");
      var use = document.createElementNS("http://www.w3.org/2000/svg", "use");
      use.setAttribute("href", "#i-" + ICONS[type]);
      svg.appendChild(use);
      row.appendChild(svg);
    }
    var label = document.createElement("span");
    label.className = "ant-message-text";
    label.textContent = text;
    row.appendChild(label);
    content.appendChild(row);
    notice.appendChild(content);
    root().appendChild(notice);
    function close() {
      if (!notice.isConnected || notice.classList.contains("ant-message-notice-leave")) return;
      notice.classList.add("ant-message-notice-leave");
      setTimeout(function () { notice.remove(); }, 200);
    }
    if (type !== "loading") setTimeout(close, type === "error" ? 4000 : 3000);
    return { close: close };
  }

  function setLoading(el) {
    if (!el || el.getAttribute("data-loading") === "1") return;
    el.setAttribute("data-loading", "1");
    el.setAttribute("aria-busy", "true");
    el.classList.add("ant-btn-loading");
    Array.prototype.forEach.call(el.children, function (ch) {
      if (ch.classList && ch.classList.contains("ai")) ch.setAttribute("data-fb-hidden", "1");
    });
    var icon = document.createElement("span");
    icon.className = "ant-btn-loading-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.appendChild(loadingSvg());
    el.insertBefore(icon, el.firstChild);
    setTimeout(function () {
      if (el.getAttribute("data-loading") !== "1") return;
      el.classList.add("is-locked");
      if (el.tagName === "BUTTON" && !el.disabled) {
        el.disabled = true;
        el.setAttribute("data-fb-dis", "1");
      }
    }, 0);
  }

  function clearLoading(el) {
    if (!el) return;
    el.classList.remove("ant-btn-loading", "is-locked");
    el.removeAttribute("data-loading");
    el.removeAttribute("aria-busy");
    var icon = el.querySelector(":scope > .ant-btn-loading-icon");
    if (icon) icon.remove();
    Array.prototype.forEach.call(el.querySelectorAll("[data-fb-hidden]"), function (n) {
      n.removeAttribute("data-fb-hidden");
    });
    if (el.getAttribute("data-fb-dis") === "1") {
      el.disabled = false;
      el.removeAttribute("data-fb-dis");
    }
  }

  function blocked(el) {
    return el.hasAttribute("disabled") || el.getAttribute("aria-disabled") === "true";
  }

  // 兼容控制台的显式反馈接口，避免同步资源后打断页面初始化。
  function createSpin(size) {
    var spin = document.createElement('span');
    spin.className = 'ant-spin ant-spin-spinning' + (size ? ' ant-spin-' + size : '');
    spin.setAttribute('role', 'status');
    spin.setAttribute('aria-label', '加载中');
    var dot = document.createElement('span');
    dot.className = 'ant-spin-dot ant-spin-dot-spin';
    for (var i = 0; i < 4; i++) {
      var item = document.createElement('i');
      item.className = 'ant-spin-dot-item';
      dot.appendChild(item);
    }
    spin.appendChild(dot);
    return spin;
  }

  function setNestedSpin(host, on) {
    if (!host) return;
    host.classList.toggle('is-spinning', !!on);
    host.setAttribute('aria-busy', on ? 'true' : 'false');
    var spin = host.querySelector(':scope > .ant-spin');
    if (on && !spin) host.insertBefore(createSpin(), host.firstChild);
    if (!on && spin) spin.remove();
  }

  function mount(scope) {
    (scope || document).querySelectorAll('[data-af-placeholder-spin]').forEach(function (el) {
      if (el.getAttribute('data-af-spin-done') !== '1' && !el.querySelector('.ant-spin')) {
        el.insertBefore(createSpin(el.getAttribute('data-af-placeholder-spin') || 'sm'), el.firstChild);
      }
    });
    (scope || document).querySelectorAll('.ant-spin-nested-loading.is-spinning').forEach(function (el) {
      setNestedSpin(el, true);
    });
  }

  function notification(type, opts) {
    opts = opts || {};
    var holder = document.querySelector('.ant-notification');
    if (!holder) {
      holder = document.createElement('div');
      holder.className = 'ant-notification';
      document.body.appendChild(holder);
    }
    var notice = document.createElement('div');
    notice.className = 'ant-notification-notice ant-notification-notice-' + type;
    notice.setAttribute('role', type === 'error' ? 'alert' : 'status');
    ['message', 'description'].forEach(function (part) {
      var text = document.createElement('div');
      text.className = 'ant-notification-notice-' + part;
      text.textContent = (part === 'message' ? opts.title : opts.description) || '';
      notice.appendChild(text);
    });
    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'ant-notification-notice-close ds-close';
    close.setAttribute('aria-label', '关闭');
    var closeIcon = loadingSvg();
    closeIcon.setAttribute('class', 'ai');
    closeIcon.querySelector('use').setAttribute('href', '#i-close');
    close.appendChild(closeIcon);
    close.addEventListener('click', function () { notice.remove(); });
    notice.appendChild(close);
    holder.appendChild(notice);
    if (opts.duration !== 0) setTimeout(function () { notice.remove(); }, opts.duration || 4500);
    return { close: function () { notice.remove(); } };
  }

  function flash(type, text) {
    try { sessionStorage.setItem('ant-feedback-flash', JSON.stringify({type: type, text: text})); }
    catch (_) { /* 隐私模式不影响后续操作 */ }
  }

  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form || form.nodeName !== "FORM" || form.hasAttribute("data-feedback-off")) return;
    var btn = e.submitter && e.submitter.tagName === "BUTTON" ? e.submitter : form.querySelector('button[type="submit"], button:not([type])');
    if (!btn) return;
    if (btn.getAttribute("data-loading") === "1") {
      e.preventDefault();
      return;
    }
    setLoading(btn);
    toast("loading", btn.getAttribute("data-loading-text") || "正在提交");
  }, true);

  document.addEventListener("click", function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var wrap = e.target.closest && e.target.closest("[data-blocked-text]");
    var nav = e.target.closest && e.target.closest("[data-nav]");
    if (wrap && (!nav || !wrap.contains(nav) || (nav && blocked(nav) && !nav.contains(e.target)))) {
      var ctrl = wrap.querySelector("[data-nav]");
      if (ctrl && blocked(ctrl)) {
        e.preventDefault();
        toast("error", wrap.getAttribute("data-blocked-text") || ctrl.getAttribute("data-error-text") || "暂时无法打开");
        return;
      }
    }
    if (!nav) return;
    if (nav.getAttribute("data-loading") === "1") {
      e.preventDefault();
      return;
    }
    if (blocked(nav)) {
      e.preventDefault();
      toast("error", nav.getAttribute("data-error-text") || "暂时无法打开");
      return;
    }
    setLoading(nav);
    if (nav.getAttribute("data-nav") === "blank") {
      var ok = nav.getAttribute("data-success-text");
      if (ok) toast("success", ok);
      setTimeout(function () { clearLoading(nav); }, 700);
    } else {
      var pending = nav.getAttribute("data-loading-text");
      if (pending) toast("loading", pending);
    }
  });

  var ok = document.querySelector("#flash-ok .ant-alert-content");
  var err = document.querySelector("#flash-err .ant-alert-content");
  if (ok) toast("success", ok.textContent);
  if (err) toast("error", err.textContent);

  function pageReady() {
    document.documentElement.classList.remove("ant-booting");
    var spin = document.getElementById("ant-page-spin");
    if (spin) {
      spin.setAttribute("aria-busy", "false");
      spin.setAttribute("aria-hidden", "true");
    }
  }
  function bootPage() {
    pageReady();
    mount(document);
    try {
      var pending = sessionStorage.getItem('ant-feedback-flash');
      sessionStorage.removeItem('ant-feedback-flash');
      if (pending) {
        pending = JSON.parse(pending);
        toast(pending.type || 'success', pending.text);
      }
    } catch (_) { /* 忽略损坏的跨页提示 */ }
    document.querySelectorAll('[data-af-toast]').forEach(function (el) {
      var type = el.getAttribute('data-af-toast') || 'info';
      var text = el.getAttribute('data-af-toast-text');
      if (el.getAttribute('data-af-toast-mode') === 'notification') {
        notification(type, {title: el.getAttribute('data-af-toast-title'), description: text});
      } else if (text) toast(type, text);
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bootPage);
  else bootPage();

  window.AntFeedback = window.AntFeedback || {
    mount: mount,
    createSpin: createSpin,
    setNestedSpin: setNestedSpin,
    setButtonLoading: function (el, on) { if (on) setLoading(el); else clearLoading(el); },
    flash: flash,
    notification: {
      success: function (opts) { return notification('success', opts); },
      error: function (opts) { return notification('error', opts); },
      info: function (opts) { return notification('info', opts); },
      warning: function (opts) { return notification('warning', opts); }
    },
    message: {
      success: function (t, _ms, _k) { return toast("success", t); },
      error: function (t, _ms, _k) { return toast("error", t); },
      info: function (t, _ms, _k) { return toast("info", t); },
      warning: function (t, _ms, _k) { return toast("warning", t); },
      loading: function (t) { return toast("loading", t); },
      close: function () {}
    },
    button: { loading: function (el, on) { if (on) setLoading(el); else clearLoading(el); } },
    pageReady: pageReady
  };
  window.MheroFeedback = { toast: toast, loading: setLoading, clear: clearLoading, pageReady: pageReady };
})();
