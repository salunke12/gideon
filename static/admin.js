(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const els = {
    chip: $("#user-chip"), avatar: $("#avatar"), who: $("#who"),
    logout: $("#logout"), logoutDialog: $("#logout-dialog"),
    total: $("#stat-total"), active: $("#stat-active"), disabled: $("#stat-disabled"), noKey: $("#stat-nokey"),
    search: $("#search"), add: $("#add-user"),
    rows: $("#user-rows"), empty: $("#empty-rows"), listError: $("#list-error"),
    dialog: $("#user-dialog"), form: $("#user-form"), title: $("#dialog-title"),
    username: $("#f-username"), first: $("#f-first"), role: $("#f-role"), status: $("#f-status"),
    statusField: $("#status-field"), key: $("#f-key"), clearKey: $("#f-clear-key"), clearKeyField: $("#clear-key-field"),
    password: $("#f-password"), reset: $("#f-reset"), resetField: $("#reset-field"), attempts: $("#attempts-note"),
    formError: $("#form-error"), actions: $("#dialog-actions"),
    deleteUser: $("#delete-user"), cancel: $("#cancel"), save: $("#save"),
    deleteConfirm: $("#delete-confirm"), deleteText: $("#delete-text"),
    keepUser: $("#keep-user"), confirmDelete: $("#confirm-delete"),
  };

  const MAX_ATTEMPTS = 5;
  const state = { users: [], me: null, editing: null, saving: false };

  /* ---------- talking to the server ---------- */

  async function api(method, url, body) {
    try {
      const res = await fetch(url, {
        method,
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
      if (res.status === 401) { location.href = "/login"; return { ok: false, data: {} }; }
      if (res.status === 403) { location.href = "/"; return { ok: false, data: {} }; }
      return { ok: res.ok, data: await res.json().catch(() => ({})) };
    } catch {
      return { ok: false, data: { error: "Can't reach the server. Check that server.py is running." } };
    }
  }

  /* ---------- the user table ---------- */

  const when = (iso) => (iso
    ? new Date(iso).toLocaleString([], { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" })
    : "Never");

  function cell(...children) {
    const td = document.createElement("td");
    td.append(...children);
    return td;
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function badge(text, kind) {
    return el("span", `badge ${kind || ""}`.trim(), text);
  }

  function renderStats() {
    const users = state.users;
    els.total.textContent = users.length;
    els.active.textContent = users.filter((u) => u.status === "active").length;
    els.disabled.textContent = users.filter((u) => u.status === "disabled").length;
    els.noKey.textContent = users.filter((u) => !u.has_key).length;
  }

  function renderRows() {
    const q = els.search.value.trim().toLowerCase();
    const shown = state.users.filter((u) => !q || u.username.toLowerCase().includes(q) || u.first_name.toLowerCase().includes(q));
    els.rows.replaceChildren();

    for (const u of shown) {
      const tr = document.createElement("tr");

      const name = el("div", "u-name", u.username);
      const sub = el("div", "u-sub", u.id === state.me ? `${u.first_name} (you)` : u.first_name);

      const status = u.status === "active" ? badge("Active", "badge-ok") : badge("Disabled", "badge-off");
      const statusCell = cell(status);
      if (u.status === "active" && u.attempts_left < MAX_ATTEMPTS) {
        statusCell.append(el("span", "cell-note", `${u.attempts_left} login attempt${u.attempts_left === 1 ? "" : "s"} left`));
      }

      let keyNode;
      if (u.has_key) keyNode = el("span", "key-hint", u.key_hint);
      else keyNode = badge("No key", "badge-warn");

      const edit = el("button", "ghost small", "Edit");
      edit.type = "button";
      edit.setAttribute("aria-label", `Edit ${u.username}`);
      edit.addEventListener("click", () => openDialog(u));

      tr.append(
        cell(name, sub),
        cell(u.role === "admin" ? badge("Admin", "badge-admin") : badge("User")),
        statusCell,
        cell(keyNode),
        cell(when(u.last_login_at)),
        cell(edit),
      );
      els.rows.appendChild(tr);
    }

    els.empty.hidden = shown.length > 0;
    els.empty.textContent = state.users.length ? "No users match your search." : "No users yet.";
  }

  async function loadUsers() {
    const { ok, data } = await api("GET", "/api/admin/users");
    if (!ok) {
      els.listError.textContent = data.error || "Couldn't load the users.";
      els.listError.hidden = false;
      els.empty.textContent = "";
      return;
    }
    els.listError.hidden = true;
    state.users = data.users;
    state.me = data.me;
    renderStats();
    renderRows();
  }

  els.search.addEventListener("input", renderRows);

  /* ---------- add / edit dialog ---------- */

  function showFormError(text) {
    els.formError.textContent = text;
    els.formError.hidden = !text;
  }

  function showActions() {
    els.deleteConfirm.hidden = true;
    els.actions.hidden = false;
  }

  function openDialog(user) {
    const creating = !user;
    const self = !creating && user.id === state.me;
    state.editing = user;
    showFormError("");
    showActions();
    els.form.reset();

    els.title.textContent = creating ? "Add user" : `Edit ${user.username}`;
    els.username.readOnly = !creating;
    els.username.value = creating ? "" : user.username;
    els.first.value = creating ? "" : user.first_name;
    els.first.placeholder = creating ? "Leave blank to use the username" : "";
    els.role.value = creating ? "user" : user.role;
    els.status.value = creating ? "active" : user.status;
    els.role.disabled = self;
    els.status.disabled = self;
    els.statusField.hidden = creating;

    els.key.placeholder = !creating && user.has_key ? `Saved: ${user.key_hint}. Leave blank to keep it.` : "Paste a Gemini API key";
    els.clearKeyField.hidden = creating || !user.has_key;

    els.password.placeholder = creating ? "At least 8 characters" : "Leave blank to keep the current password";

    const showReset = !creating && user.status === "active" && user.attempts_left < MAX_ATTEMPTS;
    els.resetField.hidden = !showReset;
    els.attempts.hidden = creating;
    if (!creating) {
      els.attempts.textContent = self
        ? "You can't change your own role or status."
        : `${user.attempts_left} of ${MAX_ATTEMPTS} login attempts left. Turning a disabled user back to Active restores all ${MAX_ATTEMPTS}.`;
    }

    els.deleteUser.hidden = creating || self;
    els.dialog.showModal();
    (creating ? els.username : els.first).focus();
  }

  els.add.addEventListener("click", () => openDialog(null));
  els.cancel.addEventListener("click", () => els.dialog.close());

  els.form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (state.saving) return;
    const user = state.editing;
    const creating = !user;
    const self = !creating && user.id === state.me;

    const body = { first_name: els.first.value.trim() };
    if (creating) body.username = els.username.value.trim();
    if (!self) body.role = els.role.value;
    if (!creating && !self) body.status = els.status.value;
    if (els.key.value.trim()) body.api_key = els.key.value.trim();
    if (els.clearKey.checked && !els.key.value.trim()) body.clear_api_key = true;
    if (creating || els.password.value) body.password = els.password.value;
    if (els.reset.checked) body.reset_attempts = true;

    state.saving = true;
    els.save.disabled = true;
    showFormError("");
    const { ok, data } = creating
      ? await api("POST", "/api/admin/users", body)
      : await api("PUT", `/api/admin/users/${user.id}`, body);
    state.saving = false;
    els.save.disabled = false;

    if (!ok) {
      showFormError(data.error || "Couldn't save. Try again.");
      return;
    }
    els.dialog.close();
    await loadUsers();
  });

  /* ---------- delete a user ---------- */

  els.deleteUser.addEventListener("click", () => {
    els.deleteText.textContent =
      `Delete ${state.editing.username}? Their saved chats and timers are deleted too. This can't be undone.`;
    els.actions.hidden = true;
    els.deleteConfirm.hidden = false;
  });
  els.keepUser.addEventListener("click", showActions);

  els.confirmDelete.addEventListener("click", async () => {
    els.confirmDelete.disabled = true;
    const { ok, data } = await api("DELETE", `/api/admin/users/${state.editing.id}`);
    els.confirmDelete.disabled = false;
    if (!ok) {
      showFormError(data.error || "Couldn't delete that user.");
      showActions();
      return;
    }
    els.dialog.close();
    await loadUsers();
  });

  /* ---------- account ---------- */

  fetch("/api/me")
    .then((res) => {
      if (res.status === 401) { location.href = "/login"; return null; }
      return res.json();
    })
    .then((me) => {
      if (!me) return;
      if (me.role !== "admin") { location.href = "/"; return; }
      els.who.textContent = me.first_name || me.username;
      els.avatar.textContent = (me.first_name || me.username).charAt(0).toUpperCase();
      els.chip.hidden = false;
    })
    .catch(() => {});

  els.logout.addEventListener("click", () => els.logoutDialog.showModal());
  els.logoutDialog.addEventListener("close", async () => {
    if (els.logoutDialog.returnValue !== "logout") return;
    try { await fetch("/api/logout", { method: "POST" }); } catch { /* go to the login page anyway */ }
    location.href = "/login";
  });

  loadUsers();
})();