'use strict';
// A local research comparison never starts trading or changes a strategy.
let researchPollTimer = null;
function researchCell(row, text) {
  const td = document.createElement('td'); td.textContent = text; row.appendChild(td);
}
async function showResearch() {
  const response = await apiFetch('/api/research');
  if (!response.ok) return;
  const data = await response.json();
  const output = document.getElementById('researchState');
  const body = document.getElementById('researchRows');
  if (data.busy) {
    output.textContent = 'Tester lokale prisdata. Ingen ordre sendes.';
    clearTimeout(researchPollTimer);
    researchPollTimer = setTimeout(showResearch, 3000);
    return;
  }
  if (data.error) { output.textContent = 'Testen kunne ikke fullføres: ' + data.error; return; }
  const results = data.results || {};
  const actual = data.realized_review || {};
  document.getElementById('researchActual').textContent = actual.available ?
    'Siste 7 dager: ' + actual.bot_closed_legs + ' avsluttede bot-deler, brutto ' + actual.bot_gross_usd.toFixed(2) + ' USD. Manuelt/uavklart: ' + actual.manual_or_unattributed_gross_usd.toFixed(2) + ' USD. Netto etter alle gebyrer er ikke verifisert.' :
    'Venter på lokal meglerhistorikk for å skille bot-handler fra manuelle posisjoner.';
  const symbols = results.symbols || {};
  body.replaceChildren();
  let shown = 0;
  for (const [symbol, report] of Object.entries(symbols)) {
    if (report.state === 'WAIT_HISTORY') {
      const tr = document.createElement('tr');
      for (const text of [symbol, 'Samler historikk', String(report.bars) + ' minuttbarer', '—', '—', '—']) researchCell(tr, text);
      body.appendChild(tr); continue;
    }
    for (const model of report.models || []) {
      const tr = document.createElement('tr'), h = model.holdout;
      const labels = {sma_reference:'SMA-referanse',trend_pullback:'Trend + tilbakefall',donchian_breakout:'Donchian-brudd',range_reversion:'Sideveis marked'};
      const label = (labels[model.strategy] || model.strategy) + ' ' + model.timeframe_minutes + 'm / ' + (model.execution === 'market' ? 'marked' : 'limitmodell') + ' / ' + (model.exit_mode === 'fixed' ? 'fast mål' : 'ATR-trailing');
      for (const text of [symbol, label, !model.data_ready ? 'Samler historikk' + (model.required_hour_bars ? ' (' + model.completed_hour_bars + '/' + model.required_hour_bars + ' timer)' : '') : String(h.trades) + (model.sufficient_samples ? '' : ' (lite utvalg)'),
        h.net_expectancy_bps == null ? '—' : (h.net_expectancy_bps / 100).toFixed(3) + '%',
        h.profit_factor == null ? '—' : h.profit_factor.toFixed(2), h.max_drawdown_pct.toFixed(3) + '%']) researchCell(tr, text);
      body.appendChild(tr); shown++;
    }
  }
  output.textContent = shown ? 'Resultater fra siste 30 % av perioden. Ingen automatisk strategibytte. ' + (results.updated_at || '') : 'Venter på nok sammenhengende historikk. Ingen ekstra markedsdata lastes ned.';
  const d = results.edge_diagnostics;
  if (d) output.textContent += ' Kryptovurderinger med positiv beregnet nettofordel: ' + d.crypto_positive_net_ev + '/' + d.crypto_observations + (d.sample_limited ? ' (siste 10 000 loggrader).' : '.');
}
async function runResearch() {
  const button = document.getElementById('researchRun'); button.disabled = true;
  try {
    const response = await apiFetch('/api/research/run', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'test_failed');
    await showResearch();
  } catch (error) {
    document.getElementById('researchState').textContent = error.message === 'research_cooldown' ? 'Kan testes én gang hvert 15. minutt. Forrige resultat beholdes.' : 'Kunne ikke starte testen: ' + error.message;
  } finally { button.disabled = false; }
}
