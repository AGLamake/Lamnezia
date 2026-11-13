// Interface behaviour shared by all pages. Loaded before Alpine (CSP build), so the components
// below are registered before Alpine starts. Templates may use only simple expressions
// (`open = !open`, `copy()`); anything else lives here. Components: docs/wiki/*/Development.md.
(() => {
  const FOCUSABLE =
    'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), ' +
    'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
  const TOAST_MS = 6000;

  const focusFirst = (root) => root?.querySelector(FOCUSABLE)?.focus();

  // ---- Alpine components -------------------------------------------------------------------

  // Navigation drawer on phones: x-data="drawer", the button has x-ref="toggle",
  // the panel x-ref="panel". Escape closes it and focus goes back to the button.
  const drawer = () => ({
    open: false,
    toggle() {
      this.open ? this.close() : this.show();
    },
    show() {
      this.open = true;
      this.$nextTick(() => focusFirst(this.$refs.panel));
    },
    close() {
      if (!this.open) return;
      this.open = false;
      this.$refs.toggle?.focus();
    },
    expanded() {
      return this.open ? "true" : "false";
    },
  });

  // Drop-down menu (WAI-ARIA menu button): x-data="menu", the button x-ref="button",
  // the list x-ref="items" with role="menu" and items role="menuitem…".
  const menu = () => ({
    open: false,
    toggle() {
      this.open ? this.close(false) : this.show();
    },
    show() {
      this.open = true;
      this.$nextTick(() => this.items()[0]?.focus());
    },
    close(refocus = true) {
      if (!this.open) return;
      this.open = false;
      if (refocus) this.$refs.button?.focus();
    },
    closeOutside() {
      this.close(false);
    },
    expanded() {
      return this.open ? "true" : "false";
    },
    items() {
      const all = this.$refs.items?.querySelectorAll('[role^="menuitem"]') || [];
      return [...all].filter((item) => item.offsetParent !== null);
    },
    move(event) {
      const items = this.items();
      if (!items.length) return;
      const index = items.indexOf(document.activeElement);
      const keys = { ArrowDown: index + 1, ArrowUp: index - 1, Home: 0, End: items.length - 1 };
      if (!(event.key in keys)) return;
      event.preventDefault();
      items[(keys[event.key] + items.length) % items.length].focus();
    },
  });

  // Appearance menu: theme (light, dark or system) and page width (normal or wide);
  // static/js/theme.js applies and remembers both.
  const themeMenu = () => ({
    ...menu(),
    choice: window.lamneziaTheme?.choice() || "system",
    width: window.lamneziaWidth?.choice() || "normal",
    choose(value) {
      this.choice = value;
      window.lamneziaTheme?.set(value);
      this.close();
    },
    chooseWidth(value) {
      this.width = value;
      window.lamneziaWidth?.set(value);
      this.close();
    },
    checked(value) {
      return this.choice === value ? "true" : "false";
    },
    widthChecked(value) {
      return this.width === value ? "true" : "false";
    },
    is(value) {
      return this.choice === value;
    },
  });

  // Copy and share buttons: x-data="copy", the text is in x-ref="source" (an input or textarea).
  // The share button (`x-show="canShare"`, `@click="share"`) opens the system share sheet
  // (Web Share API) and stays hidden where the browser has none.
  const copy = () => ({
    copied: false,
    canShare: typeof navigator.share === "function",
    async share() {
      try {
        await navigator.share({ text: this.$refs.source.value });
      } catch (error) {
        // Cancelled by the user or refused by the browser: nothing to do.
      }
    },
    async copy() {
      const source = this.$refs.source;
      try {
        await navigator.clipboard.writeText(source.value);
      } catch (error) {
        source.select();
        document.execCommand("copy");
      }
      this.copied = true;
      setTimeout(() => (this.copied = false), 2500);
    },
  });

  // Bar chart tooltips (components/bar_chart.html): hover shows one through CSS, a tap pins it
  // until another tap or a tap outside the chart.
  const barChart = () => ({
    active: null,
    pick(index) {
      this.active = this.active === index ? null : index;
    },
    clear() {
      this.active = null;
    },
    state(index) {
      return this.active === index ? "true" : "false";
    },
  });

  // One toast: success and info hide after a while (paused under the pointer or focus),
  // warnings and errors stay until closed.
  const toast = () => ({
    shown: true,
    timer: null,
    init() {
      if (this.$el.dataset.sticky === undefined) this.resume();
    },
    pause() {
      clearTimeout(this.timer);
    },
    resume() {
      if (this.$el.dataset.sticky !== undefined) return;
      clearTimeout(this.timer);
      this.timer = setTimeout(() => this.dismiss(), TOAST_MS);
    },
    dismiss() {
      this.shown = false;
      setTimeout(() => this.$el.remove(), 300);
    },
  });

  document.addEventListener("alpine:init", () => {
    const Alpine = window.Alpine;
    Alpine.data("drawer", drawer);
    Alpine.data("menu", menu);
    Alpine.data("themeMenu", themeMenu);
    Alpine.data("copy", copy);
    Alpine.data("toast", toast);
    Alpine.data("barChart", barChart);
  });

  // ---- Toasts from scripts and HTMX (`HX-Trigger: {"toast": {"level": …, "text": …}}`) -------

  function showToast(level, text) {
    const template = document.getElementById("toast-template");
    const region = document.getElementById("toasts");
    if (!template || !region) return;
    const node = template.content.firstElementChild.cloneNode(true);
    node.classList.add(`alert-${level}`);
    node.querySelector("[data-toast-text]").textContent = text;
    if (level === "error" || level === "warning") {
      node.dataset.sticky = "";
      node.setAttribute("role", "alert");
    }
    region.append(node);
  }
  window.lamneziaToast = showToast;
  document.addEventListener("toast", (event) => {
    showToast(event.detail?.level || "info", event.detail?.text || "");
  });
  // <button data-toast="success" data-toast-text="Saved.">: a toast without a request.
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-toast]");
    if (button) showToast(button.dataset.toast, button.dataset.toastText || "");
  });

  // ---- Dialogs: native <dialog> gives focus trapping, Escape and an inert page -------------

  const openDialog = (dialog) => {
    if (dialog && !dialog.open) dialog.showModal();
  };

  document.addEventListener("click", (event) => {
    const opener = event.target.closest("[data-dialog-open]");
    if (opener) openDialog(document.getElementById(opener.dataset.dialogOpen));
    const closer = event.target.closest("[data-dialog-close]");
    if (closer) closer.closest("dialog")?.close();
    // A click on the backdrop lands on the <dialog> itself.
    if (event.target instanceof HTMLDialogElement && event.target.open) {
      const box = event.target.getBoundingClientRect();
      const inside =
        event.clientX >= box.left &&
        event.clientX <= box.right &&
        event.clientY >= box.top &&
        event.clientY <= box.bottom;
      if (!inside) event.target.close();
    }
  });

  // HTMX loads modal fragments into #modal (components/modal.html): show them, clear on close.
  document.addEventListener("htmx:afterSwap", (event) => {
    if (event.detail.target.id !== "modal") return;
    const dialog = event.detail.target.querySelector("dialog");
    if (!dialog) return;
    dialog.addEventListener("close", () => dialog.remove(), { once: true });
    openDialog(dialog);
  });

  // <dialog data-dialog-lazy> with a [data-dialog-content] that HTMX loads from the opener (the chat
  // widget). It loads once and stays after closing, so opening it again is instant: the opener's
  // request is skipped and the parts marked [data-dialog-refresh] fetch what changed. While the
  // dialog is closed nothing inside it sends a request, so nothing in it is marked seen.

  const lazyContent = (element) => {
    const dialog = element?.closest?.("dialog[data-dialog-lazy]");
    return dialog ? dialog.querySelector("[data-dialog-content]") : null;
  };

  const lazyDialogOf = (opener) => {
    const dialog = document.getElementById(opener?.dataset.dialogOpen || "");
    return dialog?.matches("[data-dialog-lazy]") ? dialog : null;
  };

  document.addEventListener("htmx:confirm", (event) => {
    const element = event.detail.elt;
    const content = lazyContent(element);
    const asleep = content && !content.closest("dialog").open;
    const loaded = lazyDialogOf(element)?.querySelector("[data-dialog-content]")?.hasChildNodes();
    if (asleep || loaded) event.preventDefault();
  });

  document.addEventListener("click", (event) => {
    const dialog = lazyDialogOf(event.target.closest("[data-dialog-open]"));
    const content = dialog?.querySelector("[data-dialog-content]");
    if (!content?.hasChildNodes()) return;
    content.querySelectorAll("[data-dialog-refresh]").forEach((part) => {
      part.dispatchEvent(new Event("dialog:refresh"));
    });
    content.querySelector("textarea")?.focus();
  });

  // The text field of a lazy dialog gets the focus when the content loads and when a swap took
  // the focus away.
  document.addEventListener("htmx:afterSettle", (event) => {
    document
      .querySelectorAll("dialog[data-dialog-lazy][open] [data-dialog-content]")
      .forEach((content) => {
        if (event.detail.target === content || document.activeElement === document.body) {
          content.querySelector("textarea")?.focus();
        }
      });
  });

  // ---- Chats: [data-chat-scroll] holds the messages, the reply box stays under it ----------
  // A new scroll area starts at the bottom. A swap later follows new messages down only when
  // the area was at the bottom already or one sent a message: reading the history is not
  // interrupted. Areas are told apart by id, since a swap may replace them.

  const NEAR_BOTTOM_PX = 48;
  const settledChats = new WeakSet();
  const followedChats = new Set();

  document.addEventListener("htmx:beforeSwap", (event) => {
    const target = event.detail.target;
    const sent = event.detail.requestConfig?.verb === "post";
    document.querySelectorAll("[data-chat-scroll]").forEach((area) => {
      if (!area.contains(target) && !target.contains(area)) return;
      const atBottom = area.scrollHeight - area.scrollTop - area.clientHeight < NEAR_BOTTOM_PX;
      if (sent || atBottom) followedChats.add(area.id);
    });
  });

  const settleChats = () => {
    document.querySelectorAll("[data-chat-scroll]").forEach((area) => {
      if (!settledChats.has(area) || followedChats.has(area.id)) area.scrollTop = area.scrollHeight;
      settledChats.add(area);
    });
    followedChats.clear();
  };
  document.addEventListener("htmx:afterSettle", settleChats);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", settleChats);
  else settleChats();

  // <textarea data-autogrow> grows with its text up to its CSS max-height.
  const autogrow = (field) => {
    field.style.height = "auto";
    field.style.height = `${field.scrollHeight + field.offsetHeight - field.clientHeight}px`;
  };
  document.addEventListener("input", (event) => {
    if (event.target.matches?.("textarea[data-autogrow]")) autogrow(event.target);
  });

  // <textarea data-enter-send>: Enter sends the form, Shift+Enter starts a new line. Not on touch
  // screens, whose keyboards have no Shift+Enter, and not while an input method composes a word.
  const touchScreen = window.matchMedia("(pointer: coarse)");
  document.addEventListener("keydown", (event) => {
    const field = event.target;
    if (event.key !== "Enter" || event.shiftKey || event.isComposing || touchScreen.matches) return;
    if (!field.matches?.("textarea[data-enter-send]") || !field.form) return;
    event.preventDefault();
    field.form.requestSubmit();
  });

  // A link to #<id> of a dialog opens it as its opener does (the old chat address leads to
  // #support-chat). On "load": HTMX has set up the opener by then.
  window.addEventListener("load", () => {
    const id = location.hash.slice(1);
    const opener = id && document.querySelector(`[data-dialog-open="${CSS.escape(id)}"]`);
    if (!opener) return;
    history.replaceState(null, "", location.pathname + location.search);
    opener.click();
  });

  // ---- Command palette (components/command_palette.html) -----------------------------------
  // Ctrl+K or ⌘K opens it with the search field focused; arrows walk the field and the results,
  // Enter in the field opens the first result.

  const palette = () => document.querySelector("[data-palette]");

  document.addEventListener("keydown", (event) => {
    const dialog = palette();
    if (!dialog) return;
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      openDialog(dialog);
      dialog.querySelector("[data-palette-input]")?.select();
      return;
    }
    if (!dialog.open) return;
    const first = dialog.querySelector("[data-palette-item]");
    if (event.key === "Enter" && event.target.matches("[data-palette-input]")) {
      event.preventDefault();
      first?.click();
      return;
    }
    if (!["ArrowDown", "ArrowUp"].includes(event.key)) return;
    const stops = [
      dialog.querySelector("[data-palette-input]"),
      ...dialog.querySelectorAll("[data-palette-item]"),
    ];
    const index = stops.indexOf(document.activeElement);
    if (index < 0) return;
    event.preventDefault();
    const step = event.key === "ArrowDown" ? 1 : -1;
    stops[(index + step + stops.length) % stops.length].focus();
  });

  // ---- Confirmation of dangerous actions ---------------------------------------------------
  // <form data-confirm="Revoke the key?"> (or data-confirm on the submit button) asks first.
  // data-confirm-button names the action on the confirm button; it is red by default.

  function askConfirm(question, action, done) {
    const dialog = document.getElementById("confirm-dialog");
    if (!dialog || typeof dialog.showModal !== "function") {
      if (window.confirm(question)) done();
      return;
    }
    const accept = dialog.querySelector("[data-confirm-accept]");
    dialog.querySelector("[data-confirm-text]").textContent = question;
    accept.textContent = action || accept.dataset.defaultLabel;
    dialog.returnValue = "";
    dialog.addEventListener(
      "close",
      () => {
        if (dialog.returnValue === "confirm") done();
      },
      { once: true },
    );
    dialog.showModal();
    // The safe choice gets the focus, so Enter never confirms by accident.
    dialog.querySelector("[data-confirm-cancel]")?.focus();
  }

  document.addEventListener(
    "submit",
    (event) => {
      const form = event.target;
      const submitter = event.submitter;
      const question = submitter?.dataset.confirm || form.dataset.confirm;
      if (!question) return;
      if (form.dataset.confirmed) {
        delete form.dataset.confirmed;
        return;
      }
      event.preventDefault();
      event.stopImmediatePropagation();
      const action = submitter?.dataset.confirmButton || form.dataset.confirmButton;
      askConfirm(question, action, () => {
        form.dataset.confirmed = "1";
        form.requestSubmit(submitter || undefined);
      });
    },
    true,
  );

  // ---- Folding blocks ----------------------------------------------------------------------
  // <details data-open-wide>: folded on phones, opened on screens from 640 px (list filters).

  const openWide = () => {
    if (!window.matchMedia("(min-width: 40rem)").matches) return;
    document.querySelectorAll("details[data-open-wide]").forEach((details) => {
      details.open = true;
    });
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", openWide);
  else openWide();

  // ---- Loading cues ------------------------------------------------------------------------
  // While a request is on its way, <html data-loading> shows the bar at the top of the page
  // (CSS waits a moment, so fast answers do not blink) and the button that sent it spins.

  const BUSY_MAX_MS = 15000;
  let pending = 0;
  let loadingTimer = null;
  const setLoading = (on) => {
    clearTimeout(loadingTimer);
    if (!on) {
      delete document.documentElement.dataset.loading;
      return;
    }
    document.documentElement.dataset.loading = "";
    // A link that downloads a file leaves the page in place: the bar goes away by itself.
    loadingTimer = setTimeout(() => {
      if (!pending) setLoading(false);
    }, BUSY_MAX_MS);
  };

  const busy = (button) => {
    if (!button?.matches?.(".btn") || button.getAttribute("aria-busy") === "true") return;
    button.setAttribute("aria-busy", "true");
    // A download or a refused answer leaves the page as it was: the button comes back anyway.
    setTimeout(() => idle(button), BUSY_MAX_MS);
  };
  const idle = (button) => button?.removeAttribute("aria-busy");

  const sender = (element, event) =>
    event?.submitter || (element?.matches?.("form") ? element.querySelector("[type=submit]") : element);

  // An ordinary form that leaves the page: the bar and the button until the next page arrives.
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (event.defaultPrevented || form.hasAttribute("hx-post") || form.target) return;
    busy(sender(form, event));
    setLoading(true);
  });
  // A link to another page of the panel.
  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[href]");
    if (!link || event.defaultPrevented || event.button || event.ctrlKey || event.metaKey) return;
    if (link.target || link.hasAttribute("download") || link.origin !== location.origin) return;
    if (link.pathname === location.pathname && link.search === location.search) return;
    setLoading(true);
  });
  // Coming back with the Back button restores the page as it was left: nothing is loading.
  window.addEventListener("pageshow", () => {
    setLoading(false);
    document.querySelectorAll('.btn[aria-busy="true"]').forEach(idle);
  });

  // HTMX requests the user started. Polling and live updates come without a user's event and
  // stay quiet.
  const quiet = (event) => !event.detail.requestConfig?.triggeringEvent?.isTrusted;
  document.addEventListener("htmx:beforeRequest", (event) => {
    const element = event.detail.elt;
    if (quiet(event)) return;
    pending += 1;
    setLoading(true);
    busy(sender(element, event.detail.requestConfig?.triggeringEvent));
    // <form data-optimistic="remove">: its target goes at once and comes back if the request fails.
    if (element.dataset?.optimistic === "remove") event.detail.target.hidden = true;
  });
  document.addEventListener("htmx:afterRequest", (event) => {
    const element = event.detail.elt;
    if (quiet(event)) return;
    pending = Math.max(0, pending - 1);
    if (!pending) setLoading(false);
    idle(sender(element, event.detail.requestConfig?.triggeringEvent));
    if (element.dataset?.optimistic === "remove" && !event.detail.successful) {
      event.detail.target.hidden = false;
    }
  });

  // ---- Installed app -----------------------------------------------------------------------
  // The service worker (/sw.js) shows the offline page when the panel cannot be reached.

  if ("serviceWorker" in navigator && window.isSecureContext) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/sw.js").catch(() => {});
    });
  }

  // ---- Small form helpers ------------------------------------------------------------------

  document.addEventListener("change", (event) => {
    const field = event.target;
    // <form data-autosubmit>: filters that apply on change.
    if (field.form?.hasAttribute("data-autosubmit")) field.form.requestSubmit();
    // <select data-fill="text">: copies the chosen value into the form's `text` field.
    const target = field.dataset?.fill && field.form?.elements[field.dataset.fill];
    if (target && field.value) {
      target.value = field.value;
      target.dispatchEvent(new Event("input", { bubbles: true }));
      target.focus();
    }
    // A file input shows the chosen file's name in the form's [data-file-name].
    const fileName = field.type === "file" && field.form?.querySelector("[data-file-name]");
    if (fileName) {
      fileName.textContent = field.files[0]?.name || "";
      fileName.hidden = !field.files.length;
    }
    // <input type="checkbox" data-check-all="users">: ticks every input[name=users] of the form.
    const group = field.dataset?.checkAll;
    if (group && field.form) {
      field.form.querySelectorAll(`input[name="${CSS.escape(group)}"]`).forEach((box) => {
        box.checked = field.checked;
      });
    }
  });
})();
