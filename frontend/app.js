const $ = (id) => document.getElementById(id);

const state = {
  contacts: [],
  companies: [],
  selectedEmail: null,
  editingEmail: null,
  editingCompany: null,
  search: "",
  pitchSpamScore: 0,
  smtpProvider: "",
  smtpDefaultProvider: "zoho",
  auth: { enabled: false, email: "" },
  contactDocs: [],
  companyDocs: [],
  pendingCompanyDocs: [],
  documentLimits: { max: 10, maxFileMb: 5 },
};

let toastTimer = null;

/* ------------------------------------------------------------------ */
/* Helpers                                                             */
/* ------------------------------------------------------------------ */

const escapeHtml = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch])
  );

function notify(message, type = "success") {
  const toast = $("toast");
  toast.textContent = message;
  toast.className = `toast${type === "error" ? " error" : ""}`;
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => toast.classList.add("hidden"), 4500);
}

function showNotice(id, message, type = "info") {
  const notice = $(id);
  notice.textContent = message;
  notice.className = "notice";
  if (type === "error") notice.classList.add("error");
  if (type === "success") notice.classList.add("info");
  notice.classList.remove("hidden");
}

function hideNotice(id) {
  const notice = $(id);
  notice.textContent = "";
  notice.classList.add("hidden");
}

async function api(path, options = {}) {
  const config = { ...options };
  if (config.body && !(config.body instanceof FormData)) {
    config.headers = { "Content-Type": "application/json", ...(config.headers || {}) };
  }

  const response = await fetch(path, config);

  if (response.status === 401) {
    window.location.assign("/login");
    throw new Error("Your session expired. Please sign in again.");
  }

  let data = {};
  try {
    data = await response.json();
  } catch (error) {
    data = {};
  }

  if (!response.ok) {
    const requestLabel = `${(config.method || "GET").toUpperCase()} ${path.split("?")[0]}`;
    const message =
      data.detail || data.message || `Request failed (${response.status}) for ${requestLabel}.`;
    const requestError = new Error(message);
    requestError.status = response.status;
    requestError.data = data;
    throw requestError;
  }

  return data;
}

async function withBusy(button, label, task) {
  const original = button.innerHTML;
  button.disabled = true;
  button.textContent = label;
  try {
    return await task();
  } catch (error) {
    notify(error.message || "Something went wrong.", "error");
    return null;
  } finally {
    button.innerHTML = original;
    button.disabled = false;
  }
}

function setStatus(online) {
  $("status-dot").classList.toggle("ready", online);
  $("status-text").textContent = online ? "API online" : "API offline";
}

/* ------------------------------------------------------------------ */
/* SMTP provider                                                       */
/* ------------------------------------------------------------------ */

const SMTP_PROVIDER_LABELS = { zoho: "Zoho", gmail: "Gmail", custom: "Custom" };

function providerName(provider) {
  return SMTP_PROVIDER_LABELS[provider] || "Default";
}

function activeSmtpProvider() {
  return state.smtpProvider || state.smtpDefaultProvider;
}

function nextSmtpProvider() {
  return activeSmtpProvider() === "gmail" ? "zoho" : "gmail";
}

function updateSmtpToggle() {
  const active = activeSmtpProvider();
  const next = nextSmtpProvider();
  $("smtp-toggle-label").textContent = providerName(active);
  const button = $("smtp-toggle-button");
  button.title = `Switch SMTP provider to ${providerName(next)}`;
  button.setAttribute(
    "aria-label",
    `Switch SMTP provider from ${providerName(active)} to ${providerName(next)}`
  );
}

function setSmtpProvider(provider) {
  state.smtpProvider = provider;
  $("global-smtp-provider").value = provider;
  document.querySelectorAll("#pitch-smtp-provider").forEach((select) => {
    select.value = provider;
  });
  document.querySelectorAll("#pitch-smtp-user, #pitch-smtp-password").forEach((input) => {
    input.value = "";
  });
  updateSmtpToggle();
}

async function loadSmtpProvider() {
  try {
    const health = await api("/api/health");
    if (SMTP_PROVIDER_LABELS[health.smtp_provider]) {
      state.smtpDefaultProvider = health.smtp_provider;
    }
  } catch (error) {
    /* Keep the shipped default when health is unavailable. */
  }
  updateSmtpToggle();
}

/* ------------------------------------------------------------------ */
/* Deliverability                                                      */
/* ------------------------------------------------------------------ */

const DNS_CHECK_LABELS = { spf: "SPF", dkim: "DKIM", dmarc: "DMARC" };
const DNS_STATUS_LABELS = { pass: "Pass", missing: "Missing", error: "Lookup failed" };

function renderDeliverability(data) {
  const domain = data.domain || "";
  $("deliverability-subtitle").textContent = domain
    ? `${domain} · ${providerName(data.provider)} sending`
    : "SPF, DKIM, and DMARC for the active sending domain.";

  $("deliverability-results").innerHTML = ["spf", "dkim", "dmarc"]
    .map((key) => {
      const check = data[key] || {};
      const status = DNS_STATUS_LABELS[check.status] ? check.status : "error";
      const record = check.record
        ? `<code class="dns-record">${escapeHtml(check.record)}</code>`
        : "";
      return `
        <div class="dns-row ${status}">
          <div class="dns-row-head">
            <strong>${DNS_CHECK_LABELS[key]}</strong>
            <span class="dns-badge ${status}">${DNS_STATUS_LABELS[status]}</span>
          </div>
          <p class="dns-detail">${escapeHtml(check.detail || "No detail available.")}</p>
          ${record}
        </div>`;
    })
    .join("");

  const tips = Array.isArray(data.tips) ? data.tips : [];
  $("deliverability-tips").innerHTML = tips
    .map((tip) => `<li>${escapeHtml(tip)}</li>`)
    .join("");
}

async function checkDeliverability(forceRefresh = false) {
  $("deliverability-modal").classList.remove("hidden");
  $("deliverability-subtitle").textContent = "SPF, DKIM, and DMARC for the active sending domain.";
  $("deliverability-results").innerHTML = '<p class="muted small">Looking up SPF, DKIM, and DMARC…</p>';
  $("deliverability-tips").innerHTML = "";

  const button = $("refresh-deliverability-btn");
  button.disabled = true;
  try {
    const provider = activeSmtpProvider();
    const query = `provider=${encodeURIComponent(provider)}${forceRefresh ? "&refresh=1" : ""}`;
    const data = await api(`/api/deliverability?${query}`);
    renderDeliverability(data);
  } catch (error) {
    $("deliverability-results").innerHTML = `<div class="notice error">${escapeHtml(
      error.message || "DNS check failed."
    )}</div>`;
  } finally {
    button.disabled = false;
  }
}

function closeDeliverabilityModal() {
  $("deliverability-modal").classList.add("hidden");
}

/* ------------------------------------------------------------------ */
/* Confirmation modal                                                  */
/* ------------------------------------------------------------------ */

let confirmResolve = null;

function askConfirm({ title, message, icon = "✉", acceptLabel = "Yes, send", warn = false }) {
  if (confirmResolve) {
    const previous = confirmResolve;
    confirmResolve = null;
    previous(false);
  }

  $("confirm-modal-title").textContent = title;
  $("confirm-modal-message").textContent = message;
  $("confirm-modal-icon").textContent = icon;
  $("confirm-modal-icon").classList.toggle("warn", warn);
  $("confirm-accept-btn").textContent = acceptLabel;
  $("confirm-modal").classList.remove("hidden");
  $("confirm-accept-btn").focus();

  return new Promise((resolve) => {
    confirmResolve = resolve;
  });
}

function settleConfirm(result) {
  const modal = $("confirm-modal");
  if (modal.classList.contains("hidden")) return;
  modal.classList.add("hidden");
  const resolve = confirmResolve;
  confirmResolve = null;
  if (resolve) resolve(result);
}

/* ------------------------------------------------------------------ */
/* Auth                                                                */
/* ------------------------------------------------------------------ */

async function initAuth() {
  try {
    const me = await api("/api/auth/me");
    state.auth = { enabled: me.auth_enabled, email: me.email || "" };

    if (me.auth_enabled && !me.authenticated) {
      window.location.assign("/login");
      return false;
    }

    if (me.email) {
      $("account-email").textContent = me.email;
      $("account-avatar").textContent = me.email.charAt(0).toUpperCase();
    } else {
      $("account-block").classList.add("hidden");
      $("logout-button").classList.add("hidden");
    }
    return true;
  } catch (error) {
    notify("Could not verify your session. Refresh the page to try again.", "error");
    return false;
  }
}

/* ------------------------------------------------------------------ */
/* Contacts                                                            */
/* ------------------------------------------------------------------ */

function renderContacts() {
  const list = $("contacts-list");
  const query = state.search.trim().toLowerCase();
  const filtered = query
    ? state.contacts.filter((contact) =>
        [contact.name, contact.email, contact.company_name, contact.target_role, contact.target_industry]
          .filter(Boolean)
          .join(" ")
          .toLowerCase()
          .includes(query)
      )
    : state.contacts;

  $("contact-count").textContent =
    state.contacts.length === 1 ? "1 contact" : `${state.contacts.length} contacts`;

  if (!state.contacts.length) {
    list.innerHTML = '<div class="empty">No contacts yet.<br>Use “+ New” to add the first one.</div>';
    return;
  }

  if (!filtered.length) {
    list.innerHTML = `<div class="empty">No contacts match “${escapeHtml(state.search.trim())}”.</div>`;
    return;
  }

  list.innerHTML = filtered
    .map((contact) => {
      const selected = contact.email === state.selectedEmail ? " selected" : "";
      const meta = [contact.email, contact.target_role].filter(Boolean).join(" · ");
      const position = String(state.contacts.indexOf(contact) + 1).padStart(2, "0");
      return `
        <button type="button" class="contact${selected}" data-email="${escapeHtml(contact.email)}">
          <span class="contact-body">
            <span class="contact-name">${escapeHtml(contact.name || "Unnamed")}</span>
            <span class="contact-meta">${escapeHtml(meta)}</span>
          </span>
          <span class="contact-index">${position}</span>
        </button>`;
    })
    .join("");

  list.querySelectorAll(".contact").forEach((button) => {
    button.addEventListener("click", () => {
      const contact = state.contacts.find((item) => item.email === button.dataset.email);
      if (contact) selectContact(contact);
    });
  });
}

function selectContact(contact) {
  state.selectedEmail = contact.email;
  state.editingEmail = contact.email;

  $("pitch-name").value = contact.name || "";
  $("pitch-email").value = contact.email || "";
  $("pitch-phone").value = contact.phone || "";
  $("pitch-role").value = contact.target_role || "";
  $("pitch-industry").value = contact.target_industry || "";
  $("pitch-details").value = contact.details || contact.context || "";
  setCompanySelection(contact.company_name || "");
  loadContactDocs();
  loadCompanyDocs();

  $("name").value = contact.name || "";
  $("email").value = contact.email || "";
  $("context").value = contact.context || contact.details || "";

  $("pitch-save-button").textContent = "Save changes";
  $("remove-contact-button").disabled = false;

  invalidatePitchPreview();
  invalidateStandardPreview();
  hideNotice("pitch-verify-result");
  hideNotice("verify-result");
  renderContacts();

  if (window.innerWidth < 860) {
    $("pitch-section").scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

function startNewContact() {
  state.selectedEmail = null;
  state.editingEmail = null;

  [
    "pitch-name",
    "pitch-email",
    "pitch-phone",
    "pitch-role",
    "pitch-industry",
    "pitch-details",
    "name",
    "email",
    "context",
  ].forEach((id) => {
    $(id).value = "";
  });

  $("pitch-save-button").textContent = "Save contact";
  $("remove-contact-button").disabled = true;

  invalidatePitchPreview();
  invalidateStandardPreview();
  hideNotice("pitch-verify-result");
  hideNotice("verify-result");
  state.contactDocs = [];
  renderDocumentList("contact-doc-list", [], "contact", "");
  updateContactDocControls();
  renderContacts();
  showTab("pitch");
  $("pitch-name").focus();
}

async function loadContacts() {
  const data = await api("/api/contacts");
  state.contacts = data.contacts || [];
  if (state.selectedEmail && !state.contacts.some((c) => c.email === state.selectedEmail)) {
    state.selectedEmail = null;
  }
  renderContacts();
  return data;
}

async function savePitchContact() {
  const payload = {
    name: $("pitch-name").value.trim(),
    email: $("pitch-email").value.trim(),
    phone: $("pitch-phone").value.trim(),
    company_name: $("pitch-company").value,
    target_role: $("pitch-role").value.trim(),
    target_industry: $("pitch-industry").value.trim(),
    details: $("pitch-details").value.trim(),
  };

  if (!payload.name || !payload.email) {
    notify("Name and email are required to save a contact.", "error");
    return;
  }

  const button = $("pitch-save-button");
  const editing = state.editingEmail;
  const result = await withBusy(button, "Saving…", async () => {
    const data = await api(editing ? `/api/contacts/${encodeURIComponent(editing)}` : "/api/contacts", {
      method: editing ? "PUT" : "POST",
      body: JSON.stringify(payload),
    });
    await loadContacts();
    const saved = state.contacts.find((c) => c.email === data.contact.email);
    if (saved) selectContact(saved);
    notify(data.message || "Contact saved.");
    return data;
  });

  if (result) {
    $("pitch-save-button").textContent = "Save changes";
  }
}

async function addQuickContact() {
  const payload = {
    name: $("name").value.trim(),
    email: $("email").value.trim(),
    context: $("context").value.trim(),
  };

  if (!payload.name || !payload.email) {
    notify("Name and email are required to save a contact.", "error");
    return;
  }

  await withBusy($("add-button"), "Saving…", async () => {
    const data = await api("/api/contacts", { method: "POST", body: JSON.stringify(payload) });
    await loadContacts();
    const saved = state.contacts.find((c) => c.email === data.contact.email);
    if (saved) selectContact(saved);
    notify(data.message || "Contact saved.");
  });
}

async function removeSelectedContact() {
  if (!state.editingEmail) return;
  const contact = state.contacts.find((item) => item.email === state.editingEmail);
  const label = contact?.name || state.editingEmail;
  if (!window.confirm(`Delete ${label} from your contacts? This cannot be undone.`)) return;

  try {
    const data = await api(`/api/contacts/${encodeURIComponent(state.editingEmail)}`, { method: "DELETE" });
    state.selectedEmail = null;
    state.editingEmail = null;
    startNewContact();
    await loadContacts();
    notify(data.message || "Contact removed.");
  } catch (error) {
    notify(error.message || "Could not remove the contact.", "error");
  }
}

/* ------------------------------------------------------------------ */
/* Reference documents                                                 */
/* ------------------------------------------------------------------ */

const ALLOWED_DOC_EXTENSIONS = [".pdf", ".docx", ".txt", ".md"];

function formatBytes(bytes) {
  const value = Number(bytes) || 0;
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(0)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function validateFiles(files, existingCount) {
  const accepted = [];
  const errors = [];
  const maxBytes = state.documentLimits.maxFileMb * 1024 * 1024;

  for (const file of files) {
    const name = String(file.name || "").toLowerCase();
    const extension = name.slice(name.lastIndexOf("."));
    if (!ALLOWED_DOC_EXTENSIONS.includes(extension)) {
      errors.push(`${file.name}: only PDF, DOCX, TXT, or MD files are supported.`);
    } else if (file.size > maxBytes) {
      errors.push(`${file.name}: files must be ${state.documentLimits.maxFileMb} MB or smaller.`);
    } else if (file.size === 0) {
      errors.push(`${file.name}: the file is empty.`);
    } else if (existingCount + accepted.length >= state.documentLimits.max) {
      errors.push(`Each profile can have at most ${state.documentLimits.max} documents.`);
      break;
    } else {
      accepted.push(file);
    }
  }

  return { accepted, errors };
}

function renderDocumentList(containerId, documents, ownerType, ownerKey) {
  const container = $(containerId);
  if (!documents.length) {
    container.innerHTML = '<p class="doc-empty">No documents yet.</p>';
    return;
  }

  container.innerHTML = documents
    .map(
      (document) => `
      <div class="doc-item" data-id="${escapeHtml(document.id)}">
        <span class="doc-name" title="${escapeHtml(document.filename)}">${escapeHtml(document.filename)}</span>
        <span class="doc-size">${formatBytes(document.size_bytes)}</span>
        <button type="button" class="doc-remove" data-id="${escapeHtml(document.id)}"
          data-owner-type="${escapeHtml(ownerType)}" data-owner-key="${escapeHtml(ownerKey)}"
          aria-label="Remove ${escapeHtml(document.filename)}">Remove</button>
      </div>`
    )
    .join("");

  container.querySelectorAll(".doc-remove").forEach((button) => {
    button.addEventListener("click", () => {
      removeDocument(button.dataset.id, button.dataset.ownerType, button.dataset.ownerKey, button);
    });
  });
}

function updateContactDocControls() {
  const hasContact = Boolean(state.editingEmail);
  const atLimit = state.contactDocs.length >= state.documentLimits.max;
  const input = $("contact-doc-input");
  input.disabled = !hasContact || atLimit;
  $("contact-doc-label").classList.toggle("is-disabled", !hasContact || atLimit);

  if (!hasContact) {
    $("contact-doc-hint").textContent = "Save the contact first, then you can attach files.";
  } else if (atLimit) {
    $("contact-doc-hint").textContent = `Limit reached (${state.documentLimits.max} files). Remove one to add another.`;
  } else {
    $("contact-doc-hint").textContent = "Attach a CV, bio, or notes about this person.";
  }
}

async function loadContactDocs() {
  const key = state.editingEmail;
  if (!key) {
    state.contactDocs = [];
    renderDocumentList("contact-doc-list", [], "contact", "");
    updateContactDocControls();
    return;
  }

  try {
    const data = await api(`/api/documents?owner_type=contact&owner_key=${encodeURIComponent(key)}`);
    state.contactDocs = data.documents || [];
    state.documentLimits.max = Number(data.max_documents) || state.documentLimits.max;
    state.documentLimits.maxFileMb = Number(data.max_file_mb) || state.documentLimits.maxFileMb;
  } catch (error) {
    state.contactDocs = [];
    showNotice("pitch-verify-result", error.message || "Could not load contact documents.", "error");
  }

  renderDocumentList("contact-doc-list", state.contactDocs, "contact", key);
  updateContactDocControls();
}

async function loadCompanyDocs() {
  const key = $("pitch-company").value;
  if (!key) {
    state.companyDocs = [];
    renderDocumentList("company-doc-list", [], "company", "");
    return;
  }

  try {
    const data = await api(`/api/documents?owner_type=company&owner_key=${encodeURIComponent(key)}`);
    state.companyDocs = data.documents || [];
    state.documentLimits.max = Number(data.max_documents) || state.documentLimits.max;
    state.documentLimits.maxFileMb = Number(data.max_file_mb) || state.documentLimits.maxFileMb;
  } catch (error) {
    state.companyDocs = [];
    notify(error.message || "Could not load company documents.", "error");
  }

  renderDocumentList("company-doc-list", state.companyDocs, "company", key);
}

async function uploadFile(file, ownerType, ownerKey) {
  const form = new FormData();
  form.append("owner_type", ownerType);
  form.append("owner_key", ownerKey);
  form.append("file", file, file.name);
  return api("/api/documents", { method: "POST", body: form });
}

async function uploadDocuments(files, ownerType, ownerKey) {
  if (!ownerKey) {
    notify(
      ownerType === "contact" ? "Save the contact before uploading documents." : "Choose a company first.",
      "error"
    );
    return 0;
  }

  const existing = ownerType === "contact" ? state.contactDocs.length : state.companyDocs.length;
  const { accepted, errors } = validateFiles(Array.from(files), existing);

  let uploaded = 0;
  for (const file of accepted) {
    try {
      await uploadFile(file, ownerType, ownerKey);
      uploaded += 1;
    } catch (error) {
      errors.push(`${file.name}: ${error.message || "upload failed"}`);
    }
  }

  if (ownerType === "contact") {
    await loadContactDocs();
  } else {
    await loadCompanyDocs();
  }

  if (uploaded) {
    notify(uploaded === 1 ? "Document added." : `${uploaded} documents added.`);
    invalidatePitchPreview();
    invalidateStandardPreview();
  }
  if (errors.length) {
    notify(errors[0], "error");
  }
  return uploaded;
}

async function removeDocument(documentId, ownerType, ownerKey, button) {
  if (!window.confirm("Remove this document? The AI will no longer use it.")) return;
  button.disabled = true;
  try {
    await api(`/api/documents/${encodeURIComponent(documentId)}`, { method: "DELETE" });
    if (ownerType === "contact") {
      await loadContactDocs();
    } else {
      await loadCompanyDocs();
    }
    invalidatePitchPreview();
    invalidateStandardPreview();
    notify("Document removed.");
  } catch (error) {
    button.disabled = false;
    notify(error.message || "Could not remove the document.", "error");
  }
}

function renderPendingCompanyDocs() {
  const container = $("new-company-doc-list");
  if (!state.pendingCompanyDocs.length) {
    container.innerHTML = '<p class="doc-empty">No files selected. Add a company profile, deck, or case study.</p>';
    return;
  }

  container.innerHTML = state.pendingCompanyDocs
    .map(
      (file, index) => `
      <div class="doc-item pending">
        <span class="doc-name" title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</span>
        <span class="doc-size">${formatBytes(file.size)}</span>
        <button type="button" class="doc-remove" data-index="${index}" aria-label="Remove ${escapeHtml(file.name)}">Remove</button>
      </div>`
    )
    .join("");

  container.querySelectorAll(".doc-remove").forEach((button) => {
    button.addEventListener("click", () => {
      state.pendingCompanyDocs.splice(Number(button.dataset.index), 1);
      renderPendingCompanyDocs();
    });
  });
}

function addPendingCompanyDocs(files) {
  const { accepted, errors } = validateFiles(Array.from(files), state.companyDocs.length + state.pendingCompanyDocs.length);
  state.pendingCompanyDocs.push(...accepted);
  renderPendingCompanyDocs();
  if (errors.length) notify(errors[0], "error");
}

/* ------------------------------------------------------------------ */
/* Companies                                                           */
/* ------------------------------------------------------------------ */

function setCompanySelection(desired) {
  const select = $("pitch-company");
  const current = desired || select.value;

  if (!state.companies.length) {
    select.innerHTML = '<option value="" disabled selected>No companies yet</option>';
    return;
  }

  select.innerHTML = state.companies
    .map((company) => `<option value="${escapeHtml(company)}">${escapeHtml(company)}</option>`)
    .join("");

  if (current && !state.companies.includes(current)) {
    const option = document.createElement("option");
    option.value = current;
    option.textContent = current;
    select.appendChild(option);
  }

  const hasOption = Array.from(select.options).some((option) => option.value === current);
  select.value = hasOption ? current : state.companies[0];
}

async function loadCompanies(selectValue = null) {
  const data = await api("/api/companies");
  state.companies = data.companies || [];
  window._companyProfiles = data.profiles || {};
  setCompanySelection(selectValue);
  await loadCompanyDocs();
}

async function removeCompany() {
  const company = $("pitch-company").value;
  if (!company) return;
  if (!window.confirm(`Remove the pitching company “${company}”?`)) return;

  try {
    const data = await api(`/api/companies/${encodeURIComponent(company)}`, { method: "DELETE" });
    await loadCompanies();
    invalidatePitchPreview();
    notify(data.message || "Company removed.");
  } catch (error) {
    notify(error.message || "Could not remove the company.", "error");
  }
}

/* ------------------------------------------------------------------ */
/* Company modal                                                       */
/* ------------------------------------------------------------------ */

function openCompanyModal(editing = false) {
  state.pendingCompanyDocs = [];
  renderPendingCompanyDocs();
  hideNotice("company-modal-result");
  $("company-modal").classList.remove("hidden");

  if (editing) {
    $("company-modal-title").textContent = "Edit pitching company";
    $("save-company-btn").textContent = "Save changes";
  } else {
    $("company-modal-title").textContent = "Add pitching company";
    $("save-company-btn").textContent = "Save company";
    $("company-form").reset();
    $("new-company-tone").value = "friendly-professional";
    state.editingCompany = null;
  }
  $("new-company-name").focus();
}

function editCompany() {
  const companyName = $("pitch-company").value;
  if (!companyName) {
    notify("Select a company to edit.", "error");
    return;
  }

  const profile = window._companyProfiles?.[companyName];
  if (!profile) {
    notify("Could not load company details. Try again.", "error");
    return;
  }

  state.editingCompany = companyName;
  $("new-company-name").value = companyName;
  $("new-company-services").value = Array.isArray(profile.services) ? profile.services.join("\n") : "";
  $("new-company-value-prop").value = profile.value_proposition || "";
  $("new-company-tone").value = profile.tone || "friendly-professional";
  $("new-company-signature").value = profile.signature || "";
  state.pendingCompanyDocs = [];
  renderPendingCompanyDocs();

  openCompanyModal(true);
}

function closeCompanyModal() {
  $("company-modal").classList.add("hidden");
  $("company-form").reset();
  $("new-company-tone").value = "friendly-professional";
  state.pendingCompanyDocs = [];
  state.editingCompany = null;
  renderPendingCompanyDocs();
  $("company-modal-title").textContent = "Add pitching company";
  $("save-company-btn").textContent = "Save company";
}

async function submitCompanyForm(event) {
  event.preventDefault();
  const name = $("new-company-name").value.trim();
  const services = $("new-company-services")
    .value.split(/[\n,]/)
    .map((service) => service.trim())
    .filter(Boolean);
  const valueProposition = $("new-company-value-prop").value.trim();
  const tone = $("new-company-tone").value.trim();
  const signature = $("new-company-signature").value.trim();

  if (!name || !services.length) {
    showNotice("company-modal-result", "Company name and at least one service are required.", "error");
    return;
  }

  const button = $("save-company-btn");
  const pendingDocuments = [...state.pendingCompanyDocs];
  const isEditing = Boolean(state.editingCompany);
  const oldName = state.editingCompany;

  await withBusy(button, pendingDocuments.length ? "Saving & uploading…" : "Saving…", async () => {
    const data = await api(isEditing ? `/companies/${encodeURIComponent(oldName)}` : "/companies", {
      method: isEditing ? "PUT" : "POST",
      body: JSON.stringify({
        name,
        services,
        value_proposition: valueProposition,
        tone: tone || "friendly-professional",
        signature,
      }),
    });
    await loadCompanies(name);
    if (pendingDocuments.length) {
      await uploadDocuments(pendingDocuments, "company", name);
    }
    closeCompanyModal();
    notify(data.message || (isEditing ? `Company “${name}” updated.` : `Company “${name}” added.`));
  });
}

/* ------------------------------------------------------------------ */
/* Tabs                                                                */
/* ------------------------------------------------------------------ */

function showTab(tab) {
  const isPitch = tab === "pitch";
  $("pitch-section").classList.toggle("hidden", !isPitch);
  $("standard-section").classList.toggle("hidden", isPitch);
  $("tab-pitch-btn").classList.toggle("active", isPitch);
  $("tab-standard-btn").classList.toggle("active", !isPitch);
  $("tab-pitch-btn").setAttribute("aria-selected", String(isPitch));
  $("tab-standard-btn").setAttribute("aria-selected", String(!isPitch));
  $("tab-pill").textContent = isPitch ? "B2B pitch" : "Quick email";
}

/* ------------------------------------------------------------------ */
/* Previews & sending                                                  */
/* ------------------------------------------------------------------ */

function invalidatePitchPreview() {
  $("pitch-preview").classList.add("hidden");
  const sendButton = $("pitch-send-button");
  sendButton.disabled = true;
  sendButton.textContent = "Send pitch";
  $("pitch-spam-warning").classList.add("hidden");
  state.pitchSpamScore = 0;
}

function invalidateStandardPreview() {
  $("preview").classList.add("hidden");
  const sendButton = $("send-button");
  sendButton.disabled = true;
  sendButton.textContent = "Send email";
}

function updateSpamBadge(score) {
  const badge = $("pitch-spam-badge");
  badge.className = "badge";
  if (score <= 3) {
    badge.classList.add("spam-low");
    badge.textContent = `Spam score ${score}/10 · clean`;
  } else if (score <= 6) {
    badge.classList.add("spam-med");
    badge.textContent = `Spam score ${score}/10 · moderate`;
  } else {
    badge.classList.add("spam-high");
    badge.textContent = `Spam score ${score}/10 · high risk`;
  }
  $("pitch-spam-warning").classList.toggle("hidden", score < 7);
}

async function verifyAddress(email, noticeId, checkDns) {
  if (!email) {
    showNotice(noticeId, "Enter an email address first.", "error");
    return;
  }
  showNotice(noticeId, "Checking address…", "info");
  try {
    const data = await api(`/api/verify?check_dns=${checkDns ? "true" : "false"}`, {
      method: "POST",
      body: JSON.stringify({ email, name: "lead" }),
    });
    const reason = String(data.reason || "").replaceAll("_", " ");
    showNotice(
      noticeId,
      data.valid ? `Address looks good (${reason}).` : `Address rejected: ${reason}.`,
      data.valid ? "success" : "error"
    );
  } catch (error) {
    showNotice(noticeId, error.message || "Verification failed.", "error");
  }
}

async function generatePitch(event) {
  event.preventDefault();
  hideNotice("pitch-verify-result");
  invalidatePitchPreview();

  const payload = {
    name: $("pitch-name").value.trim(),
    email: $("pitch-email").value.trim(),
    phone: $("pitch-phone").value.trim(),
    company_name: $("pitch-company").value,
    target_role: $("pitch-role").value.trim(),
    target_industry: $("pitch-industry").value.trim(),
    details: $("pitch-details").value.trim(),
  };

  const button = $("pitch-generate-button");
  const data = await withBusy(button, "Writing…", () =>
    api("/pitch/generate", { method: "POST", body: JSON.stringify(payload) })
  );
  if (!data) return;

  $("pitch-preview-subject").textContent = data.subject;
  $("pitch-preview-body").textContent = data.body;
  $("pitch-preview-mode").textContent = `${payload.company_name} pitch`;
  state.pitchSpamScore = Number(data.spam_score) || 0;
  updateSpamBadge(state.pitchSpamScore);

  $("pitch-preview").classList.remove("hidden");
  const sendButton = $("pitch-send-button");
  sendButton.disabled = false;
  sendButton.textContent = "Send pitch";
  notify("Pitch generated. Review it before sending.");
  $("pitch-preview").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function postPitch(confirmHighSpam) {
  return api("/pitch/send", {
    method: "POST",
    body: JSON.stringify({
      recipient_email: $("pitch-email").value.trim(),
      subject: $("pitch-preview-subject").textContent.trim(),
      body: $("pitch-preview-body").textContent.trim(),
      company_name: $("pitch-company").value,
      spam_score: state.pitchSpamScore,
      confirm_high_spam: confirmHighSpam,
      smtp_provider: $("pitch-smtp-provider")?.value || undefined,
      smtp_user: $("pitch-smtp-user")?.value.trim() || undefined,
      smtp_password: $("pitch-smtp-password")?.value.trim() || undefined,
      sender_name: $("pitch-sender-name")?.value.trim() || undefined,
    }),
  });
}

async function sendPitch() {
  const recipient = $("pitch-email").value.trim();
  const subject = $("pitch-preview-subject").textContent.trim();
  const body = $("pitch-preview-body").textContent.trim();

  if (!recipient || !subject || !body) {
    notify("Generate a pitch preview before sending.", "error");
    return;
  }

  const highRisk = state.pitchSpamScore >= 7;
  const canSend = await askConfirm(
    highRisk
      ? {
          title: `High spam risk (${state.pitchSpamScore}/10)`,
          message: `This message may go to spam or be filtered.\n\nSend this pitch to ${recipient}?`,
          icon: "⚠",
          warn: true,
          acceptLabel: "Send anyway",
        }
      : {
          title: "Send this pitch?",
          message: `${recipient}\n\nSubject: ${subject}`,
          icon: "✉",
          acceptLabel: "Yes, send pitch",
        }
  );
  if (!canSend) return;

  const button = $("pitch-send-button");
  button.disabled = true;
  button.textContent = "Sending…";

  try {
    const data = await postPitch(highRisk);
    notify(data.message || "Pitch sent.");
    button.textContent = "Sent";
  } catch (error) {
    if (error.status === 422 && /spam score/i.test(error.message)) {
      const sendAnyway = await askConfirm({
        title: "High spam risk",
        message: `${error.message}\n\nSend it anyway?`,
        icon: "⚠",
        warn: true,
        acceptLabel: "Send anyway",
      });
      if (sendAnyway) {
        try {
          const data = await postPitch(true);
          notify(data.message || "Pitch sent.");
          button.textContent = "Sent";
          return;
        } catch (retryError) {
          notify(retryError.message, "error");
        }
      }
    } else {
      notify(error.message, "error");
    }
    button.disabled = false;
    button.textContent = "Send pitch";
  }
}

async function generateQuickPreview(event) {
  event.preventDefault();
  hideNotice("verify-result");
  invalidateStandardPreview();

  const payload = {
    name: $("name").value.trim(),
    email: $("email").value.trim(),
    context: $("context").value.trim(),
  };

  const button = event.currentTarget.querySelector('button[type="submit"]');
  const data = await withBusy(button, "Writing…", () =>
    api("/api/preview", { method: "POST", body: JSON.stringify(payload) })
  );
  if (!data) return;

  $("preview-subject").textContent = data.subject;
  $("preview-body").textContent = data.body;
  $("preview-mode").textContent = "AI preview";
  $("preview").classList.remove("hidden");

  const sendButton = $("send-button");
  sendButton.disabled = false;
  sendButton.textContent = "Send email";
  notify("Preview generated. Review it before sending.");
}

async function sendQuickEmail() {
  const email = $("email").value.trim();
  const subject = $("preview-subject").textContent.trim();
  const body = $("preview-body").textContent.trim();

  if (!email || !subject || !body) {
    notify("Generate a preview before sending.", "error");
    return;
  }
  if (!window.confirm(`Send this email to ${email}?\n\nSubject: ${subject}`)) return;

  const button = $("send-button");
  button.disabled = true;
  button.textContent = "Sending…";

  try {
    const data = await api("/api/send", {
      method: "POST",
      body: JSON.stringify({
        name: $("name").value.trim(),
        email,
        context: $("context").value.trim(),
        subject,
        body,
        smtp_provider: state.smtpProvider || undefined,
      }),
    });
    notify(data.message || "Email sent.");
    button.textContent = "Sent";
  } catch (error) {
    notify(error.message, "error");
    button.disabled = false;
    button.textContent = "Send email";
  }
}

/* ------------------------------------------------------------------ */
/* Events                                                              */
/* ------------------------------------------------------------------ */

function bindEvents() {
  $("tab-pitch-btn").addEventListener("click", () => showTab("pitch"));
  $("tab-standard-btn").addEventListener("click", () => showTab("standard"));

  $("smtp-toggle-button").addEventListener("click", () => {
    const next = nextSmtpProvider();
    setSmtpProvider(next);
    notify(`SMTP provider switched to ${providerName(next)}.`);
  });
  $("global-smtp-provider").addEventListener("change", (event) => {
    setSmtpProvider(event.target.value);
  });

  $("topbar-deliverability-button").addEventListener("click", () => checkDeliverability());
  $("check-deliverability-btn").addEventListener("click", () => checkDeliverability());
  $("refresh-deliverability-btn").addEventListener("click", () => checkDeliverability(true));
  $("close-deliverability-modal-btn").addEventListener("click", closeDeliverabilityModal);
  $("done-deliverability-btn").addEventListener("click", closeDeliverabilityModal);
  $("deliverability-modal").addEventListener("click", (event) => {
    if (event.target === $("deliverability-modal")) closeDeliverabilityModal();
  });

  $("new-contact-button").addEventListener("click", startNewContact);

  $("contact-search").addEventListener("input", (event) => {
    state.search = event.target.value;
    renderContacts();
  });

  ["pitch-name", "pitch-email", "pitch-phone", "pitch-company", "pitch-role", "pitch-industry", "pitch-details"].forEach(
    (id) => {
      $(id).addEventListener("input", () => {
        invalidatePitchPreview();
        if (id === "pitch-email") hideNotice("pitch-verify-result");
      });
    }
  );
  $("pitch-company").addEventListener("change", () => {
    invalidatePitchPreview();
    loadCompanyDocs();
  });

  ["name", "email", "context"].forEach((id) => {
    $(id).addEventListener("input", () => {
      invalidateStandardPreview();
      if (id === "email") hideNotice("verify-result");
    });
  });

  $("pitch-form").addEventListener("submit", generatePitch);
  $("preview-form").addEventListener("submit", generateQuickPreview);

  $("pitch-save-button").addEventListener("click", savePitchContact);
  $("add-button").addEventListener("click", addQuickContact);
  $("remove-contact-button").addEventListener("click", removeSelectedContact);

  $("pitch-verify-button").addEventListener("click", () =>
    verifyAddress($("pitch-email").value.trim(), "pitch-verify-result", true)
  );
  $("verify-button").addEventListener("click", () =>
    verifyAddress($("email").value.trim(), "verify-result", false)
  );

  $("pitch-send-button").addEventListener("click", sendPitch);
  $("send-button").addEventListener("click", sendQuickEmail);

  $("contact-doc-input").addEventListener("change", async (event) => {
    const files = Array.from(event.target.files || []);
    event.target.value = "";
    if (files.length) await uploadDocuments(files, "contact", state.editingEmail);
  });

  $("company-doc-input").addEventListener("change", async (event) => {
    const files = Array.from(event.target.files || []);
    event.target.value = "";
    if (files.length) await uploadDocuments(files, "company", $("pitch-company").value);
  });

  $("new-company-docs").addEventListener("change", (event) => {
    addPendingCompanyDocs(event.target.files || []);
    event.target.value = "";
  });

  $("open-company-modal-btn").addEventListener("click", () => openCompanyModal(false));
  const editCompanyBtn = $("edit-company-btn");
  if (editCompanyBtn) editCompanyBtn.addEventListener("click", editCompany);
  $("close-company-modal-btn").addEventListener("click", closeCompanyModal);
  $("cancel-company-modal-btn").addEventListener("click", closeCompanyModal);
  $("company-form").addEventListener("submit", submitCompanyForm);
  $("remove-company-btn").addEventListener("click", removeCompany);

  $("company-modal").addEventListener("click", (event) => {
    if (event.target === $("company-modal")) closeCompanyModal();
  });

  $("confirm-accept-btn").addEventListener("click", () => settleConfirm(true));
  $("confirm-cancel-btn").addEventListener("click", () => settleConfirm(false));
  $("confirm-modal").addEventListener("click", (event) => {
    if (event.target === $("confirm-modal")) settleConfirm(false);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (!$("confirm-modal").classList.contains("hidden")) settleConfirm(false);
    if (!$("company-modal").classList.contains("hidden")) closeCompanyModal();
    if (!$("deliverability-modal").classList.contains("hidden")) closeDeliverabilityModal();
  });

  $("logout-button").addEventListener("click", async () => {
    try {
      await api("/api/auth/logout", { method: "POST" });
    } catch (error) {
      /* The session is cleared client-side either way. */
    }
    window.location.assign("/login");
  });
}

/* ------------------------------------------------------------------ */
/* Boot                                                                */
/* ------------------------------------------------------------------ */

async function init() {
  const ready = await initAuth();
  if (!ready) return;

  bindEvents();
  updateContactDocControls();
  renderPendingCompanyDocs();

  try {
    await Promise.all([loadCompanies(), loadContacts(), loadSmtpProvider()]);
    setStatus(true);
  } catch (error) {
    setStatus(false);
    notify(error.message || "Could not load the dashboard.", "error");
  }
}

init();
