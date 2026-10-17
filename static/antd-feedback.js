/* 事故车提醒控制台 — Ant Design 5.29 默认主题交互反馈。
   只用 Button loading、Spin、message、notification。不改主题、不引入动画库。 */
(function () {
  "use strict";

  if (!window.React || !window.ReactDOM || !window.antd) {
    console.error("Ant Design 资源未加载，交互反馈不可用");
    return;
  }

  var React = window.React;
  var ReactDOM = window.ReactDOM;
  var antd = window.antd;
  var h = React.createElement;

  var feedback = {
    ready: false,
    message: antd.message,
    notification: antd.notification,
    modal: antd.Modal,
  };

  function Bridge() {
    var app = antd.App.useApp();
    React.useLayoutEffect(function () {
      feedback.message = app.message;
      feedback.notification = app.notification;
      feedback.modal = app.modal;
      feedback.ready = true;
    });
    return null;
  }

  var holderHost = document.createElement("div");
  holderHost.id = "antd-feedback-holder";
  document.body.appendChild(holderHost);
  ReactDOM.flushSync(function () {
    ReactDOM.createRoot(holderHost).render(h(antd.App, null, h(Bridge)));
  });

  var pageSpin = { spinning: false, tip: "加载中" };
  var pageSpinRoot = null;

  function renderPageSpin() {
    if (!pageSpinRoot) {
      var host = document.createElement("div");
      host.id = "antd-page-spin";
      document.body.appendChild(host);
      pageSpinRoot = ReactDOM.createRoot(host);
    }
    ReactDOM.flushSync(function () {
      pageSpinRoot.render(h(antd.Spin, {
        fullscreen: true,
        spinning: pageSpin.spinning,
        tip: pageSpin.tip,
        size: "large",
      }));
    });
  }

  function showPageSpin(tip) {
    pageSpin.spinning = true;
    if (tip) pageSpin.tip = tip;
    renderPageSpin();
  }

  function hidePageSpin() {
    pageSpin.spinning = false;
    if (pageSpinRoot) renderPageSpin();
  }

  function buttonVisual(el) {
    var danger = el.classList.contains("btn-danger");
    var outline = el.classList.contains("btn-outline");
    return {
      type: outline && !danger ? "default" : "primary",
      danger: danger,
      size: el.classList.contains("btn-sm") ? "small" : "middle",
    };
  }

  function mountButton(el, onClick) {
    var label = (el.textContent || "").replace(/\s+/g, " ").trim();
    var visual = buttonVisual(el);
    var mount = document.createElement("span");
    mount.className = "antd-btn-host";
    el.replaceWith(mount);
    var root = ReactDOM.createRoot(mount);
    var state = { loading: false };
    var longLabel = label.length > 16;
    var api = {
      _busy: false,
      setLoading: function (value) {
        state.loading = !!value;
        ReactDOM.flushSync(render);
      },
    };

    function render() {
      root.render(h(antd.Button, {
        type: visual.type,
        danger: visual.danger,
        size: visual.size,
        loading: state.loading,
        htmlType: "button",
        autoInsertSpace: false,
        style: longLabel ? { height: "auto", whiteSpace: "normal", textAlign: "left" } : undefined,
        onClick: onClick,
      }, label));
    }

    ReactDOM.flushSync(render);
    return api;
  }

  function mountInlineSpin(el, size) {
    if (!el || el._antdSpinRoot) return el && el._antdSpinRoot;
    var root = ReactDOM.createRoot(el);
    root.render(h(antd.Spin, { size: size || "small" }));
    el._antdSpinRoot = root;
    return root;
  }

  function postJson(url, body) {
    return fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Accept": "application/json",
      },
      body: JSON.stringify(body || {}),
    }).then(function (response) {
      return response.json().catch(function () {
        return {};
      }).then(function (data) {
        if (!response.ok) {
          var error = new Error((data && data.error) || "请求失败");
          error.status = response.status;
          error.payload = data;
          throw error;
        }
        return data;
      });
    }).catch(function (error) {
      if (error && error.status) throw error;
      if (error instanceof TypeError) throw new Error("网络异常，请稍后重试");
      throw error;
    });
  }

  function runAction(api, work, options) {
    options = options || {};
    if (!api || api._busy) return Promise.resolve(null);
    api._busy = true;
    api.setLoading(true);
    var key = options.key || ("antd-action-" + Date.now());
    feedback.message.loading({
      content: options.loading || "处理中…",
      duration: 0,
      key: key,
    });
    var navigating = false;
    return Promise.resolve()
      .then(work)
      .then(function (result) {
        if (options.success) {
          feedback.message.success({ content: options.success, duration: 2, key: key });
        } else {
          feedback.message.destroy(key);
        }
        if (options.reload) {
          navigating = true;
          var delay = Number(options.reloadDelay || 0);
          window.setTimeout(function () {
            showPageSpin(options.reloadTip || "正在刷新…");
            window.location.reload();
          }, delay);
        }
        return result;
      })
      .catch(function (error) {
        var text = (error && error.message) || "操作失败";
        if (error && error.level === "warning") {
          feedback.message.warning({ content: text, duration: 3, key: key });
        } else {
          feedback.message.error({ content: text, duration: 3, key: key });
        }
        return null;
      })
      .then(function (result) {
        if (!navigating) {
          api._busy = false;
          api.setLoading(false);
        }
        return result;
      });
  }

  function confirmDialog(opts) {
    opts = opts || {};
    feedback.modal.confirm({
      title: opts.title || "请确认",
      content: opts.content || "",
      okText: opts.okText || "确认",
      cancelText: opts.cancelText || "取消",
      okButtonProps: opts.danger ? { danger: true } : undefined,
      onOk: opts.onOk,
      onCancel: opts.onCancel,
    });
  }

  function notify(opts) {
    opts = opts || {};
    var type = opts.type || "info";
    var api = feedback.notification;
    var fn = api[type] || api.open || api.info;
    fn({
      message: opts.message || "",
      description: opts.description || "",
      duration: opts.duration == null ? 4 : opts.duration,
    });
  }

  function readActionBody(button, bodyName, filename) {
    var body = {};
    if (filename != null) body.filename = filename;
    if (bodyName && typeof window[bodyName] === "function") {
      var extra = window[bodyName]() || {};
      Object.keys(extra).forEach(function (key) {
        body[key] = extra[key];
      });
    }
    return body;
  }

  function enhancePosts(root) {
    root.querySelectorAll("[data-antd-post]").forEach(function (button) {
      if (button.dataset.antdBound === "1") return;
      button.dataset.antdBound = "1";
      var url = button.getAttribute("data-antd-post");
      var loading = button.getAttribute("data-antd-loading") || "处理中…";
      var success = button.getAttribute("data-antd-success") || "已完成";
      var reload = button.getAttribute("data-antd-reload") === "1";
      var reloadDelay = Number(button.getAttribute("data-antd-reload-delay") || 0);
      var confirmText = button.getAttribute("data-antd-confirm") || "";
      var bodyName = button.getAttribute("data-antd-body") || "";
      var filename = button.hasAttribute("data-antd-filename")
        ? button.getAttribute("data-antd-filename")
        : null;
      var danger = button.classList.contains("btn-danger");
      var api;
      function exec() {
        return runAction(api, function () {
          return postJson(url, readActionBody(button, bodyName, filename)).then(function (data) {
            if (data && Object.prototype.hasOwnProperty.call(data, "started") && !data.started) {
              var busy = new Error(data.error || "已有任务在执行");
              busy.level = "warning";
              throw busy;
            }
            return data;
          });
        }, {
          loading: loading,
          success: success,
          reload: reload,
          reloadDelay: reloadDelay,
        });
      }
      api = mountButton(button, function () {
        if (confirmText) {
          confirmDialog({ content: confirmText, danger: danger, onOk: exec });
          return;
        }
        exec();
      });
    });
  }

  function enhanceForms(root) {
    root.querySelectorAll("form").forEach(function (form) {
      var method = (form.getAttribute("method") || "get").toLowerCase();
      if (method !== "post" || form.dataset.antdBound === "1") return;
      var submitBtn = form.querySelector('button[type="submit"], button:not([type])');
      if (!submitBtn) return;
      form.dataset.antdBound = "1";
      var loadingText = form.getAttribute("data-antd-loading") || "提交中…";
      var confirmText = form.getAttribute("data-antd-confirm") || "";
      var danger = submitBtn.classList.contains("btn-danger");
      var api;
      function startSubmit() {
        if (form.dataset.antdSubmitting === "1" || (api && api._busy)) return;
        var go = function () {
          if (typeof form.reportValidity === "function" && !form.reportValidity()) return;
          api._busy = true;
          api.setLoading(true);
          feedback.message.loading({ content: loadingText, key: "antd-form", duration: 0 });
          form.dataset.antdSubmitting = "1";
          HTMLFormElement.prototype.submit.call(form);
        };
        if (confirmText) {
          confirmDialog({ content: confirmText, danger: danger, onOk: go });
        } else {
          go();
        }
      }
      api = mountButton(submitBtn, function () {
        startSubmit();
      });
      form.addEventListener("submit", function (event) {
        if (form.dataset.antdSubmitting === "1") return;
        event.preventDefault();
        startSubmit();
      });
      form.removeAttribute("onsubmit");
    });
  }

  function bindDownload(anchor) {
    if (!anchor || anchor.dataset.antdBound === "1") return;
    anchor.dataset.antdBound = "1";
    var href = anchor.getAttribute("href");
    var filename = anchor.getAttribute("data-filename");
    if (!filename) {
      try {
        filename = decodeURIComponent((href || "").split("/").pop() || "download");
      } catch (error) {
        filename = "download";
      }
    }
    var api;
    api = mountButton(anchor, function () {
      runAction(api, function () {
        return fetch(href).then(function (response) {
          if (!response.ok) throw new Error("下载失败");
          return response.blob();
        }).then(function (blob) {
          var url = URL.createObjectURL(blob);
          var link = document.createElement("a");
          link.href = url;
          link.download = filename;
          document.body.appendChild(link);
          link.click();
          link.remove();
          URL.revokeObjectURL(url);
        });
      }, { loading: "正在下载…", success: "已开始下载" });
    });
  }

  function enhanceDownloads(root) {
    root.querySelectorAll('a[href^="/reports/download/"]').forEach(bindDownload);
  }

  function enhanceInlineSpins(root) {
    root.querySelectorAll("[data-inline-spin]").forEach(function (el) {
      mountInlineSpin(el, el.getAttribute("data-inline-spin") || "small");
    });
  }

  function onNavigate(event) {
    if (event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    var anchor = event.target && event.target.closest && event.target.closest("a[href]");
    if (!anchor || anchor.dataset.antdBound === "1") return;
    if (anchor.target && anchor.target !== "_self") return;
    var href = anchor.getAttribute("href") || "";
    if (!href || href.charAt(0) === "#" || href.indexOf("javascript:") === 0) return;
    var url;
    try {
      url = new URL(anchor.href, window.location.href);
    } catch (error) {
      return;
    }
    if (url.origin !== window.location.origin) return;
    if (url.pathname.indexOf("/reports/download/") === 0) return;
    showPageSpin("加载中");
  }

  enhancePosts(document);
  enhanceForms(document);
  enhanceDownloads(document);
  enhanceInlineSpins(document);
  document.addEventListener("click", onNavigate, true);
  document.body.dataset.antdFeedback = "ready";

  window.AntdFeedback = {
    showPageSpin: showPageSpin,
    hidePageSpin: hidePageSpin,
    mountButton: mountButton,
    mountInlineSpin: mountInlineSpin,
    postJson: postJson,
    runAction: runAction,
    confirm: confirmDialog,
    notify: notify,
    message: {
      success: function (content) { feedback.message.success(content); },
      error: function (content) { feedback.message.error(content); },
      warning: function (content) { feedback.message.warning(content); },
      info: function (content) { feedback.message.info(content); },
      loading: function (content) { return feedback.message.loading(content); },
      destroy: function (key) { feedback.message.destroy(key); },
    },
  };
})();
