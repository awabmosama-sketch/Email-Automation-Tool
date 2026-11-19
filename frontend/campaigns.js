/* Campaign console: senders, brands, templates, campaigns, reporting. */
const $ = (id) => document.getElementById(id);

const MAPPING_FIELDS = [
  ["first_name", "First Name"],
  ["position", "Position / Job Title"],
  ["company_name", "Company Name"],
  ["personal_email", "Personal Email"],
  ["business_email", "Business Email"],
];

const SENDER_DEFAULTS = {
  smtp: {
    host: "",
    port: 587,
    security: "starttls",
    spfInclude: "",
    hostPlaceholder: "smtp.yourdomain.com",
    portPlaceholder: "465 or 587",
    hint:
      "Sending from your own domain? Copy the host, port, and security mode from your mailbox host's SMTP settings " +
      "(usually smtp.yourdomain.com or mail.yourdomain.com), use the full mailbox address as the username, " +
      "then fill in SPF, DKIM, and DMARC for that domain below.",
  },
  zoho: {
    host: "smtp.zoho.com",
    port: 465,
    security: "ssl",
    spfInclude: "include:zoho.com",
    hostPlaceholder: "smtp.zoho.com",
    portPlaceholder: "465",
    hint:
      "Zoho: smtp.zoho.com on 465 with SSL (or 587 with STARTTLS). Use a Zoho App Password and " +
      "include:zoho.com as the SPF include.",
  },
  gmail: {
    host: "smtp.gmail.com",
    port: 465,
    security: "ssl",
    spfInclude: "include:_spf.google.com",
    hostPlaceholder: "smtp.gmail.com",
    portPlaceholder: "465",
    hint:
      "Gmail: smtp.gmail.com on 465 with SSL. Use a 16-character Google App Password; " +
      "the account needs 2-Step Verification enabled.",
  },
};

const state = {
  campaigns: [],
  templates: [],
  senders: [],
  brands: [],
  totals: {},
  tab: "campaigns",
  editing: { campaign: null, template: null, sender: null, brand: null },
  mapping: {},
  preview: null,
  report: null,
  testSenderId: "",
  dnsSenderId: "",
  lastTemplateInput: null,
  confirmResolve: null,
};

let toastTimer = null;
// Provider whose defaults are currently reflected in the sender form.
let activeSenderProvider = "smtp";

/* ------------------------------------------------------------------ */
/* Helpers                                                             */
/* ------------------------------------------------------------------ */

const esc = (value) =>
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

function showNotice(message, type = "info") {
  const notice = $("console-notice");
  notice.textContent = message;
  notice.className = `notice${type === "error" ? " error" : type === "success" ? " info" : ""}`;
  notice.classList.remove("hidden");
}

function hideNotice() {
  $("console-notice").classList.add("hidden");
}

async function api(path, options = {}) {
  const config = { ...options };
  if (config.body && !(config.body instanceof FormData)) {
    config.headers = { "Content-Type": "application/json", ...(config.headers || {}) };
  }
  const response = await fetch(path, config);
  if (response.status === 401) {
    window.location.assign("/login?next=/campaigns");
    throw new Error("Your session expired. Please sign in again.");
  }
  let data = {};
  try {
    data = await response.json();
  } catch (error) {
    data = {};
  }
  if (!response.ok) {
    const label = `${(config.method || "GET").toUpperCase()} ${path.split("?")[0]}`;
    const message =
      data.detail || data.message || `Request failed (${response.status}) for ${label}.`;
    const requestError = new Error(message);
    requestError.status = response.status;
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

function openModal(id) {
  $(id).classList.remove("hidden");
}

function closeModal(id) {
  $(id).classList.add("hidden");
}

function fmtDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function fillSelect(select, items, emptyLabel, labelFn, selected = "") {
  const options = [`<option value="">${esc(emptyLabel)}</option>`];
  for (const item of items) {
    options.push(
      `<option value="${esc(item.id)}"${item.id === selected ? " selected" : ""}>${esc(labelFn(item))}</option>`
    );
  }
  select.innerHTML = options.join("");
}

function countCell(label, value) {
  return `<div class="count-cell"><span>${esc(label)}</span><strong>${Number(value || 0)}</strong></div>`;
}

function statusChip(status) {
  const safe = esc(status || "draft");
  return `<span class="status-chip ${safe}">${safe.replace(/_/g, " ")}</span>`;
}

function senderLabel(sender) {
  return sender.name ? `${sender.email} · ${sender.name}` : sender.email;
}

/* ------------------------------------------------------------------ */
/* Data loading                                                        */
/* ------------------------------------------------------------------ */

async function loadOverview() {
  const data = await api("/api/outreach/overview");
  state.campaigns = data.campaigns || [];
  state.templates = data.templates || [];
  state.senders = data.senders || [];
  state.brands = data.brands || [];
  state.totals = data.totals || {};
  renderOverview(data);
  renderCampaigns();
  renderTemplates();
  renderSenders();
  renderBrands();
}

async function refreshAll() {
  try {
    await loadOverview();
    hideNotice();
  } catch (error) {
    showNotice(error.message || "Could not load the console.", "error");
  }
}

function renderOverview(data) {
  const totals = data.totals || {};
  $("stat-sent").textContent = Number(totals.sent || 0).toLocaleString();
  $("stat-today").textContent = `${Number(totals.sent_today || 0).toLocaleString()} today`;
  $("stat-queued").textContent = Number(totals.queued || 0).toLocaleString();
  $("stat-running").textContent = `${Number(totals.running || 0)} running campaign${
    Number(totals.running || 0) === 1 ? "" : "s"
  }`;
  $("stat-bounced").textContent = Number(totals.bounced || 0).toLocaleString();
  $("stat-bounce-rate").textContent = `${Number(totals.bounce_rate || 0)}% bounce rate`;
  $("stat-inboxed").textContent = Number(totals.inboxed || 0).toLocaleString();
  $("stat-spam").textContent = Number(totals.spam_alert || 0).toLocaleString();
  $("stat-spam-rate").textContent = `${Number(totals.spam_rate || 0)}% spam rate`;

  const worker = $("worker-dot");
  const workerText = $("worker-text");
  if (data.queue_worker) {
    worker.classList.add("ready");
    workerText.textContent = "Worker active";
  } else {
    worker.classList.remove("ready");
    workerText.textContent = "Cron mode";
  }
}

/* ------------------------------------------------------------------ */
/* Campaigns                                                           */
/* ------------------------------------------------------------------ */

function renderCampaigns() {
  const list = $("campaign-list");
  if (!state.campaigns.length) {
    list.innerHTML = `<div class="empty">No campaigns yet. Create one, paste a Google Sheet URL, then sync and start.</div>`;
    return;
  }
  list.innerHTML = state.campaigns
    .map((campaign) => {
      const stats = campaign.stats || {};
      const toggle =
        campaign.status === "running"
          ? `<button type="button" class="btn btn-ghost btn-sm" data-act="pause" data-id="${esc(campaign.id)}">Pause</button>`
          : campaign.status === "completed"
            ? `<button type="button" class="btn btn-ghost btn-sm" data-act="requeue" data-id="${esc(campaign.id)}">Requeue</button>`
            : `<button type="button" class="btn btn-primary btn-sm" data-act="start" data-id="${esc(campaign.id)}">Start</button>`;
      const nextSend = campaign.next_send_at
        ? `<span>Next send <b>${esc(fmtDate(campaign.next_send_at))}</b></span>`
        : "";
      const limits = `${campaign.hourly_limit}/hr · ${campaign.daily_limit}/day · ${campaign.delay_min_minutes}–${campaign.delay_max_minutes} min delay`;
      return `
        <article class="entity-card">
          <div class="entity-head">
            <div>
              <h3>${esc(campaign.name)} ${statusChip(campaign.status)}</h3>
              <p class="entity-sub">${esc(campaign.sender_email || "No sender")} · ${esc(
                campaign.template_name || "No template"
              )}${campaign.brand_name ? ` · ${esc(campaign.brand_name)}` : ""}</p>
            </div>
            <div class="entity-actions">
              ${toggle}
              <button type="button" class="btn btn-ghost btn-sm" data-act="sync" data-id="${esc(campaign.id)}">Sync</button>
              <button type="button" class="btn btn-ghost btn-sm" data-act="report" data-id="${esc(campaign.id)}">Report</button>
              <button type="button" class="btn btn-ghost btn-sm" data-act="edit" data-id="${esc(campaign.id)}">Edit</button>
              <button type="button" class="btn btn-danger-ghost btn-sm" data-act="delete" data-id="${esc(campaign.id)}">Delete</button>
            </div>
          </div>
          <div class="count-strip">
            ${countCell("Total", stats.total)}
            ${countCell("Queued", Number(stats.queued || 0) + Number(stats.scheduled || 0))}
            ${countCell("Sent", stats.sent)}
            ${countCell("Inboxed", stats.inboxed)}
            ${countCell("Bounced", stats.bounced)}
            ${countCell("Spam", stats.spam_alert)}
          </div>
          <div class="entity-meta">
            <span>${esc(limits)}</span>
            <span>Window <b>${String(campaign.work_start_hour ?? 9).padStart(2, "0")}:00–${String(
              campaign.work_end_hour ?? 18
            ).padStart(2, "0")}:00</b> ${esc(campaign.timezone || "UTC")}</span>
            <span>Sheet <b>${campaign.sheet_url ? "linked" : "missing"}</b></span>
            ${nextSend}
          </div>
        </article>`;
    })
    .join("");
}

async function startCampaign(campaign) {
  if (campaign.sender_id) {
    try {
      const dns = await api(`/api/senders/${encodeURIComponent(campaign.sender_id)}/deliverability`);
      if (!dns.authenticated) {
        const proceed = await confirmAction(
          "DNS needs attention",
          `SPF, DKIM, and DMARC are not all passing for ${dns.domain || "this domain"}. Starting now can hurt inbox placement.`,
          "Start anyway"
        );
        if (!proceed) {
          openDns(campaign.sender_id, dns);
          return;
        }
      }
    } catch (error) {
      /* DNS is advisory; do not block on a resolver hiccup. */
    }
  }
  await api(`/api/campaigns/${encodeURIComponent(campaign.id)}/start`, { method: "POST" });
  notify("Campaign started.");
  await refreshAll();
}

async function syncCampaign(campaign, button) {
  await withBusy(button, "Syncing…", async () => {
    const result = await api(`/api/campaigns/${encodeURIComponent(campaign.id)}/sync`, {
      method: "POST",
    });
    const sheet = result.sheet || {};
    notify(
      `Synced ${Number(result.added || 0)} new leads (${Number(result.duplicates || 0)} duplicates, ${
        sheet.usable_leads ?? 0
      } usable rows).`
    );
    await refreshAll();
  });
}

async function deleteCampaign(campaign) {
  const confirmed = await confirmAction(
    "Delete campaign?",
    `"${campaign.name}" and its ${Number((campaign.stats || {}).total || 0)} recipients will be removed. This cannot be undone.`,
    "Delete"
  );
  if (!confirmed) return;
  await api(`/api/campaigns/${encodeURIComponent(campaign.id)}`, { method: "DELETE" });
  notify("Campaign deleted.");
  await refreshAll();
}

$("campaign-list").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-act]");
  if (!button) return;
  const campaign = state.campaigns.find((item) => item.id === button.dataset.id);
  if (!campaign) return;
  const act = button.dataset.act;

  if (act === "edit") return openCampaignModal(campaign);
  if (act === "report") return openReport(campaign.id);
  if (act === "sync") return syncCampaign(campaign, button);
  if (act === "delete") return deleteCampaign(campaign);

  if (act === "start") {
    await withBusy(button, "Starting…", () => startCampaign(campaign));
  } else if (act === "pause") {
    await withBusy(button, "Pausing…", async () => {
      await api(`/api/campaigns/${encodeURIComponent(campaign.id)}/pause`, { method: "POST" });
      notify("Campaign paused.");
      await refreshAll();
    });
  } else if (act === "requeue") {
    await withBusy(button, "Requeuing…", async () => {
      await api(`/api/campaigns/${encodeURIComponent(campaign.id)}/requeue`, { method: "POST" });
      notify("Campaign requeued. Start it when you are ready.");
      await refreshAll();
    });
  }
});

/* ------------------------------------------------------------------ */
/* Campaign editor                                                     */
/* ------------------------------------------------------------------ */

function fillHourSelects() {
  const start = $("campaign-work-start");
  const end = $("campaign-work-end");
  start.innerHTML = Array.from({ length: 24 }, (_, hour) => {
    const label = `${String(hour).padStart(2, "0")}:00`;
    return `<option value="${hour}">${label}</option>`;
  }).join("");
  end.innerHTML = Array.from({ length: 24 }, (_, index) => {
    const hour = index + 1;
    const label = `${String(hour).padStart(2, "0")}:00`;
    return `<option value="${hour}">${label}</option>`;
  }).join("");
}

function mappingOptions() {
  const headers = state.preview?.headers || [];
  const values = Object.values(state.mapping || {});
  return Array.from(new Set([...headers, ...values].filter(Boolean)));
}

function renderMapping() {
  const options = mappingOptions();
  $("mapping-grid").innerHTML = MAPPING_FIELDS.map(([field, label]) => {
    const current = state.mapping[field] || "";
    const choices = options
      .map(
        (header) =>
          `<option value="${esc(header)}"${header === current ? " selected" : ""}>${esc(header)}</option>`
      )
      .join("");
    return `<label>${esc(label)}<select data-map="${field}"><option value="">Not mapped</option>${choices}</select></label>`;
  }).join("");
}

$("mapping-grid").addEventListener("change", (event) => {
  const select = event.target.closest("select[data-map]");
  if (!select) return;
  state.mapping[select.dataset.map] = select.value;
});

function openCampaignModal(campaign = null) {
  state.editing.campaign = campaign;
  state.preview = null;
  state.mapping = { ...(campaign?.mapping || {}) };
  $("campaign-modal-title").textContent = campaign ? "Edit campaign" : "New campaign";
  $("campaign-name").value = campaign?.name || "";
  $("campaign-sheet-url").value = campaign?.sheet_url || "";
  $("campaign-sheet-tab").value = campaign?.sheet_tab || "";
  $("campaign-sheet-gid").value = campaign?.sheet_gid || "0";
  $("campaign-timezone").value =
    campaign?.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  $("campaign-hourly").value = campaign?.hourly_limit ?? "";
  $("campaign-daily").value = campaign?.daily_limit ?? "";
  $("campaign-delay-min").value = campaign?.delay_min_minutes ?? "";
  $("campaign-delay-max").value = campaign?.delay_max_minutes ?? "";
  $("campaign-weekdays").checked = Boolean(campaign?.weekdays_only);
  $("campaign-autocomplete").checked = campaign ? Boolean(campaign.auto_complete) : true;
  $("campaign-use-ai").checked = Boolean(campaign?.use_ai);
  $("campaign-ai-prompt").value = campaign?.ai_prompt || "";
  $("campaign-unsubscribe").value = campaign?.unsubscribe_base_url || "";

  fillSelect($("campaign-sender"), state.senders, "No sender", senderLabel, campaign?.sender_id || "");
  fillSelect(
    $("campaign-template"),
    state.templates,
    "No template",
    (item) => item.name,
    campaign?.template_id || ""
  );
  fillSelect(
    $("campaign-brand"),
    state.brands,
    "No brand",
    (item) => item.name,
    campaign?.brand_id || ""
  );

  $("campaign-work-start").value = String(campaign?.work_start_hour ?? 9);
  $("campaign-work-end").value = String(campaign?.work_end_hour ?? 18);
  $("campaign-ai-wrap").classList.toggle("hidden", !$("campaign-use-ai").checked);
  $("sheet-preview").classList.add("hidden");
  $("sheet-preview").innerHTML = "";
  $("campaign-form-error").classList.add("hidden");
  renderMapping();
  openModal("campaign-modal");
}

$("campaign-use-ai").addEventListener("change", () => {
  $("campaign-ai-wrap").classList.toggle("hidden", !$("campaign-use-ai").checked);
});

function renderSheetPreview(result) {
  const sample = result.sample || [];
  const columns = ["first_name", "position", "company_name", "business_email"];
  const warnings = (result.warnings || []).map((warning) => `<li>${esc(warning)}</li>`).join("");
  const rows = sample
    .map(
      (lead) =>
        `<tr>${columns
          .map((column) => `<td>${esc(lead[column] || "")}</td>`)
          .join("")}</tr>`
    )
    .join("");
  $("sheet-preview").innerHTML = `
    <h4>${Number(result.usable_leads || 0).toLocaleString()} usable rows of ${Number(
      result.total_rows || 0
    ).toLocaleString()} · ${esc(result.source || "sheet")}</h4>
    ${warnings ? `<ul class="preview-warnings">${warnings}</ul>` : ""}
    <div class="preview-scroll">
      <table>
        <thead><tr>${columns
          .map((column) => `<th>${esc(column.replace(/_/g, " "))}</th>`)
          .join("")}</tr></thead>
        <tbody>${rows || `<tr><td colspan="4" class="muted">No usable rows found.</td></tr>`}</tbody>
      </table>
    </div>
    <p class="muted small">Sample of the first ${sample.length} usable rows. Phone columns are ignored.</p>`;
  $("sheet-preview").classList.remove("hidden");
}

async function previewSheet(button) {
  const sheetUrl = $("campaign-sheet-url").value.trim();
  if (!sheetUrl) {
    notify("Paste a Google Sheet URL first.", "error");
    return;
  }
  await withBusy(button, "Reading sheet…", async () => {
    const result = await api("/api/sheets/preview", {
      method: "POST",
      body: JSON.stringify({
        sheet_url: sheetUrl,
        gid: $("campaign-sheet-gid").value.trim(),
        tab: $("campaign-sheet-tab").value.trim(),
        mapping: state.mapping,
      }),
    });
    state.preview = result;
    state.mapping = { ...(result.column_map || result.suggested_map || {}) };
    renderMapping();
    renderSheetPreview(result);
    notify("Sheet preview loaded. Check the mapping below.");
  });
}

$("preview-sheet-button").addEventListener("click", (event) => previewSheet(event.currentTarget));

function numberOrUndefined(id) {
  const raw = $(id).value.trim();
  if (!raw) return undefined;
  const value = Number(raw);
  return Number.isFinite(value) ? value : undefined;
}

function setFormError(id, message) {
  const notice = $(id);
  if (!message) {
    notice.classList.add("hidden");
    notice.textContent = "";
    return;
  }
  notice.textContent = message;
  notice.classList.remove("hidden");
}

$("campaign-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  setFormError("campaign-form-error", "");
  const button = $("save-campaign-button");
  const campaign = state.editing.campaign;

  const payload = {
    name: $("campaign-name").value.trim(),
    sheet_url: $("campaign-sheet-url").value.trim(),
    sheet_gid: $("campaign-sheet-gid").value.trim() || "0",
    sheet_tab: $("campaign-sheet-tab").value.trim(),
    sender_id: $("campaign-sender").value,
    template_id: $("campaign-template").value,
    brand_id: $("campaign-brand").value,
    timezone: $("campaign-timezone").value.trim() || "UTC",
    weekdays_only: $("campaign-weekdays").checked,
    auto_complete: $("campaign-autocomplete").checked,
    use_ai: $("campaign-use-ai").checked,
    ai_prompt: $("campaign-ai-prompt").value.trim(),
    unsubscribe_base_url: $("campaign-unsubscribe").value.trim(),
    mapping: state.mapping,
  };
  const hourly = numberOrUndefined("campaign-hourly");
  const daily = numberOrUndefined("campaign-daily");
  const delayMin = numberOrUndefined("campaign-delay-min");
  const delayMax = numberOrUndefined("campaign-delay-max");
  if (hourly !== undefined) payload.hourly_limit = hourly;
  if (daily !== undefined) payload.daily_limit = daily;
  if (delayMin !== undefined) payload.delay_min_minutes = delayMin;
  if (delayMax !== undefined) payload.delay_max_minutes = delayMax;
  payload.work_start_hour = Number($("campaign-work-start").value);
  payload.work_end_hour = Number($("campaign-work-end").value);

  await withBusy(button, "Saving…", async () => {
    try {
      await api(
        campaign
          ? `/api/campaigns/${encodeURIComponent(campaign.id)}`
          : "/api/campaigns",
        {
          method: campaign ? "PUT" : "POST",
          body: JSON.stringify(payload),
        }
      );
      closeModal("campaign-modal");
      notify(campaign ? "Campaign updated." : "Campaign created.");
      await refreshAll();
    } catch (error) {
      setFormError("campaign-form-error", error.message);
    }
  });
});

/* ------------------------------------------------------------------ */
/* Templates                                                           */
/* ------------------------------------------------------------------ */

function renderTemplates() {
  const list = $("template-list");
  if (!state.templates.length) {
    list.innerHTML = `<div class="empty">No templates yet. Templates support {{First Name}}, {{Company Name}}, {{Position}}, and {{signature}}.</div>`;
    return;
  }
  list.innerHTML = state.templates
    .map((template) => {
      const brand = state.brands.find((item) => item.id === template.brand_id);
      return `
        <article class="entity-card">
          <div class="entity-head">
            <div>
              <h3>${esc(template.name)}</h3>
              <p class="entity-sub">${esc(template.subject || "No subject")}${
                brand ? ` · ${esc(brand.name)}` : ""
              }</p>
            </div>
            <div class="entity-actions">
              <button type="button" class="btn btn-ghost btn-sm" data-act="edit" data-id="${esc(template.id)}">Edit</button>
              <button type="button" class="btn btn-danger-ghost btn-sm" data-act="delete" data-id="${esc(template.id)}">Delete</button>
            </div>
          </div>
          <div class="entity-meta"><span>${esc((template.body || "").slice(0, 180))}${
            (template.body || "").length > 180 ? "…" : ""
          }</span></div>
        </article>`;
    })
    .join("");
}

function openTemplateModal(template = null) {
  state.editing.template = template;
  $("template-modal-title").textContent = template ? "Edit template" : "New template";
  $("template-name").value = template?.name || "";
  $("template-subject").value = template?.subject || "";
  $("template-body").value = template?.body || "";
  $("template-prompt").value = template?.prompt || "";
  fillSelect(
    $("template-brand"),
    state.brands,
    "No brand",
    (item) => item.name,
    template?.brand_id || ""
  );
  setFormError("template-form-error", "");
  openModal("template-modal");
}

$("template-list").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-act]");
  if (!button) return;
  const template = state.templates.find((item) => item.id === button.dataset.id);
  if (!template) return;
  if (button.dataset.act === "edit") return openTemplateModal(template);
  if (button.dataset.act === "delete") {
    const confirmed = await confirmAction(
      "Delete template?",
      `"${template.name}" will be removed. Campaigns using it must be reassigned first.`,
      "Delete"
    );
    if (!confirmed) return;
    try {
      await api(`/api/templates/${encodeURIComponent(template.id)}`, { method: "DELETE" });
      notify("Template deleted.");
      await refreshAll();
    } catch (error) {
      notify(error.message, "error");
    }
  }
});

$("template-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  setFormError("template-form-error", "");
  const template = state.editing.template;
  const payload = {
    name: $("template-name").value.trim(),
    brand_id: $("template-brand").value,
    subject: $("template-subject").value.trim(),
    body: $("template-body").value.trim(),
    prompt: $("template-prompt").value.trim(),
  };
  await withBusy($("save-template-button"), "Saving…", async () => {
    try {
      await api(template ? `/api/templates/${encodeURIComponent(template.id)}` : "/api/templates", {
        method: template ? "PUT" : "POST",
        body: JSON.stringify(payload),
      });
      closeModal("template-modal");
      notify(template ? "Template updated." : "Template created.");
      await refreshAll();
    } catch (error) {
      setFormError("template-form-error", error.message);
    }
  });
});

["template-subject", "template-body"].forEach((id) => {
  $(id).addEventListener("focus", () => {
    state.lastTemplateInput = $(id);
  });
});

document.querySelectorAll(".chip[data-placeholder]").forEach((chip) => {
  chip.addEventListener("click", () => {
    const target = state.lastTemplateInput || $("template-body");
    const text = chip.dataset.placeholder;
    const start = target.selectionStart ?? target.value.length;
    const end = target.selectionEnd ?? start;
    target.value = target.value.slice(0, start) + text + target.value.slice(end);
    target.focus();
    target.selectionStart = target.selectionEnd = start + text.length;
  });
});

/* ------------------------------------------------------------------ */
/* Senders                                                             */
/* ------------------------------------------------------------------ */

function renderSenders() {
  const list = $("sender-list");
  if (!state.senders.length) {
    list.innerHTML = `<div class="empty">No sending accounts yet. Add an SMTP app password for the mailbox you send from.</div>`;
    return;
  }
  list.innerHTML = state.senders
    .map((sender) => {
      const caps = [];
      if (Number(sender.hourly_limit || 0) > 0) caps.push(`${sender.hourly_limit}/hr`);
      if (Number(sender.daily_limit || 0) > 0) caps.push(`${sender.daily_limit}/day`);
      return `
        <article class="entity-card">
          <div class="entity-head">
            <div>
              <h3>${esc(sender.email)} ${sender.has_password ? "" : statusChip("draft")}</h3>
              <p class="entity-sub">${esc(sender.name || "No display name")} · ${esc(
                sender.provider
              )} · ${esc(sender.host)}:${esc(sender.port)} ${esc(sender.security)}</p>
            </div>
            <div class="entity-actions">
              <button type="button" class="btn btn-ghost btn-sm" data-act="dns" data-id="${esc(sender.id)}">DNS</button>
              <button type="button" class="btn btn-ghost btn-sm" data-act="test" data-id="${esc(sender.id)}">Test</button>
              <button type="button" class="btn btn-ghost btn-sm" data-act="edit" data-id="${esc(sender.id)}">Edit</button>
              <button type="button" class="btn btn-danger-ghost btn-sm" data-act="delete" data-id="${esc(sender.id)}">Delete</button>
            </div>
          </div>
          <div class="entity-meta">
            <span>DKIM selector <b>${esc(sender.dkim_selector || "auto")}</b></span>
            <span>SPF <b>${esc(sender.spf_include || "not set")}</b></span>
            <span>DMARC <b>p=${esc(sender.dmarc_policy || "none")}</b></span>
            ${
              sender.provider === "outlook"
                ? `<span>M365 needs OAuth — this sender cannot authenticate</span>`
                : ""
            }
            <span>Timezone <b>${esc(sender.timezone || "UTC")}</b></span>
            <span>Caps <b>${esc(caps.join(" · ") || "unlimited")}</b></span>
            ${sender.brand ? `<span>Brand <b>${esc(sender.brand)}</b></span>` : ""}
          </div>
        </article>`;
    })
    .join("");
}

function openSenderModal(sender = null) {
  state.editing.sender = sender;
  $("sender-modal-title").textContent = sender ? "Edit sender" : "New sender";
  // Providers that are no longer offered (Outlook/M365 needs OAuth) stay visible
  // for saved senders so editing one cannot silently rewrite its provider.
  const providerSelect = $("sender-provider");
  providerSelect.querySelectorAll("option[data-legacy]").forEach((option) => option.remove());
  if (
    sender?.provider &&
    ![...providerSelect.options].some((option) => option.value === sender.provider)
  ) {
    const option = document.createElement("option");
    option.value = sender.provider;
    option.textContent = `${sender.provider} (legacy — OAuth required)`;
    option.dataset.legacy = "1";
    providerSelect.append(option);
  }
  activeSenderProvider = sender?.provider || "smtp";
  $("sender-email").value = sender?.email || "";
  $("sender-name").value = sender?.name || "";
  $("sender-provider").value = sender?.provider || "smtp";
  $("sender-host").value = sender?.host || "";
  $("sender-port").value = sender?.port || "";
  $("sender-security").value = sender?.security || "ssl";
  $("sender-username").value = sender?.username || "";
  $("sender-password").value = "";
  $("sender-password").placeholder = sender
    ? "Leave blank to keep the saved password"
    : "SMTP or app password from your host";
  applySenderProviderHints(activeSenderProvider);
  $("sender-password-req").classList.toggle("hidden", Boolean(sender));
  $("sender-brand").value = sender?.brand || "";
  $("sender-dkim").value = sender?.dkim_selector || "";
  $("sender-spf").value = sender?.spf_include || "";
  $("sender-dmarc").value = sender?.dmarc_policy || "none";
  $("sender-timezone").value = sender?.timezone || "UTC";
  $("sender-hourly").value = sender?.hourly_limit || 0;
  $("sender-daily").value = sender?.daily_limit || 0;
  setFormError("sender-form-error", "");
  openModal("sender-modal");
}

function applySenderProviderDefaults(provider) {
  const next = SENDER_DEFAULTS[provider] || SENDER_DEFAULTS.smtp;
  const previous = SENDER_DEFAULTS[activeSenderProvider] || SENDER_DEFAULTS.smtp;
  const fields = [
    ["sender-host", next.host, previous.host],
    ["sender-port", next.port, previous.port],
    ["sender-security", next.security, previous.security],
    ["sender-spf", next.spfInclude, previous.spfInclude],
  ];
  for (const [id, value, previousValue] of fields) {
    const input = $(id);
    // Only fill fields that are empty or still hold the previous provider's
    // default; custom hosts, ports, and SPF includes are left untouched.
    if (input.value.trim() === "" || input.value === String(previousValue)) {
      input.value = value;
    }
  }
  activeSenderProvider = provider;
}

function applySenderProviderHints(provider) {
  const preset = SENDER_DEFAULTS[provider] || SENDER_DEFAULTS.smtp;
  $("sender-host").placeholder = preset.hostPlaceholder;
  $("sender-port").placeholder = preset.portPlaceholder;
  const hint = $("sender-provider-hint");
  if (hint) hint.textContent = preset.hint;
}

$("sender-provider").addEventListener("change", (event) => {
  applySenderProviderDefaults(event.target.value);
  applySenderProviderHints(event.target.value);
});

$("sender-list").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-act]");
  if (!button) return;
  const sender = state.senders.find((item) => item.id === button.dataset.id);
  if (!sender) return;
  const act = button.dataset.act;
  if (act === "edit") return openSenderModal(sender);
  if (act === "dns") return openDns(sender.id);
  if (act === "test") return openTestModal(sender);
  if (act === "delete") {
    const confirmed = await confirmAction(
      "Delete sender?",
      `${sender.email} will be removed. Campaigns using it must be reassigned first.`,
      "Delete"
    );
    if (!confirmed) return;
    try {
      await api(`/api/senders/${encodeURIComponent(sender.id)}`, { method: "DELETE" });
      notify("Sender deleted.");
      await refreshAll();
    } catch (error) {
      notify(error.message, "error");
    }
  }
});

$("sender-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  setFormError("sender-form-error", "");
  const sender = state.editing.sender;
  const password = $("sender-password").value;
  const payload = {
    email: $("sender-email").value.trim(),
    name: $("sender-name").value.trim(),
    provider: $("sender-provider").value,
    host: $("sender-host").value.trim(),
    port: Number($("sender-port").value) || 465,
    security: $("sender-security").value,
    username: $("sender-username").value.trim(),
    brand: $("sender-brand").value.trim(),
    dkim_selector: $("sender-dkim").value.trim(),
    spf_include: $("sender-spf").value.trim(),
    dmarc_policy: $("sender-dmarc").value || "none",
    timezone: $("sender-timezone").value.trim() || "UTC",
    hourly_limit: Number($("sender-hourly").value) || 0,
    daily_limit: Number($("sender-daily").value) || 0,
  };
  if (password) payload.password = password;
  if (!sender && !password) {
    setFormError("sender-form-error", "An app password is required for a new sender.");
    return;
  }
  await withBusy($("save-sender-button"), "Saving…", async () => {
    try {
      await api(sender ? `/api/senders/${encodeURIComponent(sender.id)}` : "/api/senders", {
        method: sender ? "PUT" : "POST",
        body: JSON.stringify(payload),
      });
      closeModal("sender-modal");
      notify(sender ? "Sender updated." : "Sender created.");
      await refreshAll();
    } catch (error) {
      setFormError("sender-form-error", error.message);
    }
  });
});

/* ------------------------------------------------------------------ */
/* Brands                                                              */
/* ------------------------------------------------------------------ */

function renderBrands() {
  const list = $("brand-list");
  if (!state.brands.length) {
    list.innerHTML = `<div class="empty">No brands yet. Add the domain and signature once, then reuse it in campaigns.</div>`;
    return;
  }
  list.innerHTML = state.brands
    .map(
      (brand) => `
      <article class="entity-card">
        <div class="entity-head">
          <div>
            <h3>${esc(brand.name)}</h3>
            <p class="entity-sub">${esc(brand.domain || "No domain")}${
              brand.website ? ` · ${esc(brand.website)}` : ""
            }</p>
          </div>
          <div class="entity-actions">
            <button type="button" class="btn btn-ghost btn-sm" data-act="edit" data-id="${esc(brand.id)}">Edit</button>
            <button type="button" class="btn btn-danger-ghost btn-sm" data-act="delete" data-id="${esc(brand.id)}">Delete</button>
          </div>
        </div>
        <div class="entity-meta">
          <span>${esc((brand.description || brand.signature || "No description").slice(0, 200))}</span>
        </div>
      </article>`
    )
    .join("");
}

function openBrandModal(brand = null) {
  state.editing.brand = brand;
  $("brand-modal-title").textContent = brand ? "Edit brand" : "New brand";
  $("brand-name").value = brand?.name || "";
  $("brand-domain").value = brand?.domain || "";
  $("brand-website").value = brand?.website || "";
  $("brand-description").value = brand?.description || "";
  $("brand-signature").value = brand?.signature || "";
  setFormError("brand-form-error", "");
  openModal("brand-modal");
}

$("brand-list").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-act]");
  if (!button) return;
  const brand = state.brands.find((item) => item.id === button.dataset.id);
  if (!brand) return;
  if (button.dataset.act === "edit") return openBrandModal(brand);
  if (button.dataset.act === "delete") {
    const confirmed = await confirmAction(
      "Delete brand?",
      `"${brand.name}" will be removed. Campaigns using it must be reassigned first.`,
      "Delete"
    );
    if (!confirmed) return;
    try {
      await api(`/api/brands/${encodeURIComponent(brand.id)}`, { method: "DELETE" });
      notify("Brand deleted.");
      await refreshAll();
    } catch (error) {
      notify(error.message, "error");
    }
  }
});

$("brand-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  setFormError("brand-form-error", "");
  const brand = state.editing.brand;
  const payload = {
    name: $("brand-name").value.trim(),
    domain: $("brand-domain").value.trim(),
    website: $("brand-website").value.trim(),
    description: $("brand-description").value.trim(),
    signature: $("brand-signature").value.trim(),
  };
  await withBusy($("save-brand-button"), "Saving…", async () => {
    try {
      await api(brand ? `/api/brands/${encodeURIComponent(brand.id)}` : "/api/brands", {
        method: brand ? "PUT" : "POST",
        body: JSON.stringify(payload),
      });
      closeModal("brand-modal");
      notify(brand ? "Brand updated." : "Brand created.");
      await refreshAll();
    } catch (error) {
      setFormError("brand-form-error", error.message);
    }
  });
});

/* ------------------------------------------------------------------ */
/* Report                                                              */
/* ------------------------------------------------------------------ */

function reportTile(label, value) {
  return `<div class="report-tile"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`;
}

function renderReport(report) {
  const campaign = report.campaign || {};
  const counts = report.counts || {};
  const delivery = report.deliverability || {};
  $("report-modal-title").textContent = campaign.name || "Campaign report";
  $("report-subtitle").textContent = `${campaign.sender_email || "No sender"} · ${
    campaign.template_name || "No template"
  } · ${campaign.timezone || "UTC"}`;

  const recipients = (report.recipients || [])
    .map((recipient) => {
      const options = ["sent", "inboxed", "spam_alert", "bounced", "failed", "skipped"]
        .map(
          (status) =>
            `<option value="${status}"${status === recipient.status ? " selected" : ""}>${status.replace(
              /_/g,
              " "
            )}</option>`
        )
        .join("");
      return `<tr>
        <td class="email-cell">${esc(recipient.email)}</td>
        <td>${esc(recipient.first_name || "—")}<br><span class="muted small">${esc(
          recipient.company_name || ""
        )}</span></td>
        <td>${statusChip(recipient.status)}</td>
        <td><select data-recipient="${esc(recipient.id)}" aria-label="Update status">${options}</select></td>
        <td class="muted small">${esc(recipient.error || "—")}</td>
        <td class="muted small">${esc(fmtDate(recipient.updated_at))}</td>
      </tr>`;
    })
    .join("");

  const events = (report.events || [])
    .map(
      (event) => `<div class="event-row"><time>${esc(fmtDate(event.created_at))}</time>
        <span>${esc(event.message || event.status || "")}</span>
        <span class="muted small">${esc(event.email || "")}</span></div>`
    )
    .join("");

  $("report-body").innerHTML = `
    <div class="report-summary">
      ${reportTile("Total", counts.total ?? 0)}
      ${reportTile("Sent", counts.sent ?? 0)}
      ${reportTile("Queued", (counts.queued ?? 0) + (counts.scheduled ?? 0))}
      ${reportTile("Bounced", counts.bounced ?? 0)}
      ${reportTile("Inboxed", counts.inboxed ?? 0)}
      ${reportTile("Spam alerts", counts.spam_alert ?? 0)}
      ${reportTile("Failed", counts.failed ?? 0)}
      ${counts.stale ? reportTile("Awaiting confirmation", counts.stale) : ""}
      ${reportTile("Bounce rate", `${delivery.bounce_rate ?? 0}%`)}
    </div>
    <div class="report-section">
      <h4>Recipients</h4>
      <div class="table-scroll">
        <table class="recipient-table">
          <thead><tr><th>Email</th><th>Lead</th><th>Status</th><th>Set status</th><th>Last error</th><th>Updated</th></tr></thead>
          <tbody>${recipients || `<tr><td colspan="6" class="muted">No recipients yet. Sync the sheet.</td></tr>`}</tbody>
        </table>
      </div>
    </div>
    <div class="report-section">
      <h4>Recent events</h4>
      <div class="event-list">${events || `<p class="muted small">No events yet.</p>`}</div>
    </div>`;

  const toggle = $("report-toggle-button");
  if (campaign.status === "running") {
    toggle.textContent = "Pause";
    toggle.dataset.mode = "pause";
  } else if (campaign.status === "completed") {
    toggle.textContent = "Requeue";
    toggle.dataset.mode = "requeue";
  } else {
    toggle.textContent = "Start";
    toggle.dataset.mode = "start";
  }
}

async function openReport(campaignId) {
  openModal("report-modal");
  $("report-body").innerHTML = `<p class="muted">Loading report…</p>`;
  try {
    const report = await api(`/api/campaigns/${encodeURIComponent(campaignId)}?limit=300`);
    state.report = report;
    renderReport(report);
  } catch (error) {
    $("report-body").innerHTML = `<div class="notice error">${esc(error.message)}</div>`;
  }
}

$("report-sync-button").addEventListener("click", async (event) => {
  const report = state.report;
  if (!report) return;
  const campaign = report.campaign;
  await withBusy(event.currentTarget, "Syncing…", async () => {
    try {
      const result = await api(`/api/campaigns/${encodeURIComponent(campaign.id)}/sync`, {
        method: "POST",
      });
      notify(`Synced ${Number(result.added || 0)} new leads.`);
      await loadOverview();
      await openReport(campaign.id);
    } catch (error) {
      notify(error.message, "error");
    }
  });
});

$("report-toggle-button").addEventListener("click", async (event) => {
  const report = state.report;
  if (!report) return;
  const campaign = report.campaign;
  const mode = event.currentTarget.dataset.mode;
  await withBusy(event.currentTarget, "Working…", async () => {
    try {
      if (mode === "start") {
        await startCampaign(campaign);
      } else if (mode === "pause") {
        await api(`/api/campaigns/${encodeURIComponent(campaign.id)}/pause`, { method: "POST" });
        notify("Campaign paused.");
      } else {
        await api(`/api/campaigns/${encodeURIComponent(campaign.id)}/requeue`, {
          method: "POST",
        });
        notify("Campaign requeued.");
      }
      await loadOverview();
      await openReport(campaign.id);
    } catch (error) {
      notify(error.message, "error");
    }
  });
});

$("report-body").addEventListener("change", async (event) => {
  const select = event.target.closest("select[data-recipient]");
  if (!select || !state.report) return;
  const campaignId = state.report.campaign.id;
  try {
    await api(
      `/api/campaigns/${encodeURIComponent(campaignId)}/recipients/${encodeURIComponent(
        select.dataset.recipient
      )}/status`,
      { method: "POST", body: JSON.stringify({ status: select.value }) }
    );
    notify("Recipient status updated.");
    await loadOverview();
    await openReport(campaignId);
  } catch (error) {
    notify(error.message, "error");
  }
});

/* ------------------------------------------------------------------ */
/* DNS + sender tests                                                  */
/* ------------------------------------------------------------------ */

function dnsCheckRow(label, check) {
  if (!check) return "";
  const status = check.status || "unknown";
  return `<div class="dns-row ${esc(status)}">
    <div class="dns-head"><strong>${esc(label)}</strong>${statusChip(status)}</div>
    <span class="muted small">${esc(check.detail || "")}</span>
    ${check.record ? `<code>${esc(check.record)}</code>` : ""}
  </div>`;
}

function renderDns(data) {
  const setup = data.setup || {};
  const setupRows = Object.entries(setup)
    .map(
      ([key, record]) => `<div class="dns-row">
        <div class="dns-head"><strong>${esc(key.toUpperCase())} record to publish</strong><span class="muted small">${esc(
          record.type || "TXT"
        )}</span></div>
        <code>${esc(record.host || "")}</code>
        <code>${esc(record.value || "")}</code>
        <span class="muted small">${esc(record.note || "")}</span>
      </div>`
    )
    .join("");
  const tips = (data.tips || []).map((tip) => `<li>${esc(tip)}</li>`).join("");

  $("dns-subtitle").textContent = `${data.domain || "Domain"} · ${
    data.authenticated ? "authenticated" : "needs attention"
  }`;
  $("dns-body").innerHTML = `
    <div class="dns-grid">
      ${dnsCheckRow("SPF", data.spf)}
      ${dnsCheckRow("DKIM", data.dkim)}
      ${dnsCheckRow("DMARC", data.dmarc)}
    </div>
    <div class="report-section"><h4>Recommended records</h4><div class="dns-grid">${setupRows}</div></div>
    <div class="report-section"><h4>Notes</h4><ul class="dns-tips">${tips}</ul></div>`;
}

async function openDns(senderId, initialData = null) {
  state.dnsSenderId = senderId;
  openModal("dns-modal");
  if (initialData) {
    renderDns(initialData);
    return;
  }
  $("dns-body").innerHTML = `<p class="muted">Checking DNS…</p>`;
  try {
    const data = await api(`/api/senders/${encodeURIComponent(senderId)}/deliverability`);
    renderDns(data);
  } catch (error) {
    $("dns-body").innerHTML = `<div class="notice error">${esc(error.message)}</div>`;
  }
}

$("dns-refresh-button").addEventListener("click", async (event) => {
  if (!state.dnsSenderId) return;
  await withBusy(event.currentTarget, "Checking…", async () => {
    try {
      const data = await api(
        `/api/senders/${encodeURIComponent(state.dnsSenderId)}/deliverability?refresh=true`
      );
      renderDns(data);
    } catch (error) {
      notify(error.message, "error");
    }
  });
});

function openTestModal(sender) {
  state.testSenderId = sender.id;
  $("test-modal-title").textContent = `Test ${sender.email}`;
  $("test-subtitle").textContent = `${sender.host}:${sender.port} · leave the recipient blank to test only the login.`;
  $("test-recipient").value = "";
  $("test-result").classList.add("hidden");
  openModal("test-modal");
}

$("test-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const resultBox = $("test-result");
  const recipient = $("test-recipient").value.trim();
  await withBusy($("run-test-button"), "Testing…", async () => {
    try {
      const result = await api(`/api/senders/${encodeURIComponent(state.testSenderId)}/test`, {
        method: "POST",
        body: JSON.stringify({ recipient }),
      });
      resultBox.textContent = result.message || (result.ok ? "Test passed." : "Test failed.");
      resultBox.className = `notice ${result.ok ? "info" : "error"}`;
      resultBox.classList.remove("hidden");
    } catch (error) {
      resultBox.textContent = error.message;
      resultBox.className = "notice error";
      resultBox.classList.remove("hidden");
    }
  });
});

/* ------------------------------------------------------------------ */
/* Confirm dialog                                                      */
/* ------------------------------------------------------------------ */

function confirmAction(title, message, acceptLabel = "Confirm") {
  $("confirm-title").textContent = title;
  $("confirm-message").textContent = message;
  $("confirm-accept").textContent = acceptLabel;
  openModal("confirm-modal");
  return new Promise((resolve) => {
    state.confirmResolve = resolve;
  });
}

function settleConfirm(result) {
  closeModal("confirm-modal");
  const resolve = state.confirmResolve;
  state.confirmResolve = null;
  if (resolve) resolve(result);
}

$("confirm-accept").addEventListener("click", () => settleConfirm(true));
$("confirm-cancel").addEventListener("click", () => settleConfirm(false));
$("confirm-modal").addEventListener("click", (event) => {
  if (event.target === $("confirm-modal")) settleConfirm(false);
});

/* ------------------------------------------------------------------ */
/* Tabs, modals, shell                                                 */
/* ------------------------------------------------------------------ */

document.querySelectorAll(".console-tabs .tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    state.tab = tab.dataset.tab;
    document.querySelectorAll(".console-tabs .tab").forEach((item) => {
      const active = item === tab;
      item.classList.toggle("active", active);
      item.setAttribute("aria-selected", String(active));
    });
    document.querySelectorAll(".tab-panel").forEach((panel) => {
      panel.classList.toggle("hidden", panel.id !== `panel-${state.tab}`);
    });
  });
});

document.querySelectorAll("[data-close]").forEach((button) => {
  button.addEventListener("click", () => closeModal(button.dataset.close));
});

document.querySelectorAll(".modal-backdrop").forEach((backdrop) => {
  backdrop.addEventListener("click", (event) => {
    if (event.target === backdrop && backdrop.id !== "confirm-modal") {
      backdrop.classList.add("hidden");
    }
  });
});

$("new-campaign-button").addEventListener("click", () => openCampaignModal());
$("new-template-button").addEventListener("click", () => openTemplateModal());
$("new-sender-button").addEventListener("click", () => openSenderModal());
$("new-brand-button").addEventListener("click", () => openBrandModal());
$("refresh-button").addEventListener("click", (event) =>
  withBusy(event.currentTarget, "Refreshing…", refreshAll)
);

$("run-queue-button").addEventListener("click", async (event) => {
  await withBusy(event.currentTarget, "Running…", async () => {
    try {
      const result = await api("/api/queue/tick", { method: "POST" });
      notify(`Queue processed ${Number(result.processed || 0)} message(s).`);
      await refreshAll();
    } catch (error) {
      notify(error.message, "error");
    }
  });
});

$("logout-button").addEventListener("click", async () => {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch (error) {
    /* Ignore: redirect regardless. */
  }
  window.location.assign("/login");
});

async function initAuth() {
  try {
    const me = await api("/api/auth/me");
    if (!me.auth_enabled) {
      $("account-block").classList.add("hidden");
      $("logout-button").classList.add("hidden");
      return;
    }
    if (!me.authenticated) {
      window.location.assign("/login?next=/campaigns");
      return;
    }
    const email = me.email || "";
    $("account-email").textContent = email;
    $("account-avatar").textContent = (email.charAt(0) || "A").toUpperCase();
  } catch (error) {
    window.location.assign("/login?next=/campaigns");
  }
}

fillHourSelects();
renderMapping();
initAuth().finally(refreshAll);
