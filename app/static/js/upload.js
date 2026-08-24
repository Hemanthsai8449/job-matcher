(() => {
  "use strict";
  const form = document.querySelector("[data-upload-form]");
  if (!form) return;
  const zone = form.querySelector("[data-drop-zone]");
  const input = form.querySelector("[data-file-input]");
  const submit = form.querySelector("[data-upload-submit]");
  const defaultState = form.querySelector("[data-upload-default]");
  const selectedState = form.querySelector("[data-upload-selected]");
  const fileName = form.querySelector("[data-file-name]");
  const fileSize = form.querySelector("[data-file-size]");
  const maximumSize = 10 * 1024 * 1024;
  if (!zone || !input || !submit) return;

  const formatSize = (bytes) => bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  const update = () => {
    const file = input.files?.[0];
    if (!file) {
      submit.disabled = true;
      if (defaultState) defaultState.hidden = false;
      if (selectedState) selectedState.hidden = true;
      return;
    }
    if (file.size > maximumSize) {
      input.value = "";
      window.JobMatcher?.toast("Choose a resume file that is 10 MB or smaller.");
      update();
      return;
    }
    submit.disabled = false;
    if (defaultState) defaultState.hidden = true;
    if (selectedState) selectedState.hidden = false;
    if (fileName) fileName.textContent = file.name;
    if (fileSize) fileSize.textContent = `${formatSize(file.size)} - ready to upload`;
  };
  input.addEventListener("change", update);
  ["dragenter", "dragover"].forEach((name) => zone.addEventListener(name, (event) => {
    event.preventDefault();
    zone.classList.add("is-dragging");
  }));
  ["dragleave", "drop"].forEach((name) => zone.addEventListener(name, (event) => {
    event.preventDefault();
    zone.classList.remove("is-dragging");
  }));
  zone.addEventListener("drop", (event) => {
    const files = event.dataTransfer?.files;
    if (!files?.length) return;
    try {
      const transfer = new DataTransfer();
      transfer.items.add(files[0]);
      input.files = transfer.files;
      update();
    } catch (_error) {
      input.click();
    }
  });
  update();
})();
