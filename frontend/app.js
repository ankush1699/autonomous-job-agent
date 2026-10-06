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
    if (tab.dataset.tab === "scraper") { loadScraperTab(); }
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

// ── Scraper tab (redesigned Oct 2026) ─────────────────────────────────────
// Five plain questions instead of a per-platform matrix:
//   1 what jobs (roles, star = search deeper)  2 where & how recent
//   3 where to look (sources + one depth preset) 4 companies to watch
//   5 what to keep (best N scoring >= X)
// The banner at the top is the server's own description of the next run
// (GET /api/scraper/plan), so the UI never re-derives the rules. The original
// per-platform table still exists under Advanced (custom mode).
//
// job_id is persisted to localStorage so a page refresh resumes showing real
// progress for an in-flight cycle (a scrape can run 20-30+ minutes).
const SCRAPE_JOB_KEY = "resume_agent_scrape_job_id";
const QUICK_SEARCH_JOB_KEY = "resume_agent_quick_search_job_id";

let scraperSettingsCache = null;
let settingsDirty = false;

const PLATFORM_LABELS = { linkedin: "LinkedIn", indeed: "Indeed", dice: "Dice", google_jobs: "Google Jobs (SerpApi)" };
const SOURCE_INFO = [
  { key: "linkedin", label: "LinkedIn", note: "The biggest source. Searched politely; heavy use risks a temporary block." },
  { key: "indeed", label: "Indeed", note: "Kept shallow on purpose: Indeed has flagged accounts for automated traffic." },
  { key: "dice", label: "Dice", note: "Free and unlimited, good for tech roles." },
  { key: "google_jobs", label: "Google Jobs", note: "Uses your SerpApi quota: 1 of 100 free monthly searches per role, per run." },
  { key: "company_boards", label: "Company career pages", note: "Checks the companies you watch on their own job boards." },
  { key: "linkedin_company_pages", label: "LinkedIn company pages", note: "An extra LinkedIn search limited to the companies you watch there." },
];
const POSTED_OPTIONS = [[24, "Last 24 hours"], [48, "Last 2 days"], [72, "Last 3 days"], [168, "Last week"], [336, "Last 2 weeks"]];
const DEPTH_TEXT = {
  light: "Light: a quick look. About 10 results per role on LinkedIn (20 for starred roles), fewer elsewhere. Cheapest and safest.",
  normal: "Normal: the everyday setting. About 20 per role on LinkedIn (40 for starred roles), Dice 20, Indeed and Google Jobs limited to your first few roles.",
  deep: "Deep: casts the widest net. About 35 per role on LinkedIn (60 for starred roles), Dice 40. Scores more jobs, so it costs more.",
};
const SOURCE_NAMES = {
  greenhouse: "career page (Greenhouse)", lever: "career page (Lever)", ashby: "career page (Ashby)",
  workday: "career page (Workday)", linkedin: "LinkedIn page",
  linkedin_company_pages: "LinkedIn company pages", company_boards: "Company career pages",
  indeed: "Indeed", dice: "Dice", google_jobs: "Google Jobs",
};
const REJECT_LABELS = {
  already_seen: "already seen before", cross_platform_duplicate: "duplicate on another site",
  too_senior_title: "too senior (title)", no_sponsorship: "no sponsorship / clearance",
  description_too_short: "description too short", recruiting_agency: "recruiting agency",
};

function timeAgo(iso) {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

function sum(obj) { return Object.values(obj || {}).reduce((a, b) => a + (b || 0), 0); }

async function loadScraperTab() {
  await loadScraperSettings();
  loadPlan();
  loadCompanies();
  loadRunHistory();
}

// ── Settings: load, render, collect, save ────────────────────────────────
async function loadScraperSettings() {
  try {
    scraperSettingsCache = await api("/api/scraper-settings");
    settingsDirty = false;
    renderScraperForm();
  } catch (e) {
    $("#scraper-settings-status").innerHTML = `<div class="error-msg">${escapeHtml(e.message)}</div>`;
  }
}

function markDirty() {
  settingsDirty = true;
  $("#settings-dirty").classList.remove("hidden");
}

function fillPostedSelect(sel, hours, { includeSaved = false } = {}) {
  const opts = POSTED_OPTIONS.slice();
  if (hours && !opts.some(([h]) => h === hours)) opts.push([hours, `Last ${hours} hours`]);
  sel.innerHTML = opts.map(([h, label]) =>
    `<option value="${h}" ${h === hours ? "selected" : ""}>${label}</option>`).join("");
}

function renderScraperForm() {
  const s = scraperSettingsCache;
  renderRoleChips();
  renderSources();
  renderDepth();
  renderPlatformConfig();
  $("#scraper-location").value = s.location || "";
  fillPostedSelect($("#scraper-posted"), s.hours_old || 24);
  $("#scraper-remote-only").checked = !!s.remote_only;
  $("#scraper-entry-level-only").checked = !!s.entry_level_only;
  $("#scraper-exclude-agencies").checked = !!s.exclude_recruiting_agencies;
  $("#scraper-daily-cap").value = s.daily_cap || 25;
  $("#scraper-min-score").value = s.min_score ?? (metaCache && metaCache.min_score) ?? 80;
  $("#settings-dirty").classList.toggle("hidden", !settingsDirty);
}

function renderRoleChips() {
  const keywords = scraperSettingsCache.keywords || [];
  $("#role-chips").innerHTML = keywords.length
    ? keywords.map((k, i) => `
        <span class="chip ${k.enabled ? "on" : ""}">
          <button type="button" class="chip-label" data-role-toggle="${i}" title="${k.enabled ? "On: click to turn off" : "Off: click to turn on"}">${escapeHtml(k.value)}</button>
          <button type="button" class="chip-star ${k.focus ? "on" : ""}" data-role-star="${i}" title="${k.focus ? "Starred: searched deeper. Click to unstar" : "Star to search this role deeper"}" aria-label="Star ${escapeHtml(k.value)}">&#9733;</button>
          <button type="button" class="chip-x" data-role-remove="${i}" title="Remove ${escapeHtml(k.value)}" aria-label="Remove ${escapeHtml(k.value)}">&times;</button>
        </span>`).join("")
    : `<div class="spinner-note">No roles yet. Add one below.</div>`;
  document.querySelectorAll("[data-role-toggle]").forEach((b) => b.addEventListener("click", () => {
    const k = scraperSettingsCache.keywords[+b.dataset.roleToggle];
    k.enabled = !k.enabled;
    if (!k.enabled) k.focus = false;
    markDirty(); renderRoleChips(); renderPlatformConfig();
  }));
  document.querySelectorAll("[data-role-star]").forEach((b) => b.addEventListener("click", () => {
    const k = scraperSettingsCache.keywords[+b.dataset.roleStar];
    k.focus = !k.focus;
    if (k.focus) k.enabled = true;
    markDirty(); renderRoleChips(); renderPlatformConfig();
  }));
  document.querySelectorAll("[data-role-remove]").forEach((b) => b.addEventListener("click", () => {
    const removed = scraperSettingsCache.keywords[+b.dataset.roleRemove].value;
    scraperSettingsCache.keywords.splice(+b.dataset.roleRemove, 1);
    Object.values(scraperSettingsCache.platforms || {}).forEach((cfg) => {
      cfg.keywords = (cfg.keywords || []).filter((v) => (typeof v === "object" ? v.value : v) !== removed);
    });
    markDirty(); renderRoleChips(); renderPlatformConfig();
  }));
}

function addRoleFromInput() {
  const val = $("#keyword-add-input").value.trim();
  if (!val || !scraperSettingsCache) return;
  if (scraperSettingsCache.keywords.some((k) => k.value.toLowerCase() === val.toLowerCase())) {
    $("#keyword-add-input").value = "";
    return;
  }
  scraperSettingsCache.keywords.push({ value: val, enabled: true, focus: false });
  $("#keyword-add-input").value = "";
  markDirty(); renderRoleChips(); renderPlatformConfig();
}
$("#keyword-add-btn").addEventListener("click", addRoleFromInput);
$("#keyword-add-input").addEventListener("keydown", (e) => { if (e.key === "Enter") addRoleFromInput(); });

function renderSources() {
  const sources = scraperSettingsCache.sources || {};
  $("#source-list").innerHTML = SOURCE_INFO.map((src) => `
    <label class="source-row">
      <input type="checkbox" data-source="${src.key}" ${sources[src.key] !== false ? "checked" : ""} />
      <span><strong>${src.label}</strong><span class="source-note">${src.note}</span></span>
    </label>`).join("");
  document.querySelectorAll("[data-source]").forEach((cb) => cb.addEventListener("change", () => {
    scraperSettingsCache.sources = { ...(scraperSettingsCache.sources || {}), [cb.dataset.source]: cb.checked };
    markDirty();
  }));
}

function renderDepth() {
  const s = scraperSettingsCache;
  const simple = s.mode === "simple";
  document.querySelectorAll("#depth-seg [data-depth]").forEach((b) => {
    b.classList.toggle("active", simple && s.depth === b.dataset.depth);
  });
  $("#depth-explain").textContent = simple ? DEPTH_TEXT[s.depth] || "" : "";
  $("#custom-mode-note").classList.toggle("hidden", simple);
}
document.querySelectorAll("#depth-seg [data-depth]").forEach((b) => b.addEventListener("click", () => {
  if (!scraperSettingsCache) return;
  scraperSettingsCache.mode = "simple";
  scraperSettingsCache.depth = b.dataset.depth;
  markDirty(); renderDepth();
}));

["#scraper-location", "#scraper-posted", "#scraper-remote-only", "#scraper-entry-level-only",
 "#scraper-exclude-agencies", "#scraper-daily-cap", "#scraper-min-score"].forEach((sel) => {
  $(sel).addEventListener("change", markDirty);
  $(sel).addEventListener("input", markDirty);
});

// Reads what is on screen right now. Used by Save AND by "Run now" — before
// this existed, Run used whatever was last saved to disk and silently ignored
// edits still sitting in the form.
function collectScraperSettingsFromForm() {
  const s = scraperSettingsCache;
  const minScore = parseInt($("#scraper-min-score").value, 10);
  return {
    keywords: s.keywords,
    platforms: s.platforms,
    mode: s.mode,
    depth: s.depth,
    sources: s.sources,
    location: $("#scraper-location").value.trim() || "United States",
    remote_only: $("#scraper-remote-only").checked,
    hours_old: parseInt($("#scraper-posted").value, 10) || 24,
    entry_level_only: $("#scraper-entry-level-only").checked,
    exclude_recruiting_agencies: $("#scraper-exclude-agencies").checked,
    daily_cap: parseInt($("#scraper-daily-cap").value, 10) || 25,
    min_score: Number.isFinite(minScore) ? minScore : (s.min_score ?? 80),
  };
}

async function saveScraperSettings() {
  scraperSettingsCache = await api("/api/scraper-settings", { method: "POST", body: JSON.stringify(collectScraperSettingsFromForm()) });
  settingsDirty = false;
  loadMeta();  // min_score lives here; keep /api/meta's copy current
  renderScraperForm();
  loadPlan();
}

$("#scraper-settings-save-btn").addEventListener("click", async () => {
  if (!scraperSettingsCache) return;
  $("#scraper-settings-save-btn").disabled = true;
  try {
    await saveScraperSettings();
    $("#scraper-settings-status").innerHTML = `<div class="spinner-note">Saved. Takes effect on the next run, no restart needed.</div>`;
  } catch (e) {
    $("#scraper-settings-status").innerHTML = `<div class="error-msg">${escapeHtml(e.message)}</div>`;
  } finally {
    $("#scraper-settings-save-btn").disabled = false;
  }
});

// ── Plan banner ──────────────────────────────────────────────────────────
async function loadPlan() {
  try {
    const plan = await api("/api/scraper/plan");
    $("#plan-sentence").textContent = plan.sentence;
    const bits = [];
    bits.push(plan.mode === "simple" ? `Depth: ${plan.depth}` : "Custom per-platform setup");
    if (plan.focus_roles && plan.focus_roles.length) bits.push(`Starred: ${plan.focus_roles.map(escapeHtml).join(", ")}`);
    bits.push(`Up to ${plan.max_postings_from_boards} postings from job boards`);
    if (plan.google_jobs_searches_per_run) bits.push(`Google Jobs: ${plan.google_jobs_searches_per_run} of 100 monthly searches per run`);
    const lr = plan.last_run;
    if (lr) {
      const cost = lr.est_cost_usd ? `, about $${lr.est_cost_usd.toFixed(2)}` : "";
      bits.push(lr.status === "error"
        ? `Last run ${timeAgo(lr.finished_at)}: failed (${escapeHtml(lr.error || "error")})`
        : `Last run ${timeAgo(lr.finished_at)}: found ${sum(lr.found)}, scored ${lr.scored.total} (${lr.scored.model_calls} new${cost}), kept ${lr.kept}`);
    }
    $("#plan-meta").innerHTML = bits.map((b) => `<span>${b}</span>`).join("");
  } catch (e) {
    $("#plan-sentence").textContent = "Could not load the plan.";
    $("#plan-meta").innerHTML = `<span class="error-msg">${escapeHtml(e.message)}</span>`;
  }
}

// ── Run now / progress ───────────────────────────────────────────────────
$("#scrape-btn").addEventListener("click", async () => {
  $("#scrape-btn").disabled = true;
  try {
    if (scraperSettingsCache && settingsDirty) {
      $("#scrape-status").innerHTML = `<div class="spinner-note">Saving your changes first...</div>`;
      await saveScraperSettings();
    }
    $("#scrape-status").innerHTML = `<div class="spinner-note">Starting...</div>`;
    const { job_id } = await api("/api/scrape", { method: "POST" });
    localStorage.setItem(SCRAPE_JOB_KEY, job_id);
    pollScrape(job_id);
  } catch (e) {
    $("#scrape-status").innerHTML = `<div class="error-msg">${escapeHtml(e.message)}</div>`;
    $("#scrape-btn").disabled = false;
  }
});

function scrapeProgressHtml(job) {
  const detail = escapeHtml(job.phase_detail || "Working...");
  if (job.jobs_total) {
    const pct = Math.round((job.jobs_scored / job.jobs_total) * 100);
    return `
      <div class="spinner-note">${detail}</div>
      <div class="progress"><div style="width:${pct}%;"></div></div>
      <div class="mono-label" style="margin-top:4px;">${job.jobs_scored}/${job.jobs_total} scored (${pct}%)${job.high_match_count ? ` · ${job.high_match_count} passed your bar` : ""}</div>`;
  }
  return `<div class="spinner-note">${detail}</div>`;
}

function afterRunFinished() {
  loadPlan();
  loadRunHistory();
  loadCompanies();
}

async function pollScrape(jobId) {
  let job;
  try {
    job = await api(`/api/scrape/${jobId}`);
  } catch (e) {
    localStorage.removeItem(SCRAPE_JOB_KEY);
    $("#scrape-btn").disabled = false;
    $("#scrape-status").innerHTML = `<div class="error-msg">Lost track of this run (the server may have restarted). Nothing is running now. Click "Run now" to start a fresh one.</div>`;
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
  $("#scrape-status").innerHTML = job.status === "completed"
    ? `<div class="spinner-note">${escapeHtml(job.phase_detail || "Run complete")}. Results are in the Applications tab.</div>`
    : `<div class="error-msg">${escapeHtml(job.error || job.phase_detail || "The run failed.")}</div>`;
  afterRunFinished();
}

// ── Search once elsewhere (one-off, never saved) ─────────────────────────
$("#oneoff-open-btn").addEventListener("click", () => {
  const s = scraperSettingsCache || {};
  $("#quick-search-location").value = $("#scraper-location").value || s.location || "";
  fillPostedSelect($("#quick-search-posted"), parseInt($("#scraper-posted").value, 10) || s.hours_old || 24);
  $("#quick-search-keywords").value = (s.keywords || []).filter((k) => k.enabled).map((k) => k.value).join(", ");
  $("#oneoff-card").classList.remove("hidden");
  $("#quick-search-location").focus();
});
$("#oneoff-close-btn").addEventListener("click", () => $("#oneoff-card").classList.add("hidden"));

$("#quick-search-btn").addEventListener("click", async () => {
  $("#quick-search-btn").disabled = true;
  $("#quick-search-status").innerHTML = `<div class="spinner-note">Starting...</div>`;
  try {
    const body = {
      location: $("#quick-search-location").value.trim() || null,
      hours_old: parseInt($("#quick-search-posted").value, 10) || null,
      keywords: $("#quick-search-keywords").value.split(",").map((x) => x.trim()).filter(Boolean).join(",") || null,
      top_n: parseInt($("#quick-search-top-n").value, 10) || 10,
    };
    const { job_id } = await api("/api/quick-search", { method: "POST", body: JSON.stringify(body) });
    localStorage.setItem(QUICK_SEARCH_JOB_KEY, job_id);
    pollQuickSearch(job_id);
  } catch (e) {
    $("#quick-search-status").innerHTML = `<div class="error-msg">${escapeHtml(e.message)}</div>`;
    $("#quick-search-btn").disabled = false;
  }
});

async function pollQuickSearch(jobId) {
  let job;
  try {
    job = await api(`/api/scrape/${jobId}`);
  } catch (e) {
    localStorage.removeItem(QUICK_SEARCH_JOB_KEY);
    $("#quick-search-btn").disabled = false;
    $("#quick-search-status").innerHTML = `<div class="error-msg">Lost track of this search (the server may have restarted). Click "Search now" to start a fresh one.</div>`;
    return;
  }
  $("#oneoff-card").classList.remove("hidden");
  if (job.status === "running") {
    $("#quick-search-btn").disabled = true;
    $("#quick-search-status").innerHTML = scrapeProgressHtml(job);
    setTimeout(() => pollQuickSearch(jobId), 5000);
    return;
  }
  localStorage.removeItem(QUICK_SEARCH_JOB_KEY);
  $("#quick-search-btn").disabled = false;
  $("#quick-search-status").innerHTML = job.status === "completed"
    ? `<div class="spinner-note">${escapeHtml(job.phase_detail || "Search complete")}. Top matches are in the Applications tab.</div>`
    : `<div class="error-msg">${escapeHtml(job.error || job.phase_detail || "The search failed.")}</div>`;
  afterRunFinished();
}

// ── Companies to watch (one list for career pages + LinkedIn pages) ──────
async function loadCompanies() {
  try {
    const rows = await api("/api/target-companies");
    $("#company-list").innerHTML = rows.length
      ? `<div class="mono-label company-count">${rows.length} companies watched</div>` + rows.map((c) => `
          <div class="company-row">
            <div class="company-main">
              <span class="company-name">${escapeHtml(c.company)}</span>
              <span class="badge">${escapeHtml(SOURCE_NAMES[c.source] || c.source)}</span>
            </div>
            <span class="company-found">${c.found_last_run == null ? "" : `${c.found_last_run} found last run`}</span>
            <button class="secondary danger small" data-unwatch-source="${escapeHtml(c.source)}" data-unwatch-company="${escapeHtml(c.company)}">Remove</button>
          </div>`).join("")
      : `<div class="spinner-note">No companies yet. Add the ones you most want to work for.</div>`;
    document.querySelectorAll("[data-unwatch-company]").forEach((b) => b.addEventListener("click", async () => {
      try {
        await api(`/api/target-companies/${encodeURIComponent(b.dataset.unwatchSource)}/${encodeURIComponent(b.dataset.unwatchCompany)}`, { method: "DELETE" });
        loadCompanies(); loadPlan();
      } catch (e) { alert(e.message); }
    }));
  } catch (e) {
    $("#company-list").innerHTML = `<div class="error-msg">${escapeHtml(e.message)}</div>`;
  }
}

async function detectCompany() {
  const name = $("#company-name-input").value.trim();
  const url = $("#company-url-input").value.trim();
  if (!name && !url) return;
  $("#company-detect-btn").disabled = true;
  $("#company-detect-results").innerHTML = `<div class="spinner-note">Looking for ${escapeHtml(name || url)}...</div>`;
  try {
    const { candidates } = await api("/api/target-companies/detect", { method: "POST", body: JSON.stringify({ name, url: url || null }) });
    if (!candidates.length) {
      $("#company-detect-results").innerHTML = `<div class="notice">Couldn't find a careers page or LinkedIn page for "${escapeHtml(name || url)}". Paste its careers page or LinkedIn company URL and press Find again.</div>`;
      return;
    }
    $("#company-detect-results").innerHTML = `
      <div class="detect-list">${candidates.map((c, i) => `
        <div class="detect-row">
          <span><strong>${escapeHtml(c.label)}</strong> · ${escapeHtml(SOURCE_NAMES[c.source] || c.source)}${c.open_jobs != null ? ` · ${c.open_jobs} open jobs` : ""}
          ${c.name_matches ? "" : `<span class="warn">name differs: check it's the right company</span>`}</span>
          <button class="secondary small" data-watch-index="${i}">Watch</button>
        </div>`).join("")}</div>`;
    document.querySelectorAll("[data-watch-index]").forEach((b) => b.addEventListener("click", async () => {
      const c = candidates[+b.dataset.watchIndex];
      try {
        await api("/api/target-companies", { method: "POST", body: JSON.stringify({ company: name || c.label, source: c.source, identifier: c.identifier }) });
        $("#company-detect-results").innerHTML = "";
        $("#company-name-input").value = ""; $("#company-url-input").value = "";
        loadCompanies(); loadPlan();
      } catch (e) {
        $("#company-detect-results").innerHTML = `<div class="error-msg">${escapeHtml(e.message)}</div>`;
      }
    }));
  } catch (e) {
    $("#company-detect-results").innerHTML = `<div class="error-msg">${escapeHtml(e.message)}</div>`;
  } finally {
    $("#company-detect-btn").disabled = false;
  }
}
$("#company-detect-btn").addEventListener("click", detectCompany);
$("#company-name-input").addEventListener("keydown", (e) => { if (e.key === "Enter") detectCompany(); });

// ── Last runs: where jobs drop out ───────────────────────────────────────
async function loadRunHistory() {
  try {
    const runs = await api("/api/scrape-history?limit=5");
    if (!runs.length) {
      $("#run-history").innerHTML = `<div class="spinner-note">No runs recorded yet. They appear here after your next run.</div>`;
      return;
    }
    $("#run-history").innerHTML = runs.map((r) => {
      const found = sum(r.found), passedFilters = sum(r.after_filters);
      const cost = r.est_cost_usd ? ` · about $${r.est_cost_usd.toFixed(2)}` : "";
      const head = r.status === "error"
        ? `<span class="error-msg">failed: ${escapeHtml(r.error || "")}</span>`
        : `<span class="funnel">${found} found → ${passedFilters} passed filters → ${r.scored.total} scored (${r.scored.model_calls} new${cost}) → ${r.passed_bar} passed your bar → <strong>${r.kept} kept</strong>${r.waitlisted ? ` + ${r.waitlisted} waitlisted` : ""}</span>`;
      const sources = Object.keys({ ...r.found, ...r.after_filters }).map((k) =>
        `<tr><td>${escapeHtml(SOURCE_NAMES[k] || k)}</td><td>${r.found[k] || 0}</td><td>${(r.after_filters || {})[k] || 0}</td></tr>`).join("");
      const rejected = Object.entries(r.rejected || {}).filter(([, n]) => n).map(([k, n]) =>
        `<li>${n} ${escapeHtml(REJECT_LABELS[k] || k)}</li>`).join("");
      return `
        <details class="run-item">
          <summary><span class="mono-label">${escapeHtml(r.kind || "run")} · ${timeAgo(r.finished_at || r.started_at)}</span>${head}</summary>
          <div class="run-detail">
            <table class="mini-table"><thead><tr><th>Source</th><th>Found</th><th>Passed filters</th></tr></thead><tbody>${sources}</tbody></table>
            ${rejected ? `<div><span class="mono-label">Filtered out</span><ul>${rejected}</ul></div>` : ""}
          </div>
        </details>`;
    }).join("");
  } catch (e) {
    $("#run-history").innerHTML = `<div class="error-msg">${escapeHtml(e.message)}</div>`;
  }
}

// ── Advanced: the original per-platform table (custom mode) ──────────────
// LinkedIn keeps a count PER role ({"value","count"} objects); the others keep
// one shared count. Any change here switches the settings to custom mode.
const PER_KEYWORD_COUNT_PLATFORMS = new Set(["linkedin"]);

function switchToCustom() {
  if (scraperSettingsCache.mode !== "custom") {
    scraperSettingsCache.mode = "custom";
    renderDepth();
  }
  markDirty();
}

function renderPlatformConfig() {
  const enabledValues = (scraperSettingsCache.keywords || []).filter((k) => k.enabled).map((k) => k.value);
  const platforms = scraperSettingsCache.platforms || {};
  $("#platform-config-list").innerHTML = Object.keys(PLATFORM_LABELS).map((platform) => {
    const cfg = platforms[platform] || { keywords: [], count: 20 };
    const perKeywordCounts = PER_KEYWORD_COUNT_PLATFORMS.has(platform);
    const selected = new Map((cfg.keywords || []).map((k) =>
      typeof k === "object" ? [k.value, k.count ?? 15] : [k, cfg.count || 20]));
    const rows = enabledValues.length
      ? enabledValues.map((val) => {
          const on = selected.has(val);
          const countInput = perKeywordCounts
            ? `<input type="number" data-platform-keyword-count="${platform}" data-keyword-for-count="${escapeHtml(val)}"
                 value="${on ? selected.get(val) : 15}" min="1" max="100" style="width:60px;padding:3px 6px;margin-left:8px;" ${on ? "" : "disabled"} />`
            : "";
          return `
          <label class="checkbox-row" style="margin-top:0;justify-content:space-between;">
            <span style="display:flex;align-items:center;gap:8px;">
              <input type="checkbox" data-platform="${platform}" data-platform-keyword="${escapeHtml(val)}" ${on ? "checked" : ""} />
              <span>${escapeHtml(val)}</span>
            </span>${countInput}
          </label>`;
        }).join("")
      : `<span class="spinner-note">No roles turned on.</span>`;
    const shared = perKeywordCounts ? "" : `
          <label style="margin:0;display:flex;align-items:center;gap:6px;">
            <span class="mono-label">results per role</span>
            <input type="number" data-platform-count="${platform}" value="${cfg.count || 20}" min="1" max="100" style="width:70px;padding:4px 8px;" />
          </label>`;
    return `
      <div style="border:1px solid var(--hairline);border-radius:6px;padding:12px;">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;">
          <strong class="mono-label">${PLATFORM_LABELS[platform]}</strong>${shared}
        </div>
        <div style="display:flex;flex-direction:column;gap:4px;margin-top:8px;">${rows}</div>
      </div>`;
  }).join("");

  // Find-or-create a LinkedIn {value,count} entry, upgrading any legacy bare
  // string for the same role in place (two entries would scrape it twice).
  function linkedinEntry(cfg, kw) {
    cfg.keywords = cfg.keywords || [];
    let entry = cfg.keywords.find((k) => typeof k === "object" && k.value === kw);
    if (!entry) entry = { value: kw, count: typeof cfg.count === "number" ? cfg.count : 15 };
    cfg.keywords = cfg.keywords.filter((k) => (typeof k === "object" ? k.value : k) !== kw);
    cfg.keywords.push(entry);
    return entry;
  }
  const cfgFor = (platform) => scraperSettingsCache.platforms[platform] || (scraperSettingsCache.platforms[platform] = { keywords: [], count: 20 });

  document.querySelectorAll("[data-platform-keyword]").forEach((cb) => cb.addEventListener("change", () => {
    const platform = cb.dataset.platform, kw = cb.dataset.platformKeyword, cfg = cfgFor(platform);
    cfg.keywords = cfg.keywords || [];
    if (PER_KEYWORD_COUNT_PLATFORMS.has(platform)) {
      if (cb.checked) linkedinEntry(cfg, kw);
      else cfg.keywords = cfg.keywords.filter((k) => (typeof k === "object" ? k.value : k) !== kw);
    } else {
      if (cb.checked && !cfg.keywords.includes(kw)) cfg.keywords.push(kw);
      if (!cb.checked) cfg.keywords = cfg.keywords.filter((v) => v !== kw);
    }
    switchToCustom();
    renderPlatformConfig();
  }));
  document.querySelectorAll("[data-platform-count]").forEach((input) => input.addEventListener("change", () => {
    cfgFor(input.dataset.platformCount).count = parseInt(input.value, 10) || 20;
    switchToCustom();
  }));
  document.querySelectorAll("[data-platform-keyword-count]").forEach((input) => input.addEventListener("change", () => {
    linkedinEntry(cfgFor(input.dataset.platformKeywordCount), input.dataset.keywordForCount).count = parseInt(input.value, 10) || 15;
    switchToCustom();
  }));
}

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
