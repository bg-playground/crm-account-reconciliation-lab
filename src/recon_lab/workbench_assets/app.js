"use strict";
const $ = id => document.getElementById(id);
let data, selected, history = [], storageKey, dirty = false, persistent = true;
const labels = {merge_splink: "Merge · Splink", merge_ai: "Merge · AI", nomatch_ai: "No match · AI", review_ai_uncertain: "Human review · uncertain", review_ai_missing: "Human review · missing answer", review_not_judged: "Human review · not judged", not_blocked: "Not found by blocking"};
const help = {review: "Pairs the published cascade left for human review.", false_merge: "Merge recommendations that contradict synthetic ground truth.", missed_match: "True pairs not automatically matched, including review cases and blocking misses.", disagreement: "Recorded main-arm repeats land in different frozen routing bands. Unequal probabilities within one band do not count.", all: "All blocked candidates plus true pairs missed by blocking. Audit-only AI calls do not override Splink routing."};
const esc = value => String(value ?? "—").replace(/[&<>"']/g, c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
const pct = value => value == null ? "—" : value > 0 && value < 0.001 ? "<0.1%" : value < 1 && value > 0.999 ? ">99.9%" : (value * 100).toFixed(1) + "%";
function latest(id) { return history.findLast(row => row.pair_id === id); }
function status(message) { $("save-status").textContent = message; }
function allowNavigation() {
  if (!dirty) return true;
  status("Save or discard the current assessment before switching pairs or filters.");
  return false;
}
function matching() {
  const filter = $("filter").value, progress = $("progress").value, query = $("search").value.trim().toLowerCase();
  return data.pairs.filter(pair => (filter === "all" || pair.tags.includes(filter)) &&
    (progress === "all" || (progress === "assessed") === Boolean(latest(pair.id))) &&
    (!query || [pair.id, pair.left.Name, pair.right.Name].some(v => v.toLowerCase().includes(query))));
}
function renderQueue() {
  const rows = matching();
  $("count").textContent = `${rows.length} pairs`;
  $("filter-help").textContent = help[$("filter").value];
  $("pairs").replaceChildren();
  if (!rows.some(p => p.id === selected)) selected = rows[0]?.id;
  if (!rows.length) $("pairs").innerHTML = '<p class="empty">No pairs match these filters.</p>';
  for (const pair of rows) {
    const button = document.createElement("button");
    button.type = "button"; button.className = "pair";
    button.setAttribute("aria-current", String(pair.id === selected));
    button.innerHTML = `${esc(pair.left.Name)}<br>${esc(pair.right.Name)}<span>${esc(labels[pair.route])} · ${latest(pair.id) ? "Assessed" : "Not assessed"}</span>`;
    button.addEventListener("click", () => { if (allowNavigation()) { selected = pair.id; renderQueue(); renderDetail(); } });
    $("pairs").append(button);
  }
}
function renderDetail() {
  const pair = data.pairs.find(p => p.id === selected);
  if (!pair) { $("detail").innerHTML = '<p class="empty">Choose another filter to explore more evidence.</p>'; return; }
  const saved = latest(pair.id);
  const fieldLabels = {org: "Org", Id: "Account ID", Name: "Name", Website: "Website", Phone: "Phone", BillingStreet: "Street", BillingCity: "City", BillingState: "State", BillingPostalCode: "Postal code", Industry: "Industry", NumberOfEmployees: "Employees"};
  const fields = ["org", "Id", "Name", "Website", "Phone", "BillingStreet", "BillingCity", "BillingState", "BillingPostalCode", "Industry", "NumberOfEmployees"];
  $("detail").innerHTML = `<h2>${esc(labels[pair.route])}</h2><p class="subtitle">${esc(pair.id)}</p>
    <div class="table-scroll"><table><thead><tr><th scope="col">Field</th><th scope="col">Left Account</th><th scope="col">Right Account</th></tr></thead><tbody>${fields.map(field => `<tr><th scope="row">${esc(fieldLabels[field])}</th><td>${esc(pair.left[field] || "—")}</td><td>${esc(pair.right[field] || "—")}</td></tr>`).join("")}</tbody></table></div>
    <div class="scores"><p><strong>Published routing:</strong> ${esc(labels[pair.route])}</p><p><strong>Splink:</strong> ${pair.score ? `weight ${pair.score.mw.toFixed(3)} · probability ${esc(pct(pair.score.p))} · baseline ${esc(pair.baseline)}` : "No blocked candidate; no score or AI call"}</p>
    <p><strong>Recorded AI repeats:</strong> ${pair.probabilities.length ? pair.probabilities.map(v => esc(pct(v))).join(" / ") + ` · mean ${esc(pct(pair.mean))}` : "Not called"}</p>
    <p class="muted">Frozen cutoffs: Splink weight ≥ ${data.cutoffs.splink_merge_weight}; AI mean ≥ ${data.cutoffs.ai_merge} for merge, ≤ ${data.cutoffs.ai_nonmatch} for no match. AI calls on Splink merges are calibration audits. No model rationale was recorded.</p></div>
    <details class="truth"><summary>Reveal synthetic ground truth</summary><p>${pair.truth ? "Same entity" : "Different entities"}. This is a retrospective label, not evidence available to a real-world reviewer.</p></details>
    <section class="assessment"><h2>Your assessment</h2><label for="decision">Decision</label><select id="decision"><option value="">Choose a decision</option><option value="same_entity">Same entity</option><option value="different_entities">Different entities</option><option value="defer">Defer</option></select>
    <label for="notes">Notes</label><textarea id="notes" maxlength="4000" placeholder="Record the evidence behind your assessment"></textarea><div class="actions"><button id="save" type="button">Save assessment</button><button id="discard" type="button" class="secondary">Discard edits</button></div><p class="muted" id="saved-at"></p></section>`;
  $("decision").value = saved?.decision || ""; $("notes").value = saved?.notes || "";
  $("saved-at").textContent = saved ? `Last assessment: ${new Date(saved.timestamp).toLocaleString()}` : "No assessment saved for this pair.";
  for (const id of ["decision", "notes"]) $(id).addEventListener("input", () => { dirty = true; status("Unsaved assessment."); });
  $("discard").addEventListener("click", () => { dirty = false; renderDetail(); status("Edits discarded."); });
  $("save").addEventListener("click", () => {
    if (!$("decision").value) { status("Choose a decision before saving."); $("decision").focus(); return; }
    history.push({pair_id: pair.id, decision: $("decision").value, notes: $("notes").value.trim(), timestamp: new Date().toISOString()});
    try { localStorage.setItem(storageKey, JSON.stringify(history)); persistent = true; }
    catch { persistent = false; }
    dirty = false; renderQueue(); renderDetail();
    status(persistent ? `Assessment saved in this browser. ${new Set(history.map(r => r.pair_id)).size} pairs assessed.` : "Browser storage unavailable. Assessment kept in memory; export before closing.");
  });
}
async function start() {
  try {
    const response = await fetch("/api/evidence"); if (!response.ok) throw new Error("Evidence could not be loaded.");
    data = await response.json(); storageKey = `recon-review:${data.evidence_id}`;
    try {
      const stored = JSON.parse(localStorage.getItem(storageKey) || "[]");
      const ids = new Set(data.pairs.map(p => p.id));
      if (!Array.isArray(stored) || !stored.every(r => r && ids.has(r.pair_id) && ["same_entity", "different_entities", "defer"].includes(r.decision) && typeof r.notes === "string" && typeof r.timestamp === "string" && !Number.isNaN(Date.parse(r.timestamp)))) throw new Error("Invalid local review session");
      history = stored;
    } catch { status("Saved review session could not be loaded. This session starts empty; export your work before closing."); }
    $("audit").textContent = `Evidence verified · ${data.verification.calls_verified.toLocaleString()} recorded calls · 0 provider calls`;
    const metrics = data.metrics;
    const cards = [["Auto-merge precision", pct(metrics.cascade.precision.value), `Baseline ${pct(metrics.baseline.precision.value)}`], ["Auto-merge recall", pct(metrics.cascade.recall.value), `Baseline ${pct(metrics.baseline.recall.value)}`], ["Human review queue", data.pairs.filter(p => p.tags.includes("review")).length, "Published routing"], ["Incorrect merge recommendations", data.pairs.filter(p => p.tags.includes("false_merge")).length, "Against synthetic ground truth"]];
    $("summary").innerHTML = cards.map(([label,value,detail]) => `<div class="metric"><span>${esc(label)}</span><strong>${esc(value)}</strong><span>${esc(detail)}</span></div>`).join("");
    for (const id of ["filter", "progress", "search"]) {
      let previous = $(id).value;
      $(id).addEventListener(id === "search" ? "input" : "change", () => {
        if (!allowNavigation()) { $(id).value = previous; return; }
        previous = $(id).value; renderQueue(); renderDetail();
      });
    }
    $("export").addEventListener("click", () => {
      if (!allowNavigation()) return;
      const session = {schema: "recon-review-session-v1", run_id: data.run_id, evidence_id: data.evidence_id,
        exported_at: new Date().toISOString(), purpose: "Synthetic retrospective review; no merge authority; published verdict unchanged",
        ground_truth_available: true, published_verdict: data.verification.published_verdict, events: history};
      const url = URL.createObjectURL(new Blob([JSON.stringify(session, null, 2)], {type: "application/json"}));
      const a = document.createElement("a"); a.href = url; a.download = "synthetic-review-session.json"; a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      status(`Exported ${history.length} assessment events. Export is a review session, not a reconciliation plan.`);
    });
    renderQueue(); renderDetail();
  } catch (error) { $("error").hidden = false; $("error").textContent = error.message; $("audit").textContent = "Evidence unavailable"; $("export").disabled = true; }
}
window.addEventListener("beforeunload", event => { if (dirty) { event.preventDefault(); event.returnValue = ""; } });
start();
