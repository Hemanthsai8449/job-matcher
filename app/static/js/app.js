(() => {
  "use strict";

  document.documentElement.classList.replace("no-js", "js");
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const qs = (selector, root = document) => root.querySelector(selector);
  const qsa = (selector, root = document) => [...root.querySelectorAll(selector)];

  const toastRegion = qs("#toast-region");
  const showToast = (message) => {
    if (!toastRegion || !message) return;
    const item = document.createElement("div");
    item.className = "toast";
    item.setAttribute("role", "status");
    item.textContent = message;
    toastRegion.append(item);
    window.setTimeout(() => item.remove(), 4200);
  };
  window.JobMatcher = { toast: showToast };

  const header = qs("[data-site-header]");
  if (header) {
    const updateHeader = () => header.classList.toggle("is-scrolled", window.scrollY > 8);
    updateHeader();
    window.addEventListener("scroll", updateHeader, { passive: true });
  }

  const navToggle = qs("[data-nav-toggle]");
  const navigation = qs("[data-navigation]");
  if (navToggle && navigation) {
    const openIcon = qs("[data-open-icon]", navToggle);
    const closeIcon = qs("[data-close-icon]", navToggle);
    const setNavigation = (open) => {
      navigation.classList.toggle("is-open", open);
      navToggle.setAttribute("aria-expanded", String(open));
      navToggle.setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
      if (openIcon) openIcon.hidden = open;
      if (closeIcon) closeIcon.hidden = !open;
    };
    navToggle.addEventListener("click", () => setNavigation(!navigation.classList.contains("is-open")));
    navigation.addEventListener("click", (event) => {
      if (event.target.closest("a")) setNavigation(false);
    });
    document.addEventListener("click", (event) => {
      if (!navigation.contains(event.target) && !navToggle.contains(event.target)) setNavigation(false);
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") setNavigation(false);
    });
  }

  const reveals = qsa(".reveal");
  if (reveals.length) {
    if (reducedMotion || !("IntersectionObserver" in window)) {
      reveals.forEach((item) => item.classList.add("is-visible"));
    } else {
      const observer = new IntersectionObserver((entries, currentObserver) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          entry.target.classList.add("is-visible");
          currentObserver.unobserve(entry.target);
        });
      }, { rootMargin: "0px 0px -8%", threshold: 0.08 });
      reveals.forEach((item) => observer.observe(item));
    }
  }

  const dismissFlash = (flash) => {
    if (!flash || flash.dataset.dismissing) return;
    flash.dataset.dismissing = "true";
    flash.classList.add("is-leaving");
    window.setTimeout(() => flash.remove(), reducedMotion ? 0 : 280);
  };
  qsa("[data-dismiss]").forEach((button) => button.addEventListener("click", () => dismissFlash(button.closest(".flash"))));
  qsa(".flash--success, .flash--info").forEach((flash) => window.setTimeout(() => dismissFlash(flash), 8000));

  qsa("[data-password-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      const input = qs("input", button.closest(".input-wrap"));
      if (!input) return;
      const showing = input.type === "text";
      input.type = showing ? "password" : "text";
      button.setAttribute("aria-pressed", String(!showing));
      button.setAttribute("aria-label", showing ? "Show password" : "Hide password");
      input.focus({ preventScroll: true });
    });
  });

  qsa("[data-range]").forEach((range) => {
    const output = qs("[data-range-output]", range.closest(".range-field"));
    const update = () => { if (output) output.textContent = `${range.value}%`; };
    update();
    range.addEventListener("input", update);
  });

  qsa("[data-tag-editor]").forEach((editor) => {
    const list = qs("[data-tags]", editor);
    const input = qs(".tag-input-row input", editor);
    const addButton = qs("[data-add-tag]", editor);
    if (!list || !input || !addButton) return;
    const values = () => qsa('input[name="skills"]', list).map((field) => field.value.toLocaleLowerCase());
    const addTag = () => {
      const value = input.value.trim().replace(/\s+/g, " ").slice(0, 80);
      if (!value) return;
      if (values().includes(value.toLocaleLowerCase())) {
        showToast("That skill is already listed.");
        input.select();
        return;
      }
      const chip = document.createElement("span");
      chip.className = "chip chip--editable";
      chip.append(document.createTextNode(value));
      const remove = document.createElement("button");
      remove.type = "button";
      remove.dataset.removeTag = "";
      remove.setAttribute("aria-label", `Remove ${value}`);
      remove.innerHTML = "&times;";
      const hidden = document.createElement("input");
      hidden.type = "hidden";
      hidden.name = "skills";
      hidden.value = value;
      chip.append(remove, hidden);
      list.append(chip);
      input.value = "";
      input.focus();
    };
    addButton.addEventListener("click", addTag);
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === ",") {
        event.preventDefault();
        addTag();
      }
    });
    editor.addEventListener("click", (event) => {
      const remove = event.target.closest("[data-remove-tag]");
      if (remove) remove.closest(".chip")?.remove();
    });
  });

  const otpInput = qs("[data-otp-form] .otp-input");
  if (otpInput) {
    const sanitiseOtp = () => { otpInput.value = otpInput.value.replace(/\D/g, "").slice(0, 6); };
    otpInput.addEventListener("input", sanitiseOtp);
    otpInput.addEventListener("paste", () => window.setTimeout(sanitiseOtp));
  }
  qsa("[data-resend-form]").forEach((form) => {
    const button = qs("[data-resend-button]", form);
    const label = qs("[data-resend-countdown]", form);
    let remaining = Number.parseInt(form.dataset.resendSeconds || "0", 10);
    if (!button || !label || remaining <= 0) return;
    const tick = () => {
      if (remaining <= 0) {
        button.disabled = false;
        label.textContent = "";
        return;
      }
      button.disabled = true;
      label.textContent = `in ${remaining}s`;
      remaining -= 1;
      window.setTimeout(tick, 1000);
    };
    tick();
  });

  const dialogOpeners = new WeakMap();
  const closeDialog = (dialog) => {
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
    dialogOpeners.get(dialog)?.focus({ preventScroll: true });
  };
  qsa("[data-dialog-open]").forEach((button) => {
    button.addEventListener("click", () => {
      const dialog = document.getElementById(button.dataset.dialogOpen);
      if (!dialog) return;
      dialogOpeners.set(dialog, button);
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
      qs("input:not([type='hidden']), select, textarea, button", dialog)?.focus();
    });
  });
  qsa("[data-dialog]").forEach((dialog) => {
    if (!dialog.hasAttribute("aria-label") && !dialog.hasAttribute("aria-labelledby")) {
      const heading = qs("h1, h2, h3", dialog);
      if (heading) {
        heading.id ||= `${dialog.id || "job-matcher-dialog"}-title`;
        dialog.setAttribute("aria-labelledby", heading.id);
      }
    }
    qsa("[data-dialog-close]", dialog).forEach((button) => button.addEventListener("click", () => closeDialog(dialog)));
    dialog.addEventListener("click", (event) => { if (event.target === dialog) closeDialog(dialog); });
  });

  qsa("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  qsa("[data-auto-submit]").forEach((control) => {
    control.addEventListener("change", () => {
      control.setAttribute("aria-busy", "true");
      const form = control.form;
      if (form?.requestSubmit) form.requestSubmit();
      else form?.submit();
    });
  });

  qsa("[data-apply-link][data-expires-at]").forEach((link) => {
    const expiry = Date.parse(link.dataset.expiresAt || "");
    if (!Number.isFinite(expiry) || expiry > Date.now()) return;
    link.removeAttribute("href");
    link.removeAttribute("target");
    link.setAttribute("aria-disabled", "true");
    link.classList.remove("button--primary");
    link.classList.add("button--secondary");
    link.textContent = "Listing expired";
  });

  const filterToggle = qs("[data-filter-toggle]");
  const filterPanel = qs("[data-filter-panel]");
  const filterBackdrop = qs("[data-filter-backdrop]");
  if (filterToggle && filterPanel) {
    const setFilters = (open) => {
      filterPanel.classList.toggle("is-open", open);
      filterToggle.setAttribute("aria-expanded", String(open));
      if (filterBackdrop) filterBackdrop.hidden = !open;
      document.body.style.overflow = open ? "hidden" : "";
      if (open) qs("input, select, button", filterPanel)?.focus();
    };
    filterToggle.addEventListener("click", () => setFilters(true));
    qs("[data-filter-close]", filterPanel)?.addEventListener("click", () => setFilters(false));
    filterBackdrop?.addEventListener("click", () => setFilters(false));
    document.addEventListener("keydown", (event) => { if (event.key === "Escape") setFilters(false); });
  }

  qsa("[data-go-back]").forEach((button) => button.addEventListener("click", (event) => {
    event.preventDefault();
    if (window.history.length > 1) window.history.back();
    else window.location.assign("/");
  }));

  qsa("form").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (event.defaultPrevented) return;
      const submit = qs('button[type="submit"]', form);
      if (!submit || submit.dataset.noLock !== undefined) return;
      window.requestAnimationFrame(() => {
        if (event.defaultPrevented) return;
        submit.disabled = true;
        submit.setAttribute("aria-busy", "true");
      });
    });
  });
})();
