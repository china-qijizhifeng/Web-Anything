(function cloudPostApplication() {
  "use strict";

  const siteId = "cloudpost";
  const app = document.querySelector("#app");
  let runtime;
  let searchText = "";

  function sitePrefix() {
    const tokens = window.location.pathname.split("/").filter(Boolean);
    if (tokens[0] === "p" && /^\d+$/.test(tokens[1] || "")) {
      return ["", tokens[0], tokens[1]].join("/");
    }
    const siteIndex = tokens.indexOf("sites");
    if (siteIndex >= 0 && tokens[siteIndex + 1] === siteId) {
      return "/" + tokens.slice(0, siteIndex + 2).join("/");
    }
    return "";
  }

  function resolveUrl(path) {
    return sitePrefix() + (path.startsWith("/") ? path : "/" + path);
  }

  function logicalPath() {
    const prefix = sitePrefix();
    const current = window.location.pathname;
    if (prefix && current.startsWith(prefix)) return current.slice(prefix.length) || "/";
    return current || "/";
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function navigate(path, replace) {
    const target = resolveUrl(path);
    window.history[replace ? "replaceState" : "pushState"]({}, "", target);
    render();
  }

  function shell(content, routeName) {
    const state = runtime.getState();
    const unread = Object.values(state.messages).filter(message => message.folder === "inbox" && !message.read).length;
    return `
      <div class="app-shell" data-current-route="${escapeHtml(routeName)}">
        <header class="topbar">
          <button class="brand" data-nav="/inbox" aria-label="CloudPost inbox">
            <span class="brand-mark">C</span><span>CloudPost</span>
          </button>
          <label class="search-box">
            <span aria-hidden="true">⌕</span>
            <input id="mail-search" value="${escapeHtml(searchText)}" placeholder="Search synthetic mail" autocomplete="off">
          </label>
          <div class="top-actions">
            <span class="fixed-time" title="Benchmark fixed time">${escapeHtml(runtime.getClock().iso.slice(0, 16).replace("T", " "))} UTC</span>
            <button class="icon-button" data-task-action="reset" title="Reset benchmark">↺</button>
            <button class="avatar" data-nav="/login" aria-label="Demo account">MC</button>
          </div>
        </header>
        <div class="workspace">
          <aside class="sidebar">
            <button class="compose-button" data-nav="/compose"><span>＋</span> Compose</button>
            <nav aria-label="Mail folders">
              <button class="nav-item ${routeName === "inbox" ? "active" : ""}" data-nav="/inbox"><span>▱</span> Inbox <strong>${unread}</strong></button>
              <button class="nav-item"><span>★</span> Starred</button>
              <button class="nav-item"><span>◷</span> Snoozed</button>
              <button class="nav-item"><span>➤</span> Sent</button>
            </nav>
            <div class="sidebar-spacer"></div>
            <button class="nav-item ${routeName === "settings" ? "active" : ""}" data-nav="/settings"><span>⚙</span> Settings</button>
            <div class="demo-note">Local demo · fixed data</div>
          </aside>
          <main class="main-panel">${content}</main>
        </div>
      </div>`;
  }

  function inboxView() {
    const state = runtime.getState();
    const normalized = searchText.trim().toLowerCase();
    const rows = state.message_order
      .map(id => state.messages[id])
      .filter(Boolean)
      .filter(message => message.folder === "inbox")
      .filter(message => !normalized || [message.sender_name, message.subject, message.preview].join(" ").toLowerCase().includes(normalized))
      .map(message => `
        <article class="mail-row ${message.read ? "read" : "unread"}" data-object-id="${escapeHtml(message.id)}">
          <button class="star ${message.starred ? "selected" : ""}" data-task-action="star-${escapeHtml(message.id)}" aria-label="${message.starred ? "Unstar" : "Star"} ${escapeHtml(message.subject)}">${message.starred ? "★" : "☆"}</button>
          <button class="message-link" data-nav="/message/${escapeHtml(message.id)}">
            <span class="sender">${escapeHtml(message.sender_name)}</span>
            <span class="subject">${escapeHtml(message.subject)}</span>
            <span class="preview">— ${escapeHtml(message.preview)}</span>
            <time>${escapeHtml(message.received_label)}</time>
          </button>
        </article>`).join("");
    return shell(`
      <section data-route="inbox">
        <div class="panel-toolbar">
          <div><button class="icon-button" aria-label="Select all">□</button><button class="icon-button" aria-label="Refresh">↻</button></div>
          <span>${rows ? "1–4 of 4" : "No matches"}</span>
        </div>
        <div class="category-tabs"><button class="active">Primary</button><button>Projects</button><button>Updates</button></div>
        <div class="mail-list">${rows || '<div class="empty-state">No synthetic messages match this search.</div>'}</div>
      </section>`, "inbox");
  }

  function messageView(messageId) {
    const state = runtime.getState();
    const message = state.messages[messageId];
    if (!message) return shell('<section data-route="message"><div class="empty-state">Message not found.</div></section>', "message");
    return shell(`
      <article class="message-detail" data-route="message" data-object-id="${escapeHtml(message.id)}">
        <div class="panel-toolbar"><button class="icon-button" data-nav="/inbox" aria-label="Back to inbox">←</button><div><button class="icon-button">⌂</button><button class="icon-button">⋮</button></div></div>
        <div class="message-content">
          <h1>${escapeHtml(message.subject)}</h1>
          <div class="message-meta"><span class="avatar large">${escapeHtml(message.sender_name.split(" ").map(part => part[0]).join(""))}</span><div><strong>${escapeHtml(message.sender_name)}</strong><small>${escapeHtml(message.sender_address)} · to Mira</small></div><time>${escapeHtml(message.received_label)}</time></div>
          <p>${escapeHtml(message.body)}</p>
          <div class="message-actions"><button>↩ Reply</button><button>⇥ Forward</button></div>
        </div>
      </article>`, "message");
  }

  function composeView() {
    return shell(`
      <section class="compose-view" data-route="compose">
        <div class="compose-card">
          <div class="compose-title"><h1>New message</h1><button class="icon-button" data-nav="/inbox" aria-label="Close compose">×</button></div>
          <label><span>To</span><input id="compose-to" autocomplete="off" placeholder="recipient@example.test"></label>
          <label><span>Subject</span><input id="compose-subject" autocomplete="off" placeholder="Subject"></label>
          <textarea id="compose-body" aria-label="Message body" placeholder="Write a synthetic message"></textarea>
          <div class="compose-actions"><button class="send-button" data-task-action="send-message">Send</button><span>All data stays in this browser.</span></div>
        </div>
      </section>`, "compose");
  }

  function settingsView() {
    const state = runtime.getState();
    return shell(`
      <section class="settings-view" data-route="settings">
        <h1>CloudPost settings</h1>
        <div class="setting-card" data-setting-id="compact_mode"><div><strong>Compact message rows</strong><p>Use a denser inbox layout.</p></div><input id="compact-mode" type="checkbox" ${state.settings.compact_mode ? "checked" : ""}></div>
        <div class="setting-card"><div><strong>Signature</strong><p>${escapeHtml(state.settings.signature)}</p></div><button>Edit</button></div>
        <div class="setting-card"><div><strong>Benchmark state</strong><p>Restore the fixed fixtures and demo account.</p></div><button data-task-action="reset">Reset benchmark</button></div>
      </section>`, "settings");
  }

  function loginView() {
    const state = runtime.getState();
    const user = state.users.usr_demo_001;
    return `
      <main class="login-page" data-route="login">
        <section class="login-card" data-user-id="usr_demo_001">
          <div class="brand-mark large-brand">C</div>
          <p class="eyebrow">LOCAL SYNTHETIC ACCOUNT</p>
          <h1>Welcome to CloudPost</h1>
          <p>Use the fixed demo account. No password or network connection is required.</p>
          <div class="account-row"><span class="avatar">MC</span><div><strong>${escapeHtml(user.name)}</strong><small>${escapeHtml(user.address)}</small></div></div>
          <button class="send-button wide" data-task-action="demo-login">Continue to inbox</button>
          <button class="text-button" data-task-action="reset">Reset benchmark first</button>
        </section>
      </main>`;
  }

  function render() {
    const path = logicalPath();
    if (path === "/login") app.innerHTML = loginView();
    else if (path === "/compose") app.innerHTML = composeView();
    else if (path === "/settings") app.innerHTML = settingsView();
    else if (path.startsWith("/message/")) app.innerHTML = messageView(path.split("/").pop());
    else app.innerHTML = inboxView();
  }

  function handleClick(event) {
    const nav = event.target.closest("[data-nav]");
    if (nav) {
      navigate(nav.dataset.nav, false);
      return;
    }
    const action = event.target.closest("[data-task-action]");
    if (!action) return;
    if (action.dataset.taskAction === "reset") {
      runtime.reset();
      render();
      return;
    }
    if (action.dataset.taskAction === "demo-login") {
      runtime.update(state => {
        state.auth = { authenticated: true, user_id: "usr_demo_001", role: "member" };
      });
      navigate("/inbox", true);
      return;
    }
    if (action.dataset.taskAction.startsWith("star-")) {
      const messageId = action.dataset.taskAction.slice(5);
      runtime.update(state => {
        if (state.messages[messageId]) state.messages[messageId].starred = !state.messages[messageId].starred;
      });
      return;
    }
    if (action.dataset.taskAction === "send-message") {
      const to = document.querySelector("#compose-to").value.trim();
      const subject = document.querySelector("#compose-subject").value.trim();
      const body = document.querySelector("#compose-body").value.trim();
      if (!to || !subject || !body) return;
      runtime.update(state => {
        state.messages.msg_sent_001 = {
          id: "msg_sent_001",
          sender_id: "usr_demo_001",
          sender_name: "Mira Chen",
          sender_address: "mira@cloudpost.example",
          recipient_address: to,
          subject,
          preview: body,
          body,
          sent_at: runtime.getClock().iso,
          folder: "sent",
          read: true,
          starred: false
        };
      });
      navigate("/inbox", true);
    }
  }

  async function initialize() {
    const [contract, initialState] = await Promise.all([
      fetch(resolveUrl("/synthetic_contract.json")).then(response => response.json()),
      fetch(resolveUrl("/fixtures/initial-state.json")).then(response => response.json())
    ]);
    runtime = window.WebAnythingRuntime.create({
      namespace: contract.storage.namespace,
      schemaVersion: contract.storage.schema_version,
      fixedTime: contract.clock.fixed_time,
      randomSeed: contract.random.seed,
      initialState,
      initialUrl: contract.reset.initial_url,
      resolveUrl
    });
    window.addEventListener("webanything:state", render);
    window.addEventListener("webanything:route", render);
    window.addEventListener("popstate", render);
    app.addEventListener("click", handleClick);
    app.addEventListener("input", event => {
      if (event.target.id === "mail-search") {
        searchText = event.target.value;
        render();
        const search = document.querySelector("#mail-search");
        search.focus();
        search.setSelectionRange(search.value.length, search.value.length);
      }
    });
    app.addEventListener("change", event => {
      if (event.target.id === "compact-mode") {
        runtime.update(state => { state.settings.compact_mode = event.target.checked; });
      }
    });
    if (logicalPath() === "/") navigate(contract.reset.initial_url, true);
    else render();
  }

  initialize().catch(error => {
    app.innerHTML = `<main class="fatal-error"><h1>CloudPost could not start</h1><p>${escapeHtml(error.message)}</p></main>`;
  });
})();
