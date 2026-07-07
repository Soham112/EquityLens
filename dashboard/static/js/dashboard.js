/* EquityLens Dashboard — frontend logic */

let allResults = [];
let tickerNames = {};   // ticker → company name, from /api/names

async function init() {
  await Promise.all([loadScan(), loadHealth(), loadJournal(), loadNames()]);
  setupFilters();
}

async function loadNames() {
  try {
    const res = await fetch('/api/names');
    if (res.ok) {
      tickerNames = await res.json();
      // Re-render if the scan table beat us here
      if (allResults.length) applyFilters();
    }
  } catch (e) { /* names are cosmetic — never block the table */ }
}

async function loadScan() {
  try {
    const res = await fetch('/api/scan');
    if (!res.ok) {
      document.getElementById('results-body').innerHTML =
        `<tr><td colspan="14" class="loading">No scan data. Run: <code>python workflows/daily_scan.py</code></td></tr>`;
      return;
    }
    const data = await res.json();
    allResults = data.results || [];

    if (data.empty) {
      document.getElementById('results-body').innerHTML =
        `<tr><td colspan="14" class="loading">${data.message}</td></tr>`;
      return;
    }

    document.getElementById('total-scanned').textContent = data.total_scanned ?? allResults.length;
    document.getElementById('buy-count').textContent = data.buy_signals ?? 0;
    document.getElementById('watchlist-count').textContent = data.watchlist_signals ?? 0;
    document.getElementById('last-updated').textContent = data.timestamp
      ? 'Updated ' + new Date(data.timestamp).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'})
      : '';

    if (data.regime) {
      const badge = document.getElementById('regime-badge');
      badge.textContent = data.regime;
      badge.className = 'badge badge-' + data.regime.toLowerCase();
    }
    if (data.vix != null) {
      document.getElementById('vix-display').textContent = `VIX ${data.vix.toFixed(1)}`;
    }
    const vixBanner = document.getElementById('vix-banner');
    if (data.new_buys_paused) {
      vixBanner.classList.add('visible');
    } else {
      vixBanner.classList.remove('visible');
    }

    renderTable(allResults);
  } catch (e) {
    console.error('loadScan:', e);
    document.getElementById('results-body').innerHTML =
      `<tr><td colspan="14" class="loading">Could not connect to API. Start: <code>uvicorn dashboard.app:app --reload</code></td></tr>`;
  }
}

async function loadHealth() {
  try {
    const res = await fetch('/api/health');
    if (!res.ok) return;
    const data = await res.json();
    document.getElementById('health-status').textContent = data.status || '—';
  } catch (e) { /* ignore */ }
}

async function loadJournal() {
  try {
    const res = await fetch('/api/journal/metrics');
    if (!res.ok) return;
    const data = await res.json();
    if (data.hit_rate != null) {
      document.getElementById('hit-rate').textContent = (data.hit_rate * 100).toFixed(0) + '%';
    }
    const driftEl = document.getElementById('drift-alert');
    if (data.drift_alert) {
      driftEl.textContent = 'YES';
      driftEl.style.color = 'var(--avoid)';
    } else {
      driftEl.textContent = data.total_closed ? 'No' : '—';
    }
  } catch (e) { /* ignore */ }
}

function renderTable(results) {
  const body = document.getElementById('results-body');
  if (!results.length) {
    body.innerHTML = '<tr><td colspan="14" class="loading">No results match filters.</td></tr>';
    return;
  }

  body.innerHTML = results.map(r => {
    const killClass = r.kill_switch ? ' kill-switch-row' : '';
    const flags = r.red_flags?.join(', ') || '';
    const qty = r.recommended_pct ? (r.recommended_pct * 100).toFixed(1) + '%' : '—';
    const epill = earningsPill(r.earnings_phase, r.days_to_earnings);

    const name = tickerNames[r.ticker] || '';
    return `<tr class="${killClass}" onclick="showDetail('${r.ticker}')">
      <td class="ticker-cell">${r.ticker}</td>
      <td class="name-cell" title="${name}">${name || '—'}</td>
      <td><span class="pill pill-${r.signal}">${signalLabel(r.signal)}</span></td>
      <td class="score-cell">${(r.conviction ?? 0).toFixed(1)}</td>
      <td class="score-cell">${(r.data_confidence ?? 0).toFixed(1)}</td>
      <td class="score-cell">${(r.hunter_score ?? 0).toFixed(1)}</td>
      <td><span class="dot dot-${r.data_quality}"></span>${r.data_quality}</td>
      <td>${epill}</td>
      <td>${r.sector ?? '—'}</td>
      <td class="price-cell">${r.stop_tier1 ? '$'+r.stop_tier1.toFixed(2) : '—'}</td>
      <td class="price-cell">${r.stop_tier2 ? '$'+r.stop_tier2.toFixed(2) : '—'}</td>
      <td class="price-cell">${r.stop_tier3 ? '$'+r.stop_tier3.toFixed(2) : '—'}</td>
      <td class="price-cell">${qty}</td>
      <td class="flag-cell" title="${flags}">${flags || '—'}</td>
    </tr>`;
  }).join('');
}

function applyFilters() {
  const sig = document.getElementById('filter-signal').value;
  const minConv = parseFloat(document.getElementById('filter-conviction').value) || 0;
  const quality = document.getElementById('filter-quality').value;
  const earnings = document.getElementById('filter-earnings').value;
  const search = document.getElementById('filter-search').value.toUpperCase().trim();

  const cautionPhases = ['EARNINGS_CAUTION', 'EARNINGS_BLACKOUT'];

  let filtered = allResults.filter(r => {
    if (sig !== 'ALL' && r.signal !== sig) return false;
    if (r.conviction < minConv) return false;
    if (quality === 'GREEN' && r.data_quality !== 'GREEN') return false;
    if (quality === 'YELLOW' && !['GREEN','YELLOW'].includes(r.data_quality)) return false;
    if (earnings !== 'ALL') {
      const phase = r.earnings_phase ?? 'NORMAL';
      if (earnings === 'NORMAL' && phase !== 'NORMAL') return false;
      if (earnings === 'EARNINGS_WATCH' && phase !== 'EARNINGS_WATCH') return false;
      if (earnings === 'EARNINGS_CAUTION' && !cautionPhases.includes(phase)) return false;
      if (earnings === 'EARNINGS_BLACKOUT' && phase !== 'EARNINGS_BLACKOUT') return false;
    }
    if (search && !r.ticker.includes(search)
        && !(tickerNames[r.ticker] || '').toUpperCase().includes(search)) return false;
    return true;
  });

  renderTable(filtered);
}

function setupFilters() {
  ['filter-signal','filter-conviction','filter-quality','filter-earnings','filter-search'].forEach(id => {
    document.getElementById(id)?.addEventListener('input', applyFilters);
    document.getElementById(id)?.addEventListener('change', applyFilters);
  });

  document.getElementById('btn-refresh')?.addEventListener('click', () => {
    allResults = [];
    document.getElementById('results-body').innerHTML =
      '<tr><td colspan="14" class="loading">Refreshing...</td></tr>';
    init();
  });

  document.getElementById('btn-analyze')?.addEventListener('click', () => {
    document.getElementById('analyze-modal').style.display = 'flex';
  });

  document.getElementById('btn-close-modal')?.addEventListener('click', () => {
    document.getElementById('analyze-modal').style.display = 'none';
    document.getElementById('modal-result').style.display = 'none';
    document.getElementById('modal-result').textContent = '';
  });

  document.getElementById('btn-close-detail')?.addEventListener('click', () => {
    document.getElementById('detail-panel').style.display = 'none';
  });

  document.getElementById('btn-run-analysis')?.addEventListener('click', runAnalysis);
}

async function runAnalysis() {
  const ticker = document.getElementById('modal-ticker').value.trim().toUpperCase();
  const sector = document.getElementById('modal-sector').value;
  if (!ticker) return;

  const btn = document.getElementById('btn-run-analysis');
  btn.disabled = true;
  btn.textContent = 'Analyzing...';

  const resultEl = document.getElementById('modal-result');
  resultEl.style.display = 'block';
  resultEl.textContent = 'Running analysis pipeline...';

  try {
    const res = await fetch(`/api/analyze/${ticker}?sector=${sector}`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) {
      resultEl.textContent = `Error: ${data.detail || 'Unknown error'}`;
      return;
    }
    const earningsLine = data.earnings_phase && data.earnings_phase !== 'NORMAL'
      ? `\nEarnings:   ${earningsPhaseName(data.earnings_phase)}${data.days_to_earnings != null ? ' ('+data.days_to_earnings+'d out)' : ''}`
      : '';
    resultEl.textContent = [
      `Signal:     ${data.signal}`,
      `Conviction: ${data.conviction?.toFixed(1)} | Data Conf: ${data.data_confidence?.toFixed(1)}`,
      `Hunter:     ${data.hunter_score?.toFixed(1)} | Sentiment: ${data.sentiment_boost?.toFixed(2)}`,
      `Quality:    ${data.data_quality}${earningsLine}`,
      `Stops:      T1=$${data.stop_tier1?.toFixed(2) ?? '—'} T2=$${data.stop_tier2?.toFixed(2) ?? '—'} T3=$${data.stop_tier3?.toFixed(2) ?? '—'}`,
      `Size:       ${data.recommended_position_pct ? (data.recommended_position_pct*100).toFixed(1)+'%' : '—'}`,
      ``,
      `Thesis: ${data.thesis}`,
      data.alerts?.length ? `\nAlerts:\n${data.alerts.map(a=>'  · '+a).join('\n')}` : '',
    ].join('\n');
  } catch (e) {
    resultEl.textContent = `Error: ${e.message}`;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Run Analysis';
  }
}

function showDetail(ticker) {
  const r = allResults.find(x => x.ticker === ticker);
  if (!r) return;

  document.getElementById('detail-panel').style.display = 'block';
  document.getElementById('detail-ticker').textContent =
    `${r.ticker} — ${r.signal}`;

  document.getElementById('detail-thesis').textContent = r.thesis || '—';

  document.getElementById('detail-scores').innerHTML = kv([
    ['Conviction', (r.conviction ?? 0).toFixed(1)],
    ['Data Confidence', (r.data_confidence ?? 0).toFixed(1)],
    ['Hunter Score', (r.hunter_score ?? 0).toFixed(1)],
    ['Sentiment Boost', (r.sentiment_boost ?? 0).toFixed(2)],
    ['Sector Status', r.sector_status || '—'],
    ['Regime', r.regime || '—'],
    ['Rec. Position', r.recommended_pct ? (r.recommended_pct*100).toFixed(1)+'%' : '—'],
  ]);

  document.getElementById('detail-stops').innerHTML = kv([
    ['Tier 1 — Alert', r.stop_tier1 ? '$'+r.stop_tier1.toFixed(2) : '—'],
    ['Tier 2 — Confirm', r.stop_tier2 ? '$'+r.stop_tier2.toFixed(2) : '—'],
    ['Tier 3 — Hard Stop', r.stop_tier3 ? '$'+r.stop_tier3.toFixed(2) : '—'],
  ]);

  const ep = r.earnings_phase ?? 'NORMAL';
  const daysOut = r.days_to_earnings;
  const earningsRows = [
    ['Phase', earningsPhaseName(ep)],
    ['Days to Earnings', daysOut != null ? daysOut + 'd' : '—'],
  ];
  if (ep === 'EARNINGS_CAUTION') {
    earningsRows.push(['Action', 'No new buys — stops widened 1× ATR']);
  } else if (ep === 'EARNINGS_BLACKOUT') {
    earningsRows.push(['Action', 'BLACKOUT — no entries, stops widened 1.5× ATR']);
  } else if (ep === 'EARNINGS_WATCH') {
    earningsRows.push(['Action', 'Monitor — normal sizing allowed']);
  } else {
    earningsRows.push(['Action', 'Normal — no restrictions']);
  }
  document.getElementById('detail-earnings').innerHTML = kv(earningsRows);

  const flagsEl = document.getElementById('detail-flags');
  if (r.red_flags?.length) {
    flagsEl.innerHTML = r.red_flags.map(f =>
      `<div class="flag-item">· ${f}</div>`).join('');
  } else {
    flagsEl.innerHTML = '<div style="color:var(--quality-green);font-size:12px">No red flags</div>';
  }

  const alertsEl = document.getElementById('detail-alerts');
  if (r.alerts?.length) {
    alertsEl.innerHTML = r.alerts.map(a =>
      `<div class="alert-item">· ${a}</div>`).join('');
  } else {
    alertsEl.innerHTML = '<div style="color:var(--text-muted);font-size:12px">No alerts</div>';
  }

  // Scroll to detail panel
  document.getElementById('detail-panel').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function signalLabel(signal) {
  const labels = { TRIM_25: 'TRIM 25%', TRIM_50: 'TRIM 50%', EXIT: 'EXIT', NO_ADD: 'NO ADD' };
  return labels[signal] ?? signal;
}

function earningsPill(phase, daysOut) {
  if (!phase || phase === 'NORMAL') return '<span class="epill epill-NORMAL">—</span>';
  const labels = {
    EARNINGS_WATCH:    `Watch ${daysOut != null ? daysOut+'d' : ''}`,
    EARNINGS_CAUTION:  `Caution ${daysOut != null ? daysOut+'d' : ''}`,
    EARNINGS_BLACKOUT: `Blackout ${daysOut != null ? daysOut+'d' : ''}`,
  };
  return `<span class="epill epill-${phase}">${labels[phase] ?? phase}</span>`;
}

function earningsPhaseName(phase) {
  const names = {
    NORMAL:            'Normal',
    EARNINGS_WATCH:    'Earnings Watch',
    EARNINGS_CAUTION:  'Earnings Caution',
    EARNINGS_BLACKOUT: 'Earnings Blackout',
  };
  return names[phase] ?? phase;
}

function kv(pairs) {
  return pairs.map(([k, v]) =>
    `<div class="kv-row"><span class="kv-key">${k}</span><span class="kv-value">${v}</span></div>`
  ).join('');
}

document.addEventListener('DOMContentLoaded', init);
