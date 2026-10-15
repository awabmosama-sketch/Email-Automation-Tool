const byId = (id) => document.getElementById(id);

let currentPitchSpamScore = 0;
let contactsData = [];
let editingContactEmail = null;

// Tab switching
const tabPitchBtn = byId("tab-pitch-btn");
const tabStandardBtn = byId("tab-standard-btn");
const pitchSection = byId("pitch-section");
const standardSection = byId("standard-section");
const tabPill = byId("tab-pill");

tabPitchBtn.addEventListener("click", () => {
  tabPitchBtn.classList.add("active");
  tabStandardBtn.classList.remove("active");
  pitchSection.classList.remove("hidden");
  standardSection.classList.add("hidden");
  tabPill.textContent = "B2B PITCH AI";
});

tabStandardBtn.addEventListener("click", () => {
  tabStandardBtn.classList.add("active");
  tabPitchBtn.classList.remove("active");
  standardSection.classList.remove("hidden");
  pitchSection.classList.add("hidden");
  tabPill.textContent = "AI POWERED";
});

// Helper for standard preview
function contactPayload() {
  return {
    name: byId("name").value,
    email: byId("email").value,
    context: byId("context").value,
  };
}

function showResult(message, valid) {
  const result = byId("verify-result");
  result.className = `result ${valid ? "valid" : "invalid"}`;
  result.textContent = message;
}

function showPitchResult(message, valid) {
  const result = byId("pitch-verify-result");
  result.className = `result ${valid ? "valid" : "invalid"}`;
  result.textContent = message;
}

function invalidateStandardPreview() {
  byId("preview").classList.add("hidden");
  byId("send-button").disabled = true;
  byId("send-button").textContent = "Send to this contact";
}

function invalidatePitchPreview() {
  byId("pitch-preview").classList.add("hidden");
  byId("pitch-send-button").disabled = true;
  byId("pitch-send-button").textContent = "Send Pitch";
  byId("pitch-spam-warning").classList.add("hidden");
}

function setContact(contact) {
  editingContactEmail = contact.email || null;
  // Populate Pitch Generator fields
  byId("pitch-name").value = contact.name || "";
  byId("pitch-email").value = contact.email || "";
  byId("pitch-phone").value = contact.phone || "";
  if (contact.company_name) {
    byId("pitch-company").value = contact.company_name;
  }
  byId("pitch-role").value = contact.target_role || "";
  byId("pitch-industry").value = contact.target_industry || "";
  byId("pitch-details").value = contact.details || contact.context || "";

  // Populate Standard fields
  byId("name").value = contact.name || "";
  byId("email").value = contact.email || "";
  byId("context").value = contact.context || contact.details || "";

  invalidateStandardPreview();
  invalidatePitchPreview();
  byId("pitch-save-button").textContent = "Save changes";
  byId("remove-contact-button").disabled = false;
  byId("verify-result").textContent = "";
  byId("pitch-verify-result").textContent = "";

  document.querySelectorAll(".contact").forEach((item) => {
    item.classList.toggle("selected", item.dataset.email === contact.email);
  });
}

async function loadCompanies(selectValue = null) {
  try {
    const response = await fetch("/companies");
    const data = await response.json();
    const select = byId("pitch-company");
    const currentVal = selectValue || select.value;
    if (data.companies && data.companies.length) {
      select.innerHTML = data.companies
        .map((c) => `<option value="${c}">${c}</option>`)
        .join("");
      if (currentVal && data.companies.includes(currentVal)) {
        select.value = currentVal;
      }
    } else {
      select.innerHTML = '<option value="" disabled selected>No company profiles</option>';
    }
  } catch (error) {
    console.error("Error loading companies:", error);
  }
}

byId("remove-company-btn").addEventListener("click", async () => {
  const companyName = byId("pitch-company").value;
  if (!companyName) return;
  if (!window.confirm(`Remove the pitching company '${companyName}'?`)) return;

  const response = await fetch(`/api/companies/${encodeURIComponent(companyName)}`, { method: "DELETE" });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    showPitchResult(data.detail || "Could not remove company.", false);
    return;
  }
  await loadCompanies();
  showPitchResult(data.message, true);
});

// Company Modal controls
const companyModal = byId("company-modal");
const openCompanyModalBtn = byId("open-company-modal-btn");
const closeCompanyModalBtn = byId("close-company-modal-btn");
const cancelCompanyModalBtn = byId("cancel-company-modal-btn");
const companyForm = byId("company-form");
const companyModalResult = byId("company-modal-result");

function openCompanyModal() {
  companyModal.classList.remove("hidden");
  companyModalResult.textContent = "";
  byId("new-company-name").focus();
}

function closeCompanyModal() {
  companyModal.classList.add("hidden");
  companyForm.reset();
  byId("new-company-tone").value = "friendly-professional";
}

if (openCompanyModalBtn) openCompanyModalBtn.addEventListener("click", openCompanyModal);
if (closeCompanyModalBtn) closeCompanyModalBtn.addEventListener("click", closeCompanyModal);
if (cancelCompanyModalBtn) cancelCompanyModalBtn.addEventListener("click", closeCompanyModal);

if (companyForm) {
  companyForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const saveBtn = byId("save-company-btn");
    saveBtn.disabled = true;
    saveBtn.textContent = "Saving...";
    companyModalResult.className = "result";
    companyModalResult.textContent = "Registering company profile...";

    const name = byId("new-company-name").value.trim();
    const rawServices = byId("new-company-services").value;
    const services = rawServices
      .split(/[\n,]/)
      .map((s) => s.trim())
      .filter(Boolean);
    const valueProp = byId("new-company-value-prop").value.trim();
    const tone = byId("new-company-tone").value.trim();
    const signature = byId("new-company-signature").value.trim();

    if (!name || services.length === 0) {
      companyModalResult.className = "result invalid";
      companyModalResult.textContent = "Company name and at least one service are required.";
      saveBtn.disabled = false;
      saveBtn.textContent = "Save Company Profile";
      return;
    }

    try {
      const response = await fetch("/companies", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: name,
          services: services,
          value_proposition: valueProp,
          tone: tone || "friendly-professional",
          signature: signature,
        }),
      });
      const data = await response.json();
      if (!response.ok) {
        companyModalResult.className = "result invalid";
        companyModalResult.textContent = data.detail || "Failed to save company.";
        saveBtn.disabled = false;
        saveBtn.textContent = "Save Company Profile";
        return;
      }

      await loadCompanies(name);
      closeCompanyModal();
      showPitchResult(`Company '${name}' added and selected.`, true);
    } catch (err) {
      companyModalResult.className = "result invalid";
      companyModalResult.textContent = "Network error saving company profile.";
    } finally {
      saveBtn.disabled = false;
      saveBtn.textContent = "Save Company Profile";
    }
  });
}

async function loadDashboard() {
  try {
    const [healthResponse, contactsResponse] = await Promise.all([
      fetch("/api/health"),
      fetch("/api/contacts"),
    ]);
    const health = await healthResponse.json();
    const contactData = await contactsResponse.json();
    contactsData = contactData.contacts || [];

    byId("status-dot").classList.add("ready");
    byId("status-text").textContent = "API online";
    byId("contact-count").textContent = contactData.count;
    byId("sync-time").textContent = `${health.contacts_loaded} loaded`;

    const list = byId("contacts-list");
    list.innerHTML = contactsData.length
      ? contactsData
          .map(
            (contact, index) => `
        <button class="contact" data-email="${contact.email}" data-index="${index}">
          <span>
            <span class="contact-name">${contact.name || "Unnamed"}</span>
            <span class="contact-email">${contact.email} ${contact.target_role ? `• ${contact.target_role}` : ""}</span>
          </span>
          <span class="contact-index">0${index + 1}</span>
        </button>`
          )
          .join("")
      : '<div class="empty">No contacts found.</div>';

    list.querySelectorAll(".contact").forEach((item) =>
      item.addEventListener("click", () =>
        setContact(contactsData[Number(item.dataset.index)])
      )
    );
  } catch (error) {
    byId("status-text").textContent = "API offline";
  }
}

// Pitch Form Inputs change
[
  "pitch-name",
  "pitch-email",
  "pitch-phone",
  "pitch-company",
  "pitch-role",
  "pitch-industry",
  "pitch-details",
].forEach((id) => {
  const el = byId(id);
  if (el) el.addEventListener("input", invalidatePitchPreview);
});

// Pitch verify address
byId("pitch-verify-button").addEventListener("click", async () => {
  const email = byId("pitch-email").value.trim();
  if (!email) {
    showPitchResult("Please enter an email address to check.", false);
    return;
  }
  showPitchResult("Verifying address...", true);
  try {
    const response = await fetch("/api/verify?check_dns=true", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: email, name: byId("pitch-name").value || "lead" }),
    });
    const data = await response.json();
    showPitchResult(
      data.valid ? `Address looks good (${data.reason})` : `Address rejected: ${data.reason}`,
      data.valid
    );
  } catch (error) {
    showPitchResult("Verification request failed.", false);
  }
});

// Pitch save contact
byId("pitch-save-button").addEventListener("click", async () => {
  const payload = {
    name: byId("pitch-name").value.trim(),
    email: byId("pitch-email").value.trim(),
    phone: byId("pitch-phone").value.trim(),
    company_name: byId("pitch-company").value,
    target_role: byId("pitch-role").value.trim(),
    target_industry: byId("pitch-industry").value.trim(),
    details: byId("pitch-details").value.trim(),
  };

  if (!payload.name || !payload.email) {
    showPitchResult("Name and email are required to save a contact.", false);
    return;
  }

  try {
    const endpoint = editingContactEmail
      ? `/api/contacts/${encodeURIComponent(editingContactEmail)}`
      : "/api/contacts";
    const response = await fetch(endpoint, {
      method: editingContactEmail ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) {
      showPitchResult(data.detail || "Could not add contact.", false);
      return;
    }
    showPitchResult(data.message, true);
    editingContactEmail = data.contact.email;
    byId("pitch-save-button").textContent = "Save changes";
    await loadDashboard();
  } catch (error) {
    showPitchResult("Could not reach API.", false);
  }
});

byId("remove-contact-button").addEventListener("click", async () => {
  if (!editingContactEmail) return;
  const contact = contactsData.find((item) => item.email === editingContactEmail);
  const label = contact?.name || editingContactEmail;
  if (!window.confirm(`Remove ${label} from your contacts?`)) return;

  const response = await fetch(`/api/contacts/${encodeURIComponent(editingContactEmail)}`, { method: "DELETE" });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    showPitchResult(data.detail || "Could not remove contact.", false);
    return;
  }

  editingContactEmail = null;
  byId("remove-contact-button").disabled = true;
  byId("pitch-save-button").textContent = "Save contact";
  showPitchResult(data.message, true);
  await loadDashboard();
});

// Pitch Generate Preview
byId("pitch-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const generateBtn = byId("pitch-generate-button");
  generateBtn.disabled = true;
  generateBtn.textContent = "Crafting pitch...";
  showPitchResult("Generating tailored pitch email...", true);

  const payload = {
    name: byId("pitch-name").value.trim(),
    email: byId("pitch-email").value.trim(),
    phone: byId("pitch-phone").value.trim(),
    company_name: byId("pitch-company").value,
    target_role: byId("pitch-role").value.trim(),
    target_industry: byId("pitch-industry").value.trim(),
    details: byId("pitch-details").value.trim(),
  };

  try {
    const response = await fetch("/pitch/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();

    if (!response.ok) {
      showPitchResult(data.detail || "Pitch generation failed.", false);
      return;
    }

    showPitchResult("Pitch generated successfully.", true);
    byId("pitch-preview-subject").textContent = data.subject;
    byId("pitch-preview-body").textContent = data.body;
    byId("pitch-preview-mode").textContent = `${payload.company_name} PITCH PREVIEW`;

    currentPitchSpamScore = Number(data.spam_score) || 0;
    const badge = byId("pitch-spam-badge");
    badge.className = "spam-badge";

    if (currentPitchSpamScore <= 3) {
      badge.classList.add("spam-low");
      badge.textContent = `Spam Score: ${currentPitchSpamScore}/10 (Clean)`;
      byId("pitch-spam-warning").classList.add("hidden");
    } else if (currentPitchSpamScore <= 6) {
      badge.classList.add("spam-med");
      badge.textContent = `Spam Score: ${currentPitchSpamScore}/10 (Moderate)`;
      byId("pitch-spam-warning").classList.add("hidden");
    } else {
      badge.classList.add("spam-high");
      badge.textContent = `Spam Score: ${currentPitchSpamScore}/10 (High Risk)`;
      byId("pitch-spam-warning").classList.remove("hidden");
    }

    byId("pitch-preview").classList.remove("hidden");
    byId("pitch-send-button").disabled = false;
    byId("pitch-send-button").textContent = "Send Pitch";
  } catch (error) {
    showPitchResult("Error connecting to server for pitch generation.", false);
  } finally {
    generateBtn.disabled = false;
    generateBtn.innerHTML = "Generate Preview <span>&rarr;</span>";
  }
});

// Pitch Send
byId("pitch-send-button").addEventListener("click", async () => {
  const recipientEmail = byId("pitch-email").value.trim();
  const subject = byId("pitch-preview-subject").textContent.trim();
  const body = byId("pitch-preview-body").textContent.trim();
  const companyName = byId("pitch-company").value;

  if (!recipientEmail || !subject || !body) {
    showPitchResult("Cannot send: missing pitch preview content.", false);
    return;
  }

  if (currentPitchSpamScore >= 7) {
    const explicitConfirm = window.confirm(
      `⚠️ HIGH SPAM SCORE ALERT (Score: ${currentPitchSpamScore}/10)\n\n` +
      `This email has a high risk of being flagged as spam by mail filters.\n\n` +
      `Do you explicitly confirm that you want to proceed and send this pitch to ${recipientEmail}?`
    );
    if (!explicitConfirm) return;
  } else {
    const normalConfirm = window.confirm(
      `Are you ready to send this pitch email to ${recipientEmail}?`
    );
    if (!normalConfirm) return;
  }

  const sendBtn = byId("pitch-send-button");
  sendBtn.disabled = true;
  sendBtn.textContent = "Sending pitch...";

  try {
    const response = await fetch("/pitch/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        recipient_email: recipientEmail,
        subject: subject,
        body: body,
        company_name: companyName,
        spam_score: currentPitchSpamScore,
      }),
    });
    const data = await response.json();

    if (response.ok && data.status === "sent") {
      showPitchResult(data.message, true);
      sendBtn.textContent = "Pitch sent!";
    } else {
      showPitchResult(data.message || data.detail || "Pitch delivery failed.", false);
      sendBtn.disabled = false;
      sendBtn.textContent = "Send Pitch";
    }
  } catch (error) {
    showPitchResult("Network error sending pitch email.", false);
    sendBtn.disabled = false;
    sendBtn.textContent = "Send Pitch";
  }
});

// Standard Preview Form handlers (preserved)
["name", "email", "context"].forEach((fieldId) =>
  byId(fieldId).addEventListener("input", invalidateStandardPreview)
);

byId("verify-button").addEventListener("click", async () => {
  const result = byId("verify-result");
  result.textContent = "Checking...";
  const response = await fetch("/api/verify", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(contactPayload()),
  });
  const data = await response.json();
  showResult(
    data.valid ? `Address looks good (${data.reason})` : `Address rejected: ${data.reason}`,
    data.valid
  );
});

byId("add-button").addEventListener("click", async () => {
  try {
    const response = await fetch("/api/contacts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(contactPayload()),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      showResult(data.detail || "Could not add contact.", false);
      return;
    }
    showResult(data.message, true);
    await loadDashboard();
  } catch (error) {
    showResult("Could not reach the API.", false);
  }
});

byId("preview-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const response = await fetch("/api/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(contactPayload()),
  });
  const data = await response.json();
  if (!response.ok) {
    showResult(data.detail || "AI preview failed.", false);
    return;
  }
  byId("preview-subject").textContent = data.subject;
  byId("preview-body").textContent = data.body;
  byId("preview-mode").textContent = data.mode.replaceAll("_", " ");
  byId("preview").classList.remove("hidden");
  byId("send-button").disabled = false;
});

byId("send-button").addEventListener("click", async () => {
  byId("send-button").disabled = true;
  byId("send-button").textContent = "Sending...";
  const response = await fetch("/api/send", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ...contactPayload(),
      subject: byId("preview-subject").textContent,
      body: byId("preview-body").textContent,
    }),
  });
  const data = await response.json().catch(() => ({}));
  showResult(
    response.ok ? data.message : data.detail || "Email could not be sent.",
    response.ok
  );
  byId("send-button").textContent = response.ok ? "Email sent" : "Send to this contact";
  if (!response.ok) byId("send-button").disabled = false;
});

// Initialization
loadCompanies();
loadDashboard();