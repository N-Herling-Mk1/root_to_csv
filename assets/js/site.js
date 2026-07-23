console.log('root_to_csv site.js v34 loaded');
/* root2csv site.js — every feature isolated so one failure can't kill the rest */


/* ---------------- copy buttons ---------------- */
try {
document.querySelectorAll('[data-copy]').forEach(btn => {
  btn.addEventListener('click', () => {
    const block = btn.parentElement.cloneNode(true);
    block.querySelector('button').remove();
    const text = block.textContent.replace(/^\n+|\s+$/g, '');
    (navigator.clipboard ? navigator.clipboard.writeText(text)
                         : Promise.reject()).then(() => {
      btn.textContent = 'COPIED'; btn.classList.add('ok');
      setTimeout(() => { btn.textContent = 'COPY'; btn.classList.remove('ok'); }, 1400);
    }).catch(() => { btn.textContent = 'SELECT+C'; });
  });
});
} catch (err) { console.warn('copy buttons:', err); }
/* ---------------- glossary (glossary.html only) ---------------- */
try {
const TERMS = [
["base policy","The defaults you get by never editing manifest.json (or taking the quick path): jagged = pad_max with -999 pads, scalar as-is, vec1 collapsed, hard structs per the fate table. Run the steps in sequence and take what comes out — policy editing is opt-in."],
["branch","One named column of a TTree/RNTuple. Every branch is read once in Pass 1 and filed into exactly one of the six bins."],
["TTree","ROOT's classic columnar event container. The toolkit auto-detects the tree with the most entries; override with --tree."],
["RNTuple","ROOT's next-generation columnar format. Read transparently by the same code path as TTrees — future-proofing for the ATLAS migration."],
["event","One row of the tree — one collision record. Rows in the flat CSV correspond 1:1 to events."],
["jagged array","A branch whose vector length varies per event (2 jets here, 4 there). The defining ATLAS shape, and the reason this toolkit exists."],
["awkward array","The python library (ak) that represents jagged/nested data natively. Pass 1 reads every branch as an awkward array to classify it."],
["uproot","Pure-python ROOT I/O. Reads TTrees and RNTuples with no ROOT build or ATLAS environment — why deploy is just git clone + pip."],
["sentinel / pad flag","The value written where a short event has no element for a column. Base policy: -999 (numeric — columns stay numeric). --fill x restores the legacy events.root 'x'; --fill nan pads NaN; any number works."],
["spacer count","How many sentinel insertions a jagged branch generates when padded to max_len. Reported per branch and in total — the bloat meter."],
["canonical parquet","The bulk-storage artifact: every readable branch with lists kept as lists, jagged-deep intact. CSVs are disposable views regenerated from it."],
["manifest.json","Machine-readable census of every branch: bin, types, length stats, samples, and the editable per-branch policy. Drives --from-scan builds."],
["manifest.txt","Human-readable audit of the same census — six-bin summary, per-branch listing, jagged-deep detail, condensed flattened-vs-unreadable list."],
["policy","Per-jagged-branch instruction in manifest.json: pad_max (default), first:N (keep N columns), or drop (exclude from CSV)."],
["pad_max","Default jagged policy — explode to max_len columns, pad shorter events with the -999 flag (use --fill x for byte-parity with the original events.root CSVs)."],
["first:N","Jagged policy — keep only columns _0…_{N-1}. The cure for outlier-driven column explosions."],
["drop","Jagged policy — exclude the branch from the CSV entirely. It stays in the canonical parquet."],
["fan-out","How many CSV columns a branch produces. Scalars/vec1 → 1; jagged → max_len; jagged-deep → the hypothetical count you were spared."],
["len_med / len_p95 / len_max","Length statistics per jagged branch. A big gap between median and max flags one fat outlier event setting your column count."],
["fill fraction","Share of events where a jagged branch is non-empty. Low fill + high max_len = a mostly-sentinel block of columns."],
["tag column","--tag KEY=VALUE stamps an integer column on every row — provenance labels like is_signal=1 or channel flags."],
["vec1 collapse","Length-1 vectors are unwrapped to plain scalars via [:,0] — no _0 suffix, no padding."],
["jagged-deep","ndim > 2: lists of lists per event. Never flattened to CSV; preserved intact in the parquet with full structure recorded in the manifest."],
["unreadable","A branch uproot cannot decode (custom C++ class). Name, C++ typename, and reason are recorded — that's all that can be done, and it's enough."],
["counter branch","ROOT-generated nX branches (njet_pt) that store per-event vector lengths. They read as ordinary scalars."],
["quick mode (2a)","convert.py file.root — scan + flatten in one shot with default policies. No pre-scan step, hard structs removed from the CSV."],
["from-scan mode (2b)","convert.py --from-scan DIR — build the CSV from the canonical parquet, honoring your policy edits. ROOT is never re-read."],
["build report","<name>_build.txt from a from-scan build: CSV shape and size, fill used, and which branches the policies dropped."],
["headless","A server with no display. Everything here is CLI + files; anything visual is a file you copy down or view through a tunnel."],
["loopback / 127.0.0.1","The bind address that keeps a served page reachable only from the machine itself. Rule zero for anything served on shared servers."],
["ssh tunnel","ssh -L localport:localhost:remoteport user@server — forwards your local browser to a loopback-bound service. The only door."],
["code-server","VS Code in a browser tab, running on the server. Password lives in ~/.config/code-server/config.yaml on the server."],
["bigmem3 / atlng01 / atlng02","Group compute nodes this toolkit targets. Deploy is identical everywhere: clone, pip install, run."]
];

const gl = document.getElementById('gl');
if (gl) {
  gl.innerHTML = TERMS.map(([t, d]) =>
    `<div class="e"><div class="t">${t}</div><div class="d">${d}</div></div>`).join('');
  const entries = [...gl.children];
  const q = document.getElementById('q'), qc = document.getElementById('qcount'),
        none = document.getElementById('glnone');
  const filter = () => {
    const s = q.value.trim().toLowerCase();
    let hits = 0;
    entries.forEach((el, i) => {
      const hay = (TERMS[i][0] + ' ' + TERMS[i][1]).toLowerCase();
      const ok = !s || hay.includes(s);
      el.classList.toggle('hide', !ok);
      if (ok) hits++;
    });
    qc.textContent = s ? hits + ' / ' + TERMS.length : TERMS.length + ' terms';
    none.classList.toggle('show', hits === 0);
  };
  q.addEventListener('input', filter);
  filter();
}
} catch (err) { console.warn('glossary (glossary.html only):', err); }
/* ---------------- landing stepper (index.html only) ---------------- */
try {
const steps = document.querySelectorAll('.tstep');
if (steps.length) {
  steps.forEach(b => b.addEventListener('click', () => {
    steps.forEach(x => {
      const on = x === b;
      x.classList.toggle('on', on);
      x.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    document.querySelectorAll('.tpane').forEach(p =>
      p.classList.toggle('on', p.id === b.dataset.pane));
    document.querySelectorAll('.taccess .ta').forEach(a =>
      a.classList.toggle('on', a.dataset.pane === b.dataset.pane));
  }));
}
} catch (err) { console.warn('landing stepper (index.html only):', err); }


/* ---------------- edge rail: contextual hover guide (v5) ---------------- */
function initGuideRail() {
  try {
  const rail = document.getElementById('edgerail');
  if (rail) {
    if (rail.dataset.armed) return;      // idempotent — never double-bind
    rail.dataset.armed = '1';
    const tracer = document.getElementById('erTracer');
    const card   = document.getElementById('erCard');
    const tEl    = document.getElementById('erTitle');
    const xEl    = document.getElementById('erText');
    const bEl    = document.getElementById('erClick');
    let hideTimer = null;

    const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

    function showFor(el) {
      const r  = el.getBoundingClientRect();
      const er = rail.getBoundingClientRect();
      const y  = clamp(r.top + r.height / 2 - er.top, 14, er.height - 14);
      tEl.textContent = el.dataset.guideTitle || 'GUIDE';
      xEl.textContent = el.dataset.guide || '';
      const clickable = el.dataset.guideClick === '1';
      bEl.style.display = clickable ? 'inline-block' : 'none';
      rail.classList.toggle('clickable', clickable);
      tracer.style.top = y + 'px';
      card.style.top   = clamp(y, 84, er.height - 84) + 'px';
      rail.classList.add('active');
      clearTimeout(hideTimer);
    }
    function scheduleIdle() {
      clearTimeout(hideTimer);
      hideTimer = setTimeout(() => {
        rail.classList.remove('active', 'clickable');
        tracer.style.top = '';
      }, 350);
    }
    document.addEventListener('mouseover', e => {
      const t = e.target.closest && e.target.closest('[data-guide]');
      if (t) showFor(t);
      else if (!(e.target.closest && e.target.closest('#edgerail'))) scheduleIdle();
    });
    document.addEventListener('mouseleave', scheduleIdle);
  }
  } catch (err) { console.warn('guide rail:', err); }
}
initGuideRail();                                   // rail precedes this script
document.addEventListener('DOMContentLoaded', initGuideRail);   // safety net
