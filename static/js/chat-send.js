// Chat reply boxes (support/_composer.html, <form data-chat-send>) send without waiting: the
// message shows at once in the thread's [data-chat-pending] (support/_pending.html), muted, and
// the box is cleared for the next one. Messages go one at a time, in order. When the panel
// answers, the thread's messages are replaced by the fresh ones, a screenshot among them; a
// message that failed stays with "Not sent" and a retry button, its text kept. Each message
// carries a token, so a retry after a lost answer is not saved twice
// (apps/support/services/sending.py). Without scripts the form posts and the page reloads.
(() => {
  const queues = new WeakMap(); // [data-chat-pending] → { busy, items }
  const items = new WeakMap(); // message card → its item

  const newToken = () =>
    crypto.randomUUID?.() ??
    Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) =>
      byte.toString(16).padStart(2, "0"),
    ).join("");

  const scrollDown = (list) => {
    const area = list.closest("[data-chat-scroll]");
    if (area) area.scrollTop = area.scrollHeight;
  };

  // A muted card like the thread's own messages, from the thread's <template>.
  const makeCard = (template, text, file, internal) => {
    const card = template.content.firstElementChild.cloneNode(true);
    const { ownClass = "", internalClass, fileLabel } = template.dataset;
    const classes = (internal && internalClass) || ownClass;
    card.classList.add(...classes.split(" ").filter(Boolean));
    const textLine = card.querySelector("[data-pending-text]");
    textLine.textContent = text;
    textLine.hidden = !text.trim();
    if (file) {
      const fileLine = card.querySelector("[data-pending-file]");
      fileLine.textContent = `${fileLabel}: ${file.name}`;
      fileLine.hidden = false;
    }
    return card;
  };

  // The box is ready for the next message; templates and the internal note start over too.
  const clear = (form) => {
    form.reset();
    const field = form.elements.text;
    field.value = "";
    field.dispatchEvent(new Event("input", { bubbles: true })); // autogrow (ui.js)
    form.elements.image?.dispatchEvent(new Event("change", { bubbles: true })); // file name
    field.focus();
  };

  const showSending = (item) => {
    delete item.card.dataset.failed;
    item.card.querySelector("[data-pending-state]").textContent = item.sendingLabel;
    item.card.querySelector("[data-pending-failed]").hidden = true;
  };

  const showFailed = (item, error) => {
    item.card.dataset.failed = "";
    item.card.querySelector("[data-pending-state]").textContent = item.failedLabel;
    item.card.querySelector("[data-pending-error]").textContent = error;
    item.card.querySelector("[data-pending-failed]").hidden = false;
  };

  // As an HTMX swap, so the new part is set up and the swap events carry their usual details.
  const replace = (element, html) => {
    const eventInfo = { elt: element, target: element };
    htmx.swap(element, html, { swapStyle: "outerHTML" }, { eventInfo });
  };

  // The panel answers with the whole thread (support/_thread.html or inbox/_thread.html).
  // Errors of its form mean the message was not saved. Without a form the request has been
  // closed: the thread is replaced as a whole. Otherwise only the messages are, so text typed
  // meanwhile stays in the box.
  const apply = (item, html) => {
    const page = new DOMParser().parseFromString(html, "text/html");
    const fresh = page.querySelector("[data-chat-messages]");
    if (!fresh) return showFailed(item, "");
    const form = page.getElementById(item.formId);
    const errors = [...(form?.querySelectorAll(".error-text") ?? [])]
      .map((error) => error.textContent.trim())
      .filter(Boolean);
    if (errors.length) return showFailed(item, errors.join(" "));
    const list = item.card.parentElement;
    const chat = list?.closest(".chat");
    item.card.remove();
    if (!chat) return;
    if (!form) {
      replace(chat, page.querySelector(".chat")?.outerHTML ?? "");
      return;
    }
    replace(chat.querySelector("[data-chat-messages]"), fresh.outerHTML);
    scrollDown(list);
  };

  const send = async (item) => {
    showSending(item);
    try {
      const response = await fetch(item.url, {
        method: "POST",
        body: item.data,
        headers: { "HX-Request": "true" },
        credentials: "same-origin",
      });
      if (!response.ok) return showFailed(item, "");
      apply(item, await response.text());
    } catch {
      showFailed(item, "");
    }
  };

  const enqueue = async (list, item) => {
    const queue = queues.get(list) ?? { busy: false, items: [] };
    queues.set(list, queue);
    queue.items.push(item);
    if (queue.busy) return;
    queue.busy = true;
    while (queue.items.length) await send(queue.items.shift());
    queue.busy = false;
  };

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!form.matches?.("form[data-chat-send]") || !window.htmx || !window.fetch) return;
    const chat = form.closest(".chat");
    const list = chat?.querySelector("[data-chat-pending]");
    const template = chat?.querySelector("template[data-chat-pending-message]");
    if (!list || !template) return;
    event.preventDefault();
    const text = form.elements.text.value;
    const file = form.elements.image?.files[0];
    if (!text.trim() && !file) return;
    const data = new FormData(form);
    data.set("token", newToken());
    const card = makeCard(template, text, file, form.elements.internal?.checked);
    const item = {
      card,
      data,
      url: form.action,
      formId: form.id,
      sendingLabel: card.querySelector("[data-pending-state]").textContent,
      failedLabel: template.dataset.failed,
    };
    items.set(card, item);
    list.append(card);
    clear(form);
    scrollDown(list);
    enqueue(list, item);
  });

  // A failed message is sent again with the same token; a message on its way is not.
  document.addEventListener("click", (event) => {
    const card = event.target.closest?.("[data-pending-retry]")?.closest(".chat-pending");
    const item = card && items.get(card);
    if (!item || !card.hasAttribute("data-failed")) return;
    showSending(item);
    enqueue(card.parentElement, item);
  });
})();
