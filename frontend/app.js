// API_BASE: same-origin by default (backend serves this file); override by
// setting window.API_BASE before this script loads if frontend/backend are
// on different hosts (e.g. Cloudflare Pages talking to a Fly.io API).
const API_BASE = window.API_BASE || "";

const $ = (sel) => document.querySelector(sel);

// Company/title/apply_link can come from scraped job sites or a pasted
// apply link — untrusted text going into innerHTML, so escape before
// interpolating (apply_link doubles as an href, which is the sharper risk).
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function getToken() {
  return localStorage.getItem("resume_agent_token") || "";
}

async function api(path, opts = {}) {
  const res = await fetch(API_BASE + path, {
    ...opts,
    headers: {
      "Content-Type": "application/json",
      "Authorization": `Bearer ${getToken()}`,
      ...(opts.headers || {}),
    },
  });
  if (res.status === 401) {
    localStorage.removeItem("resume_agent_token");
    showLogin("Session expired — please sign in again.");
    throw new Error("Unauthorized");
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed (${res.status})`);
  }
  return res.json();
}

function showLogin(error) {
  $("#login-screen").classList.remove("hidden");
  $("#app").classList.add("hidden");
  $("#login-error").textContent = error || "";
}

function showApp() {
  $("#login-screen").classList.add("hidden");
  $("#app").classList.remove("hidden");
}

// ── Login ────────────────────────────────────────────────────────────────
$("#login-btn").addEventListener("click", async () => {
  const password = $("#password-input").value;
  if (!password) return;
  localStorage.setItem("resume_agent_token", password);
  try {
    // Any authed GET proves the password is correct before we commit to it.
    await api("/api/entries");
    showApp();
    loadEntries();
    startPolling();
  } catch (e) {
    localStorage.removeItem("resume_agent_token");
    $("#login-error").textContent = "Incorrect password.";
  }
});
$("#password-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("#login-btn").click();
});

// ── Tabs ─────────────────────────────────────────────────────────────────
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.add("hidden"));
    tab.classList.add("active");
    $(`#tab-${tab.dataset.tab}`).classList.remove("hidden");
    if (tab.dataset.tab === "applications") loadEntries();
    if (tab.dataset.tab === "scraper") { loadScraperSettings(); loadAtsCompanies(); loadLinkedInCompanies(); }
  });
});

// ── Add JD ───────────────────────────────────────────────────────────────
// Submitting only ever POSTs and clears the form immediately — it never
// waits for scoring or generation. The new entry shows up in the
// Applications tab and fills itself in on its own via polling.
$("#add-btn").addEventListener("click", async () => {
  const company = $("#company").value.trim();
  const title = $("#title").value.trim();
  const jd = $("#jd").value.trim();
  const apply_link = $("#apply-link").value.trim();
  if (!company || !jd) {
    $("#add-status").innerHTML = `<div class="error-msg">Company and job description are required.</div>`;
    return;
  }

  $("#add-btn").disabled = true;
  try {
    const result = await api("/api/entries", {
      method: "POST",
      body: JSON.stringify({ company, title, jd, apply_link }),
    });
    $("#company").value = "";
    $("#title").value = "";
    $("#jd").value = "";
    $("#apply-link").value = "";
    $("#add-status").innerHTML = result.duplicate
      ? `<div class="error-msg">Already in your Applications tab as "${result.company} — ${result.title}" (status: ${result.apply_status}). Not added again.</div>`
      : `<div class="spinner-note">Added — scoring in the background. Check the Applications tab any time; you don't need to wait here.</div>`;
    loadEntries();
    startPolling();
  } catch (e) {
    $("#add-status").innerHTML = `<div class="error-msg">${e.message}</div>`;
  } finally {
    $("#add-btn").disabled = false;
  }
});

// ── Applications list ────────────────────────────────────────────────────
$("#refresh-entries").addEventListener("click", loadEntries);

let entriesCache = [];
// JD text is omitted from the list endpoint (can be long) — fetched
// on-demand the first time an entry is expanded, then cached here so
// re-expanding or a background poll re-render doesn't re-fetch it.
const expandedJdIds = new Set();
const jdTextCache = {};

async function loadEntries() {
  try {
    entriesCache = await api("/api/entries");
    renderEntries();
  } catch (e) {
    $("#entries-list").innerHTML = `<div class="error-msg">${e.message}</div>`;
  }
}

// ── Date filter + sort ───────────────────────────────────────────────────
// Filters by the "added" date (created_at) — a single exact-day match, not
// a range — and optionally re-sorts by score. Both client-side only,
// entriesCache itself is untouched so it still holds everything for counts
// like "Retry all failed" and the polling in-flight check.
function filteredEntries() {
  const day = $("#filter-date").value;   // "YYYY-MM-DD" or ""
  let list = entriesCache;
  if (day) {
    list = list.filter((e) => (e.created_at || "").slice(0, 10) === day);
  }
  // Searches company, title, AND the full JD text (already in entriesCache
  // from /api/entries — no extra fetch needed) — this is specifically for
  // "did I already apply to this posting" lookups across 50+ entries, where
  // the distinguishing detail is sometimes only in the JD body, not the
  // company/title shown on the card.
  const search = $("#filter-search").value.trim().toLowerCase();
  if (search) {
    list = list.filter((e) =>
      (e.company || "").toLowerCase().includes(search)
      || (e.title || "").toLowerCase().includes(search)
      || (e.jd || "").toLowerCase().includes(search)
    );
  }
  if ($("#hide-applied-toggle").checked) {
    list = list.filter((e) => !e.applied);
  }
  if ($("#hide-not-applying-toggle").checked) {
    list = list.filter((e) => !e.not_applying);
  }
  // Cap-missed entries (cleared the score bar but fell outside that cycle's
  // daily cap) are a waitlist: kept on disk, hidden unless asked for.
  if (!$("#show-waitlist-toggle").checked) {
    list = list.filter((e) => !e.cap_missed);
  }
  if ($("#sort-select").value === "score") {
    list = [...list].sort((a, b) => (b.score ?? -1) - (a.score ?? -1));
  }
  return list;
}

// Defaults to today so the toolbar opens showing "what came in today"
// rather than every entry ever added — Clear (below) is the explicit
// escape hatch back to seeing everything.
function todayDateString() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}
$("#filter-date").value = todayDateString();

// Native date inputs only open the picker when you hit the tiny calendar
// icon in most browsers — clicking anywhere else just puts a text cursor in
// an unfamiliar mm/dd/yyyy field. showPicker() makes the whole box behave
// like a normal clickable control. Feature-detected since Safari didn't
// support it for a while; falls back to default (icon-only) behavior there.
$("#filter-date").addEventListener("click", () => {
  if (typeof $("#filter-date").showPicker === "function") {
    $("#filter-date").showPicker();
  }
});

$("#filter-search").addEventListener("input", renderEntries);
$("#filter-date").addEventListener("change", renderEntries);
$("#sort-select").addEventListener("change", renderEntries);
$("#hide-applied-toggle").addEventListener("change", renderEntries);
$("#hide-not-applying-toggle").addEventListener("change", renderEntries);
$("#show-waitlist-toggle").addEventListener("change", renderEntries);
$("#filter-clear").addEventListener("click", () => {
  $("#filter-search").value = "";
  $("#filter-date").value = "";
  $("#sort-select").value = "date";
  $("#hide-applied-toggle").checked = false;
  $("#hide-not-applying-toggle").checked = false;
  $("#show-waitlist-toggle").checked = false;
  renderEntries();
});

function renderEntries() {
  const visible = filteredEntries();
  $("#entries-list").innerHTML = visible.length
    ? visible.map(entryRowHtml).join("")
    : entriesCache.length
      ? `<div class="spinner-note">Nothing matches these filters — try Clear filters to see everything.</div>`
      : `<div class="spinner-note">No applications yet — add a JD to get started.</div>`;
  const unappliedCount = visible.filter((e) => !e.applied && !e.not_applying).length;
  const waitlistHidden = $("#show-waitlist-toggle").checked
    ? 0 : entriesCache.filter((e) => e.cap_missed).length;
  $("#entries-count").textContent = visible.length
    ? `${visible.length} shown · ${unappliedCount} not yet applied`
      + (waitlistHidden ? ` · ${waitlistHidden} waitlisted (hidden)` : "")
    : "";
  // Re-attach button listeners each render (innerHTML wipes them).
  document.querySelectorAll("[data-generate-id]").forEach((btn) => {
    btn.addEventListener("click", () => onGenerateClick(btn.dataset.generateId));
  });
  document.querySelectorAll("[data-stop-generate-id]").forEach((btn) => {
    btn.addEventListener("click", () => onStopGenerateClick(btn.dataset.stopGenerateId));
  });
  document.querySelectorAll("[data-rescore-id]").forEach((btn) => {
    btn.addEventListener("click", () => onRescoreClick(btn.dataset.rescoreId));
  });
  document.querySelectorAll("[data-toggle-applied-id]").forEach((btn) => {
    btn.addEventListener("click", () => onToggleAppliedClick(btn.dataset.toggleAppliedId));
  });
  document.querySelectorAll("[data-toggle-not-applying-id]").forEach((btn) => {
    btn.addEventListener("click", () => onToggleNotApplyingClick(btn.dataset.toggleNotApplyingId));
  });
  document.querySelectorAll("[data-toggle-jd-id]").forEach((btn) => {
    btn.addEventListener("click", () => onToggleJdClick(btn.dataset.toggleJdId));
  });
  document.querySelectorAll("[data-delete-id]").forEach((btn) => {
    btn.addEventListener("click", () => onDeleteClick(btn.dataset.deleteId, btn.dataset.deleteLabel));
  });
  const failedCount = entriesCache.filter((e) => e.score_status === "error").length;
  $("#rescore-failed-btn").classList.toggle("hidden", failedCount === 0);
  $("#rescore-failed-btn").textContent = `Retry all failed (${failedCount})`;
}

// Admin-y bulk actions live behind one toggle so the everyday toolbar
// (date filter, refresh) stays short.
$("#manage-toggle-btn").addEventListener("click", () => {
  $("#manage-panel").classList.toggle("hidden");
  if (!$("#manage-panel").classList.contains("hidden")) loadEmailOverrides();
});

// ── Email overrides ─────────────────────────────────────────────────────
// Per-company email swap, applied by the pipeline's finalizer for every
// future resume/CL whose company name contains the pattern (case-insensitive
// substring match) — e.g. pattern "microsoft" covers "Microsoft",
// "Microsoft Corporation", etc. Configured once here instead of hand-editing
// each generated file.
async function loadEmailOverrides() {
  try {
    const rules = await api("/api/email-overrides");
    $("#email-override-list").innerHTML = rules.length
      ? rules.map((r) => `
          <div class="run-row" style="padding:6px 0;">
            <span class="mono-label">"${escapeHtml(r.pattern)}" → ${escapeHtml(r.email)}</span>
            <button data-remove-override="${escapeHtml(r.pattern)}" class="secondary" style="margin-top:0;padding:4px 10px;">Remove</button>
          </div>`).join("")
      : `<div class="spinner-note">No overrides configured — every resume uses the default email.</div>`;
    document.querySelectorAll("[data-remove-override]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          await api(`/api/email-overrides/${encodeURIComponent(btn.dataset.removeOverride)}`, { method: "DELETE" });
          loadEmailOverrides();
        } catch (e) {
          alert(e.message);
        }
      });
    });
  } catch (e) {
    $("#email-override-list").innerHTML = `<div class="error-msg">${e.message}</div>`;
  }
}

$("#email-override-add-btn").addEventListener("click", async () => {
  const pattern = $("#email-override-pattern").value.trim();
  const email = $("#email-override-email").value.trim();
  if (!pattern || !email) {
    alert("Both a company-name pattern and an email are required.");
    return;
  }
  try {
    await api("/api/email-overrides", { method: "POST", body: JSON.stringify({ pattern, email }) });
    $("#email-override-pattern").value = "";
    $("#email-override-email").value = "";
    loadEmailOverrides();
  } catch (e) {
    alert(e.message);
  }
});

// Backfill for jobs scored before the scraper switched from writing to
// Google Sheets to writing directly into this Applications tab. Scoped by
// the min-score input here and the single date filter above (reused, not
// duplicated) — otherwise every click re-imports the entire sheet history.
$("#import-sheet-btn").addEventListener("click", async () => {
  const min_score = parseInt($("#import-min-score").value, 10) || 0;
  const filterDay = $("#filter-date").value;
  const date_from = filterDay;
  const date_to = filterDay;
  $("#import-sheet-btn").disabled = true;
  $("#import-sheet-status").innerHTML = `<div class="spinner-note">Reading sheet and cross-referencing job_cache.json...</div>`;
  try {
    const r = await api("/api/entries/import-from-sheet", {
      method: "POST",
      body: JSON.stringify({ min_score, date_from, date_to }),
    });
    const total = r.imported_full + r.imported_summary_only;
    $("#import-sheet-status").innerHTML = total
      ? `<div class="spinner-note">Imported ${total} job(s) — ${r.imported_full} with full JD text, ${r.imported_summary_only} with sheet summary only (job_cache had expired). Skipped: ${r.skipped_existing} already tracked, ${r.skipped_below_threshold} below min score, ${r.skipped_out_of_range} outside date range.</div>`
      : `<div class="spinner-note">Nothing matched — ${r.skipped_existing} already tracked, ${r.skipped_below_threshold} below min score, ${r.skipped_out_of_range} outside date range.</div>`;
    loadEntries();
  } catch (e) {
    $("#import-sheet-status").innerHTML = `<div class="error-msg">${e.message}</div>`;
  } finally {
    $("#import-sheet-btn").disabled = false;
  }
});

// Declutter: bulk-delete every scored entry below a threshold. Skips
// anything marked applied (real record of action, not clutter) and
// anything still scoring/errored (nothing to judge yet). Only removes
// entries.json rows — generated files and all score caches are untouched.
$("#delete-below-btn").addEventListener("click", async () => {
  const min_score = parseInt($("#delete-threshold-input").value, 10) || 0;
  const also_delete_from_sheet = $("#delete-also-sheet").checked;
  const preview = entriesCache.filter(
    (e) => e.score_status === "scored" && (e.score || 0) < min_score && !e.applied
  ).length;
  if (!preview && !also_delete_from_sheet) {
    alert(`No scored entries below ${min_score} to delete (entries you've marked applied are always kept).`);
    return;
  }
  const sheetNote = also_delete_from_sheet ? " Also rewrites the Google Sheet, keeping only rows scoring at or above this (or marked Applied)." : "";
  if (!confirm(`Delete ${preview} entr${preview === 1 ? "y" : "ies"} scored below ${min_score}?${sheetNote} Applied entries are kept regardless of score. Nothing on disk (generated files, caches) is touched.`)) {
    return;
  }
  $("#delete-below-btn").disabled = true;
  try {
    const r = await api("/api/entries/delete-below-threshold", {
      method: "POST",
      body: JSON.stringify({ min_score, skip_applied: true, also_delete_from_sheet }),
    });
    let msg = `Deleted ${r.deleted_count} entr${r.deleted_count === 1 ? "y" : "ies"} below ${min_score}.`;
    if (also_delete_from_sheet) {
      msg += r.sheet_error
        ? ` Sheet cleanup failed: ${r.sheet_error}`
        : ` Sheet: deleted ${r.sheet_deleted_count}, kept ${r.sheet_kept_count}.`;
    }
    $("#import-sheet-status").innerHTML = `<div class="spinner-note">${msg}</div>`;
    loadEntries();
  } catch (e) {
    alert(e.message);
  } finally {
    $("#delete-below-btn").disabled = false;
  }
});

async function onToggleJdClick(entryId) {
  if (expandedJdIds.has(entryId)) {
    expandedJdIds.delete(entryId);
    renderEntries();
    return;
  }
  expandedJdIds.add(entryId);
  if (!jdTextCache[entryId]) {
    try {
      const full = await api(`/api/entries/${entryId}`);
      jdTextCache[entryId] = full.jd || "(no job description text)";
    } catch (e) {
      jdTextCache[entryId] = `Failed to load: ${e.message}`;
    }
  }
  renderEntries();
}

async function onDeleteClick(entryId, label) {
  if (!confirm(`Delete "${label}" from the list? Any already-generated resume/cover letter files are kept on disk — this only removes it from tracking.`)) {
    return;
  }
  try {
    await api(`/api/entries/${entryId}`, { method: "DELETE" });
    expandedJdIds.delete(entryId);
    delete jdTextCache[entryId];
    loadEntries();
  } catch (e) {
    alert(e.message);
  }
}

// Score badge only — the number/verdict, no reasoning text. Reasoning/red
// flags render separately in .entry-summary so the score itself stays a
// compact glanceable badge instead of a paragraph.
function scoreBadgeHtml(e) {
  if (e.score_status === "scoring") {
    return `<span class="mono-label">SCORING...</span>`;
  }
  if (e.score_status === "error") {
    return `
      <span class="error-msg">Scoring failed: ${e.score_error || "unknown error"}</span>
      <button data-rescore-id="${e.id}" class="secondary" style="margin-top:6px;padding:4px 12px;">Retry</button>`;
  }
  const cls = e.proceed ? "pass" : "fail";
  return `
    <span class="verdict-score ${cls}" style="font-size:1.3rem;">${e.score}/100</span>
    <span class="mono-label">${e.proceed ? "PASSES BAR" : "BELOW THRESHOLD"}</span>`;
}

function summaryHtml(e) {
  const parts = [];
  if (e.reasoning) parts.push(`<div>${e.reasoning}</div>`);
  if (e.red_flags && e.red_flags.length) parts.push(`<div>Red flags: ${e.red_flags.join(", ")}</div>`);
  return parts.length ? `<div class="entry-summary">${parts.join("")}</div>` : "";
}

// tailoring_recommended is a SEPARATE judgment from score/proceed — score
// answers "should I apply to this job at all" (job vs. full profile);
// tailoring_recommended answers "does my BASE RESUME, as already written,
// already cover this specific JD, or would tailoring help" (base resume
// text vs. this JD). An earlier version of this hint just thresholded the
// apply-score itself, which conflated the two — a job can score 95 on fit
// while the base resume still misses something this JD specifically wants.
// A pure hint either way: Generate stays fully available regardless.
function tailoringHintHtml(e) {
  if (e.tailoring_recommended === false) {
    return `<div class="tailoring-hint tailoring-skip">✓ ${escapeHtml(e.tailoring_reasoning) || "Your base resume likely covers this."} Tailoring is optional here.</div>`;
  }
  if (e.tailoring_recommended === true) {
    return `<div class="tailoring-hint tailoring-needed">✎ ${escapeHtml(e.tailoring_reasoning) || "Tailoring recommended for this posting."}</div>`;
  }
  return ""; // null/undefined — scored before this field existed, no verdict yet
}

function generateButtonHtml(e, label) {
  return `
    ${tailoringHintHtml(e)}
    <div class="checkbox-row" style="margin-top:0;">
      <input type="checkbox" id="cl-${e.id}" />
      <label for="cl-${e.id}">Also cover letter</label>
    </div>
    <button data-generate-id="${e.id}" class="secondary" style="margin-top:0;padding:8px 16px;">${label}</button>`;
}

// The backend doesn't expose real per-stage pipeline progress (it's one
// opaque synchronous LangGraph call — should_apply → triage → strategist →
// writer → editor → finalizer — with no intermediate status written back to
// the entry), so this is a time-estimated bar, not a true one: it fills
// toward the typical 30-90s duration and is deliberately capped short of
// 100% so it never visually claims "done" before the real status flips away
// from "running" on the next poll (every 3s, via startPolling). Honest about
// the estimate in the label rather than pretending to know the real stage.
const GENERATE_EXPECTED_SECONDS = 75; // midpoint of the observed 30-90s range
const GENERATE_PROGRESS_CAP = 92; // never shown as done from elapsed-time alone

function generateProgressHtml(e) {
  const startedAt = e.apply_started_at ? new Date(e.apply_started_at).getTime() : null;
  const elapsedSec = startedAt ? Math.max(0, Math.round((Date.now() - startedAt) / 1000)) : 0;
  const pct = Math.min(GENERATE_PROGRESS_CAP, Math.round((elapsedSec / GENERATE_EXPECTED_SECONDS) * 100));
  return `
    <div style="width:100%;">
      <div class="spinner-note">Generating — ${elapsedSec}s elapsed (usually 30–90s)...</div>
      <div style="background:var(--hairline);border-radius:6px;overflow:hidden;height:8px;margin-top:6px;">
        <div style="background:var(--blue);height:100%;width:${pct}%;transition:width 0.6s linear;"></div>
      </div>
    </div>`;
}

// Action buttons only (Generate/Stop/Retry + the cover-letter checkbox).
// Status messages (generating/cancelled/error/skipped) render as their own
// line above the buttons, inside .entry-actions, so they read as part of
// the action area rather than mixed into the summary.
function generateActionsHtml(e) {
  if (e.score_status === "scoring") return ""; // nothing to do yet
  if (e.apply_status === "idle") {
    return generateButtonHtml(e, "Generate");
  }
  if (e.apply_status === "running") {
    return generateProgressHtml(e)
      + `<button data-stop-generate-id="${e.id}" class="secondary danger" style="margin-top:0;padding:8px 16px;">Stop</button>`;
  }
  if (e.apply_status === "cancelled") {
    return `<span class="error-msg">${e.apply_error || "Cancelled."}</span>` + generateButtonHtml(e, "Retry");
  }
  if (e.apply_status === "skipped_by_gate") {
    return `<span class="spinner-note">Pipeline's own gate declined this posting — no documents generated.</span>` + generateButtonHtml(e, "Retry");
  }
  if (e.apply_status === "error") {
    // The backend only blocks a retry while apply_status is "running" —
    // an errored attempt is always safe to retry, so the button must stay
    // visible here too, not just for the never-tried "idle" state.
    return `<span class="error-msg">${e.apply_error || "Generation failed."}</span>` + generateButtonHtml(e, "Retry");
  }
  return "";
}

// Generated resume/CL download links — its own section, separate from the
// action buttons, so "here's what got produced" is visually distinct from
// "here's what you can click to do something".
function generatedFilesHtml(e) {
  if (e.apply_status !== "completed") return "";
  const links = (e.files || [])
    .map((f) => `<a href="${API_BASE}/api/entries/${e.id}/download/${encodeURIComponent(f)}?token=${getToken()}" target="_blank">${f}</a>`)
    .join("");
  return `<div class="entry-generated"><div class="run-links">${links || "No files produced."}</div></div>`;
}

function formatDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function applyLinkHtml(e) {
  const link = (e.apply_link || "").trim();
  if (!link || !/^https?:\/\//i.test(link)) return "";
  return `<a href="${escapeHtml(link)}" target="_blank" rel="noopener noreferrer" class="secondary" style="display:inline-block;text-decoration:none;margin-top:0;padding:6px 14px;">Apply Link ↗</a>`;
}

function appliedToggleHtml(e) {
  const label = e.applied ? `Applied ${formatDate(e.applied_at)}` : "Mark as applied";
  const cls = e.applied ? "secondary applied" : "secondary";
  return `<button data-toggle-applied-id="${e.id}" class="${cls}" style="padding:4px 12px;">${label}</button>`;
}

// Distinct from Delete: "not applying" keeps the entry (and its score/JD)
// around for reference instead of removing it, but visually sets it apart
// so it stops competing for attention with postings still worth acting on.
function notApplyingToggleHtml(e) {
  const label = e.not_applying ? `Not applying (${formatDate(e.not_applying_at)}) — undo` : "Not applying";
  const cls = e.not_applying ? "secondary not-applying active" : "secondary not-applying";
  return `<button data-toggle-not-applying-id="${e.id}" class="${cls}" style="padding:4px 12px;">${label}</button>`;
}

function jdToggleHtml(e) {
  const expanded = expandedJdIds.has(e.id);
  return `<button data-toggle-jd-id="${e.id}" class="secondary" style="padding:4px 12px;">${expanded ? "Hide JD ▴" : "View JD ▾"}</button>`;
}

function jdPanelHtml(e) {
  if (!expandedJdIds.has(e.id)) return "";
  const text = jdTextCache[e.id];
  const body = text === undefined ? "Loading…" : escapeHtml(text);
  return `<div class="jd-panel">${body}</div>`;
}

function deleteButtonHtml(e) {
  const label = escapeHtml(`${e.company || "?"} — ${e.title || "?"}`).replace(/"/g, "&quot;");
  return `<button data-delete-id="${e.id}" data-delete-label="${label}" class="secondary danger" style="padding:6px 16px;">Delete</button>`;
}

// Scraper-sourced entries that matched an existing one (by JD hash or apply
// link, e.g. a job board reposting the same JD under a new listing ID) are
// still created rather than silently dropped — this banner is how that
// match surfaces, so the user decides whether it's really the same posting
// instead of the scraper deciding for them.
function duplicateWarningHtml(e) {
  if (!e.possible_duplicate) return "";
  return `<div class="duplicate-warning">⚠️ ${escapeHtml(e.duplicate_note) || "Possible duplicate of an existing entry."}</div>`;
}

// Clear, separately-bounded sections: header (company/title/date/source/apply
// link) + score, summary (reasoning/red flags), generated files, action
// buttons (generate/retry/stop/mark-applied/view JD), JD panel, and finally
// a visually separated footer holding only the (red) delete button — so
// each kind of information has its own place instead of one flowing block.
function entryRowHtml(e) {
  // Scraped facts (location / remote / posted date / salary) were always
  // fetched but never persisted onto entries until rubric v2 — older
  // entries simply won't have them, so each is optional here.
  const meta = [
    formatDate(e.created_at),
    e.source === "scraper" ? (e.platform ? `scraped · ${e.platform}` : "scraped") : "manual",
    e.is_remote ? "remote" : (e.location || ""),
    e.date_posted ? `posted ${formatDate(e.date_posted)}` : "",
    e.salary || "",
  ].filter(Boolean).join(" · ");
  // Scored under an older rubric: the number on this card is not comparable
  // to current scores. Only flagged on real LLM scores — red-flag rejects
  // (score 0) don't depend on the rubric.
  const staleRubric = e.score_status === "scored" && e.stage !== "red_flag"
    && metaCache && e.rubric_version !== metaCache.rubric_version;
  const staleBadge = staleRubric
    ? `<div class="stale-rubric">⟳ Scored under an older rubric (v${e.rubric_version ?? "1"}) — this number isn't comparable to current scores. <button data-rescore-id="${e.id}" class="secondary" style="padding:2px 10px;margin-left:6px;">Rescore</button></div>`
    : "";
  return `
    <div class="entry-card ${e.applied ? "applied-row" : ""} ${e.not_applying ? "not-applying-row" : ""} ${e.cap_missed ? "cap-missed-row" : ""}">
      <div class="entry-header">
        <div>
          ${e.applied ? `<span class="applied-badge">✓ APPLIED</span>` : ""}
          ${e.not_applying ? `<span class="not-applying-badge">✕ NOT APPLYING</span>` : ""}
          ${e.cap_missed ? `<span class="cap-missed-badge" title="Cleared the score bar but fell outside that cycle's daily cap">⏳ WAITLIST</span>` : ""}
          <div class="entry-company">${escapeHtml(e.company) || "?"}</div>
          <div class="entry-jobtitle">${escapeHtml(e.title) || "?"}</div>
          <div class="entry-meta">
            <span class="mono-label">${meta}</span>
            ${applyLinkHtml(e)}
          </div>
        </div>
        <div class="entry-score">${scoreBadgeHtml(e)}</div>
      </div>
      ${duplicateWarningHtml(e)}
      ${staleBadge}
      ${summaryHtml(e)}
      ${generatedFilesHtml(e)}
      <div class="entry-actions">
        ${generateActionsHtml(e)}
        ${appliedToggleHtml(e)}
        ${notApplyingToggleHtml(e)}
        ${jdToggleHtml(e)}
      </div>
      ${jdPanelHtml(e)}
      <div class="entry-footer">
        ${deleteButtonHtml(e)}
      </div>
    </div>`;
}

async function onToggleAppliedClick(entryId) {
  try {
    await api(`/api/entries/${entryId}/toggle-applied`, { method: "POST" });
    loadEntries();
  } catch (e) {
    alert(e.message);
  }
}

async function onToggleNotApplyingClick(entryId) {
  try {
    await api(`/api/entries/${entryId}/toggle-not-applying`, { method: "POST" });
    loadEntries();
  } catch (e) {
    alert(e.message);
  }
}

async function onRescoreClick(entryId) {
  try {
    await api(`/api/entries/${entryId}/rescore`, { method: "POST" });
    loadEntries();
    startPolling();
  } catch (e) {
    alert(e.message);
  }
}

$("#rescore-failed-btn").addEventListener("click", async () => {
  $("#rescore-failed-btn").disabled = true;
  try {
    const { retried } = await api("/api/entries/rescore-failed", { method: "POST" });
    loadEntries();
    startPolling();
    if (!retried) alert("No failed entries to retry.");
  } catch (e) {
    alert(e.message);
  } finally {
    $("#rescore-failed-btn").disabled = false;
  }
});

async function onGenerateClick(entryId) {
  const clCheckbox = $(`#cl-${entryId}`);
  const generate_cover_letter = clCheckbox ? clCheckbox.checked : false;
  try {
    await api(`/api/entries/${entryId}/generate`, {
      method: "POST",
      body: JSON.stringify({ generate_cover_letter }),
    });
    loadEntries();
    startPolling();
  } catch (e) {
    alert(e.message);
  }
}

async function onStopGenerateClick(entryId) {
  // Honest UI: this frees up the entry immediately (status flips away from
  // "running" so you can retry or move on) but does NOT kill the in-flight
  // pipeline call server-side — see server.py's cancel_generate() docstring.
  try {
    await api(`/api/entries/${entryId}/cancel-generate`, { method: "POST" });
    loadEntries();
  } catch (e) {
    alert(e.message);
  }
}

// ── Background polling ───────────────────────────────────────────────────
// Runs every 3s whenever anything is still in flight (scoring or
// generating), across ANY entry — this is what makes "add several JDs
// without waiting" actually work: you can leave the Applications tab open
// (or come back to it later) and every entry fills itself in on its own.
let pollTimer = null;

function anyInFlight() {
  return entriesCache.some(
    (e) => e.score_status === "scoring" || e.apply_status === "running"
  );
}

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    if (!getToken()) return;
    await loadEntries();
    if (!anyInFlight()) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }, 3000);
}

// ── Scraper ──────────────────────────────────────────────────────────────
// job_id is persisted to localStorage so a page refresh (or coming back to
// this tab later) resumes showing real progress instead of silently losing
// track of an in-progress cycle — a scrape can run 20-30+ minutes, and
// before this the "running" text was pure in-memory JS state with no way
// to tell an actually-stuck cycle apart from one that's still working.
const SCRAPE_JOB_KEY = "resume_agent_scrape_job_id";

$("#scrape-btn").addEventListener("click", async () => {
  $("#scrape-btn").disabled = true;
  $("#scrape-status").innerHTML = `<div class="spinner-note">Saving current settings...</div>`;
  try {
    // Persist whatever's currently in the Search settings form BEFORE
    // triggering the cycle, so "Run" always uses what's on screen right now
    // — not whatever was last explicitly Saved (see collectScraperSettingsFromForm).
    if (scraperSettingsCache) await saveScraperSettings();
    $("#scrape-status").innerHTML = `<div class="spinner-note">Starting scrape cycle...</div>`;
    const { job_id } = await api("/api/scrape", { method: "POST" });
    localStorage.setItem(SCRAPE_JOB_KEY, job_id);
    pollScrape(job_id);
  } catch (e) {
    $("#scrape-status").innerHTML = `<div class="error-msg">${e.message}</div>`;
    $("#scrape-btn").disabled = false;
  }
});

function scrapeProgressHtml(job) {
  const detail = job.phase_detail || "Working...";
  if (job.jobs_total) {
    const pct = Math.round((job.jobs_scored / job.jobs_total) * 100);
    return `
      <div class="spinner-note">${detail}</div>
      <div style="background:var(--hairline);border-radius:6px;overflow:hidden;height:8px;margin-top:8px;">
        <div style="background:var(--blue);height:100%;width:${pct}%;transition:width 0.3s;"></div>
      </div>
      <div class="mono-label" style="margin-top:4px;">${job.jobs_scored}/${job.jobs_total} scored (${pct}%)${job.high_match_count ? ` — ${job.high_match_count} high-match` : ""}</div>`;
  }
  return `<div class="spinner-note">${detail}</div>`;
}

async function pollScrape(jobId) {
  let job;
  try {
    job = await api(`/api/scrape/${jobId}`);
  } catch (e) {
    // Job no longer exists server-side — most likely the server restarted
    // mid-cycle. Surface that honestly instead of hanging on "running"
    // forever, and stop tracking it.
    localStorage.removeItem(SCRAPE_JOB_KEY);
    $("#scrape-btn").disabled = false;
    $("#scrape-status").innerHTML = `<div class="error-msg">Lost track of this scrape job (the server may have restarted) — nothing is running anymore. Click "Run scrape cycle now" to start a fresh one.</div>`;
    return;
  }

  if (job.status === "running") {
    $("#scrape-btn").disabled = true;
    $("#scrape-status").innerHTML = scrapeProgressHtml(job);
    setTimeout(() => pollScrape(jobId), 5000);
    return;
  }

  localStorage.removeItem(SCRAPE_JOB_KEY);
  $("#scrape-btn").disabled = false;
  $("#scrape-status").innerHTML =
    job.status === "completed"
      ? `<div class="spinner-note">${job.phase_detail || "Cycle complete"} — check the Applications tab for results.</div>`
      : `<div class="error-msg">${job.error || job.phase_detail || "Scrape failed."}</div>`;
}

// ── Quick search — a one-off location/recency/title-scoped run that never
// touches saved Search settings (unlike "Run", which persists the form
// before firing). Mirrors the regular scrape's trigger/poll pattern above
// but against its own endpoint, status div, and localStorage key, so the two
// can run independently without interfering with each other.
const QUICK_SEARCH_JOB_KEY = "resume_agent_quick_search_job_id";

$("#quick-search-btn").addEventListener("click", async () => {
  $("#quick-search-btn").disabled = true;
  $("#quick-search-status").innerHTML = `<div class="spinner-note">Starting quick search...</div>`;
  try {
    const topN = parseInt($("#quick-search-top-n").value, 10) || 10;
    const hoursOld = parseInt($("#quick-search-hours-old").value, 10);
    const body = {
      location: $("#quick-search-location").value.trim() || null,
      hours_old: Number.isFinite(hoursOld) ? hoursOld : null,
      keywords: $("#quick-search-keywords").value.trim() || null,
      top_n: topN,
    };
    const { job_id } = await api("/api/quick-search", { method: "POST", body: JSON.stringify(body) });
    localStorage.setItem(QUICK_SEARCH_JOB_KEY, job_id);
    pollQuickSearch(job_id);
  } catch (e) {
    $("#quick-search-status").innerHTML = `<div class="error-msg">${e.message}</div>`;
    $("#quick-search-btn").disabled = false;
  }
});

async function pollQuickSearch(jobId) {
  let job;
  try {
    job = await api(`/api/scrape/${jobId}`);  // same job-tracking store as the regular scrape
  } catch (e) {
    localStorage.removeItem(QUICK_SEARCH_JOB_KEY);
    $("#quick-search-btn").disabled = false;
    $("#quick-search-status").innerHTML = `<div class="error-msg">Lost track of this search (the server may have restarted) — nothing is running anymore. Click "Search now" to start a fresh one.</div>`;
    return;
  }

  if (job.status === "running") {
    $("#quick-search-btn").disabled = true;
    $("#quick-search-status").innerHTML = scrapeProgressHtml(job);
    setTimeout(() => pollQuickSearch(jobId), 5000);
    return;
  }

  localStorage.removeItem(QUICK_SEARCH_JOB_KEY);
  $("#quick-search-btn").disabled = false;
  $("#quick-search-status").innerHTML =
    job.status === "completed"
      ? `<div class="spinner-note">${job.phase_detail || "Search complete"} — top matches are in the Applications tab.</div>`
      : `<div class="error-msg">${job.error || job.phase_detail || "Quick search failed."}</div>`;
}

// ── Scraper search settings ─────────────────────────────────────────────
// Keywords are select/deselect (checkbox), not a plain text field — see
// scraper/scraper_settings.py's docstring for why. Each platform then picks
// its OWN subset of the enabled master pool at its OWN count — SerpApi's
// hard monthly quota wants few keywords, Dice is free and can go deep.
// Everything here is read fresh by the scraper on every cycle (no restart
// needed to apply).
let scraperSettingsCache = null;
const PLATFORM_LABELS = { linkedin: "LinkedIn", indeed: "Indeed", dice: "Dice", google_jobs: "Google Jobs (SerpApi)" };

async function loadScraperSettings() {
  try {
    scraperSettingsCache = await api("/api/scraper-settings");
    renderKeywordCheckboxes();
    renderPlatformConfig();
    $("#scraper-location").value = scraperSettingsCache.location || "";
    $("#scraper-remote-only").checked = !!scraperSettingsCache.remote_only;
    $("#scraper-entry-level-only").checked = !!scraperSettingsCache.entry_level_only;
    $("#scraper-exclude-agencies").checked = !!scraperSettingsCache.exclude_recruiting_agencies;
    $("#scraper-hours-old").value = scraperSettingsCache.hours_old || 24;
    $("#scraper-daily-cap").value = scraperSettingsCache.daily_cap || 25;
    $("#scraper-min-score").value = scraperSettingsCache.min_score ?? (metaCache && metaCache.min_score) ?? 75;
  } catch (e) {
    $("#scraper-settings-status").innerHTML = `<div class="error-msg">${e.message}</div>`;
  }
}

function renderKeywordCheckboxes() {
  const keywords = scraperSettingsCache.keywords || [];
  $("#keyword-checkbox-list").innerHTML = keywords.length
    ? keywords.map((k, i) => `
        <label class="checkbox-row" style="margin-top:0;">
          <input type="checkbox" data-keyword-index="${i}" ${k.enabled ? "checked" : ""} />
          <span style="flex:1;">${escapeHtml(k.value)}</span>
          <button data-remove-keyword-index="${i}" class="secondary danger" style="margin-top:0;padding:2px 10px;font-size:0.7rem;">Remove</button>
        </label>`).join("")
    : `<div class="spinner-note">No keywords yet — add one below.</div>`;
  document.querySelectorAll("[data-keyword-index]").forEach((cb) => {
    cb.addEventListener("change", () => {
      scraperSettingsCache.keywords[parseInt(cb.dataset.keywordIndex, 10)].enabled = cb.checked;
      renderPlatformConfig(); // enabled-set changed — refresh which keywords each platform can offer
    });
  });
  document.querySelectorAll("[data-remove-keyword-index]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const removed = scraperSettingsCache.keywords[parseInt(btn.dataset.removeKeywordIndex, 10)].value;
      scraperSettingsCache.keywords.splice(parseInt(btn.dataset.removeKeywordIndex, 10), 1);
      // Also drop it from every platform's selected subset so it can't
      // linger there disabled-but-still-listed.
      Object.values(scraperSettingsCache.platforms || {}).forEach((cfg) => {
        cfg.keywords = (cfg.keywords || []).filter((v) => v !== removed);
      });
      renderKeywordCheckboxes();
      renderPlatformConfig();
    });
  });
}

$("#keyword-add-btn").addEventListener("click", () => {
  const val = $("#keyword-add-input").value.trim();
  if (!val || !scraperSettingsCache) return;
  scraperSettingsCache.keywords.push({ value: val, enabled: true });
  $("#keyword-add-input").value = "";
  renderKeywordCheckboxes();
  renderPlatformConfig();
});

// LinkedIn is the one platform with a count PER KEYWORD instead of one
// shared count for all of them ({"keywords": [{"value","count"}, ...]}
// instead of {"keywords": [...strings], "count": N}) — the daily_cap's
// ranking is global/score-based, not keyword-aware, so a broad title that
// happens to score high across many generic postings can otherwise crowd
// out a narrower title's candidates entirely; per-keyword counts are the
// lever to deliberately weight one title's representation over another.
const PER_KEYWORD_COUNT_PLATFORMS = new Set(["linkedin"]);

function renderPlatformConfig() {
  const enabledValues = (scraperSettingsCache.keywords || []).filter((k) => k.enabled).map((k) => k.value);
  const platforms = scraperSettingsCache.platforms || {};
  $("#platform-config-list").innerHTML = Object.keys(PLATFORM_LABELS).map((platform) => {
    const cfg = platforms[platform] || { keywords: [], count: 20 };
    const perKeywordCounts = PER_KEYWORD_COUNT_PLATFORMS.has(platform);
    const keywordEntries = cfg.keywords || [];
    // Normalize to {value, enabled, count} regardless of which shape this
    // platform's keywords are stored in — a plain string (shared-count
    // platforms) or a {value,count} object (LinkedIn).
    const selected = new Map(keywordEntries.map((k) =>
      typeof k === "object" ? [k.value, k.count ?? 15] : [k, cfg.count || 20]
    ));
    const keywordRows = enabledValues.length
      ? enabledValues.map((val) => {
          const isSelected = selected.has(val);
          const countInput = perKeywordCounts
            ? `<input type="number" data-platform-keyword-count="${platform}" data-keyword-for-count="${escapeHtml(val)}"
                 value="${isSelected ? selected.get(val) : 15}" min="1" max="100"
                 style="width:60px;padding:3px 6px;margin-left:8px;" ${isSelected ? "" : "disabled"} />`
            : "";
          return `
          <label class="checkbox-row" style="margin-top:0;justify-content:space-between;">
            <span style="display:flex;align-items:center;gap:8px;">
              <input type="checkbox" data-platform="${platform}" data-platform-keyword="${escapeHtml(val)}" ${isSelected ? "checked" : ""} />
              <span>${escapeHtml(val)}</span>
            </span>
            ${countInput}
          </label>`;
        }).join("")
      : `<span class="spinner-note">No keywords enabled in the pool above.</span>`;
    // Non-per-keyword platforms keep their single shared count control;
    // per-keyword platforms drop it entirely — the count now lives inline
    // with each keyword row above instead.
    const sharedCountControl = perKeywordCounts ? "" : `
          <label style="margin:0;display:flex;align-items:center;gap:6px;">
            <span class="mono-label">count/keyword</span>
            <input type="number" data-platform-count="${platform}" value="${cfg.count || 20}" min="1" max="100" style="width:70px;padding:4px 8px;" />
          </label>`;
    return `
      <div style="border:1px solid var(--hairline);border-radius:6px;padding:12px;">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;">
          <strong class="mono-label">${PLATFORM_LABELS[platform]}</strong>
          ${sharedCountControl}
        </div>
        <div style="display:flex;flex-direction:column;gap:4px;margin-top:8px;">${keywordRows}</div>
      </div>`;
  }).join("");

  // For per-keyword platforms, cfg.keywords holds {value,count} objects —
  // find-or-create the entry for this keyword. Must also match an EXISTING
  // bare-string entry (the old shared-count shape, e.g. from a settings
  // file saved before this feature existed) and upgrade it in place —
  // matching objects only left a stale string entry AND a new object entry
  // both in the array for the same keyword, causing that keyword to be
  // scraped twice per cycle with two different counts. This also strips any
  // OTHER duplicate entries for the same value it finds along the way, so a
  // legacy file gets fully cleaned up the next time it's touched, not just
  // patched around.
  function _linkedinKeywordEntry(cfg, kw) {
    cfg.keywords = cfg.keywords || [];
    const matches = cfg.keywords.filter((k) => (typeof k === "object" ? k.value : k) === kw);
    let entry = matches.find((k) => typeof k === "object");
    if (!entry) {
      const legacyCount = (typeof cfg.count === "number") ? cfg.count : 15;
      entry = { value: kw, count: legacyCount };
    }
    cfg.keywords = cfg.keywords.filter((k) => (typeof k === "object" ? k.value : k) !== kw);
    cfg.keywords.push(entry);
    return entry;
  }

  document.querySelectorAll("[data-platform-keyword]").forEach((cb) => {
    cb.addEventListener("change", () => {
      const platform = cb.dataset.platform;
      const kw = cb.dataset.platformKeyword;
      const cfg = scraperSettingsCache.platforms[platform] || (scraperSettingsCache.platforms[platform] = { keywords: [], count: 20 });
      if (PER_KEYWORD_COUNT_PLATFORMS.has(platform)) {
        cfg.keywords = cfg.keywords || [];
        if (cb.checked) {
          _linkedinKeywordEntry(cfg, kw);
        } else {
          cfg.keywords = cfg.keywords.filter((k) => (typeof k === "object" ? k.value : k) !== kw);
        }
      } else {
        cfg.keywords = cfg.keywords || [];
        if (cb.checked && !cfg.keywords.includes(kw)) cfg.keywords.push(kw);
        if (!cb.checked) cfg.keywords = cfg.keywords.filter((v) => v !== kw);
      }
      renderPlatformConfig(); // re-render so the count input enables/disables to match
    });
  });
  document.querySelectorAll("[data-platform-count]").forEach((input) => {
    input.addEventListener("change", () => {
      const platform = input.dataset.platformCount;
      const cfg = scraperSettingsCache.platforms[platform] || (scraperSettingsCache.platforms[platform] = { keywords: [], count: 20 });
      cfg.count = parseInt(input.value, 10) || 20;
    });
  });
  document.querySelectorAll("[data-platform-keyword-count]").forEach((input) => {
    input.addEventListener("change", () => {
      const platform = input.dataset.platformKeywordCount;
      const kw = input.dataset.keywordForCount;
      const cfg = scraperSettingsCache.platforms[platform] || (scraperSettingsCache.platforms[platform] = { keywords: [], count: 20 });
      const entry = _linkedinKeywordEntry(cfg, kw);
      entry.count = parseInt(input.value, 10) || 15;
    });
  });
}

// Reads whatever is currently displayed in the Search settings form — used
// by both the explicit Save button AND "Run scrape cycle now" (see below).
// Without this shared read, "Run" used to fire /api/scrape directly against
// whatever was last explicitly SAVED to disk, silently ignoring any edits
// sitting in the form if the user forgot to click Save first — a real
// live bug: changing keywords/counts/checkboxes and clicking Run reused the
// old persisted settings with no warning.
function collectScraperSettingsFromForm() {
  return {
    keywords: scraperSettingsCache.keywords,
    platforms: scraperSettingsCache.platforms,
    location: $("#scraper-location").value.trim() || "United States",
    remote_only: $("#scraper-remote-only").checked,
    hours_old: parseInt($("#scraper-hours-old").value, 10) || 24,
    entry_level_only: $("#scraper-entry-level-only").checked,
    exclude_recruiting_agencies: $("#scraper-exclude-agencies").checked,
    daily_cap: parseInt($("#scraper-daily-cap").value, 10) || 25,
    // `|| 75` would turn a typed 0 into 75; only fall back when the field is
    // actually empty/non-numeric. Range is enforced server-side (0-100).
    min_score: (() => {
      const v = parseInt($("#scraper-min-score").value, 10);
      return Number.isFinite(v) ? v : (scraperSettingsCache.min_score ?? 75);
    })(),
  };
}

async function saveScraperSettings() {
  const settings = collectScraperSettingsFromForm();
  scraperSettingsCache = await api("/api/scraper-settings", { method: "POST", body: JSON.stringify(settings) });
  loadMeta();  // min_score lives in these settings — keep /api/meta's copy (and any UI derived from it) current
  renderKeywordCheckboxes();
  renderPlatformConfig();
}

$("#scraper-settings-save-btn").addEventListener("click", async () => {
  if (!scraperSettingsCache) return;
  $("#scraper-settings-save-btn").disabled = true;
  try {
    await saveScraperSettings();
    $("#scraper-settings-status").innerHTML = `<div class="spinner-note">Saved — takes effect on the next scrape cycle, no restart needed.</div>`;
  } catch (e) {
    $("#scraper-settings-status").innerHTML = `<div class="error-msg">${e.message}</div>`;
  } finally {
    $("#scraper-settings-save-btn").disabled = false;
  }
});

// ── ATS target companies ────────────────────────────────────────────────
async function loadAtsCompanies() {
  try {
    const companies = await api("/api/ats-companies");
    $("#ats-company-list").innerHTML = companies.length
      ? companies.map((c) => `
          <div class="run-row" style="padding:6px 0;">
            <span class="mono-label">${escapeHtml(c.company)} — ${escapeHtml(c.ats)} — ${escapeHtml(c.identifier)}</span>
            <button data-remove-ats-company="${escapeHtml(c.company)}" class="secondary" style="margin-top:0;padding:4px 10px;">Remove</button>
          </div>`).join("")
      : `<div class="spinner-note">No companies configured yet — this source stays inactive until you add one.</div>`;
    document.querySelectorAll("[data-remove-ats-company]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          await api(`/api/ats-companies/${encodeURIComponent(btn.dataset.removeAtsCompany)}`, { method: "DELETE" });
          loadAtsCompanies();
        } catch (e) {
          alert(e.message);
        }
      });
    });
  } catch (e) {
    $("#ats-company-list").innerHTML = `<div class="error-msg">${e.message}</div>`;
  }
}

$("#ats-company-add-btn").addEventListener("click", async () => {
  const company = $("#ats-company-name").value.trim();
  const ats = $("#ats-company-type").value;
  const identifier = $("#ats-company-identifier").value.trim();
  if (!company || !identifier) {
    $("#ats-company-status").innerHTML = `<div class="error-msg">Company name and identifier are required.</div>`;
    return;
  }
  try {
    await api("/api/ats-companies", { method: "POST", body: JSON.stringify({ company, ats, identifier }) });
    $("#ats-company-name").value = "";
    $("#ats-company-identifier").value = "";
    $("#ats-company-status").innerHTML = "";
    loadAtsCompanies();
  } catch (e) {
    $("#ats-company-status").innerHTML = `<div class="error-msg">${e.message}</div>`;
  }
});

// ── LinkedIn target companies ───────────────────────────────────────────
async function loadLinkedInCompanies() {
  try {
    const companies = await api("/api/linkedin-companies");
    $("#li-company-list").innerHTML = companies.length
      ? companies.map((c) => `
          <div class="run-row" style="padding:6px 0;">
            <span class="mono-label">${escapeHtml(c.company)} — id ${escapeHtml(String(c.linkedin_id))}</span>
            <button data-remove-li-company="${escapeHtml(c.company)}" class="secondary" style="margin-top:0;padding:4px 10px;">Remove</button>
          </div>`).join("")
      : `<div class="spinner-note">No companies configured yet — this pass stays inactive until you add one.</div>`;
    document.querySelectorAll("[data-remove-li-company]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          await api(`/api/linkedin-companies/${encodeURIComponent(btn.dataset.removeLiCompany)}`, { method: "DELETE" });
          loadLinkedInCompanies();
        } catch (e) {
          alert(e.message);
        }
      });
    });
  } catch (e) {
    $("#li-company-list").innerHTML = `<div class="error-msg">${e.message}</div>`;
  }
}

$("#li-company-add-btn").addEventListener("click", async () => {
  const company = $("#li-company-name").value.trim();
  const linkedin_id = $("#li-company-id").value.trim();
  if (!company || !linkedin_id) {
    $("#li-company-status").innerHTML = `<div class="error-msg">Company name and LinkedIn id are required.</div>`;
    return;
  }
  try {
    await api("/api/linkedin-companies", { method: "POST", body: JSON.stringify({ company, linkedin_id: Number(linkedin_id) }) });
    $("#li-company-name").value = "";
    $("#li-company-id").value = "";
    $("#li-company-status").innerHTML = "";
    loadLinkedInCompanies();
  } catch (e) {
    $("#li-company-status").innerHTML = `<div class="error-msg">${e.message}</div>`;
  }
});

// ── Boot ─────────────────────────────────────────────────────────────────
// Server constants (current rubric version, min score) — fetched once so the
// UI can tell a current score from one computed under an older rubric.
let metaCache = null;
async function loadMeta() {
  try { metaCache = await api("/api/meta"); renderEntries(); } catch (_) { /* non-fatal */ }
}

if (getToken()) {
  api("/api/entries").then(() => {
    showApp();
    loadMeta();
    loadEntries();
    startPolling();
    const inFlightJobId = localStorage.getItem(SCRAPE_JOB_KEY);
    if (inFlightJobId) pollScrape(inFlightJobId);
    const inFlightQuickSearchId = localStorage.getItem(QUICK_SEARCH_JOB_KEY);
    if (inFlightQuickSearchId) pollQuickSearch(inFlightQuickSearchId);
  }).catch(() => showLogin());
} else {
  showLogin();
}
