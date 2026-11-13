// Markdown preview of FAQ articles in the admin: a button under each text field asks the panel
// to render the text as the public page will, cleaned the same way. Nothing is saved.
(() => {
  function attach(field) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "button";
    button.textContent = field.dataset.previewLabel;
    const output = document.createElement("div");
    output.hidden = true;
    output.style.cssText = "margin-top: 8px; padding: 8px 12px; border: 1px solid var(--hairline-color, #ccc); max-width: 60em;";
    button.addEventListener("click", () => {
      const token = field.form.querySelector("[name=csrfmiddlewaretoken]").value;
      fetch(field.dataset.markdownPreview, {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRFToken": token },
        body: new URLSearchParams({ text: field.value }),
      })
        .then((response) => (response.ok ? response.text() : Promise.reject(response.status)))
        .then((html) => {
          output.innerHTML = html;
        })
        .catch(() => {
          output.textContent = field.dataset.previewError;
        })
        .finally(() => {
          output.hidden = false;
        });
    });
    field.after(button, output);
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-markdown-preview]").forEach(attach);
  });
})();
