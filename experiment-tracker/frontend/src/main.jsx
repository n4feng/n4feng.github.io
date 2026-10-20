import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './styles.css';
import { buildPlotOptions, savePlotOptions } from './plot-options.js';

const COLORS = ['#4b69df', '#159b93', '#e59139', '#b567c4', '#dc626c', '#7584a4'];
const SERVER_KEY = 'naihe-experiments-server';
const SESSION_KEY = 'naihe-experiments-session';

function Icon({ name, size = 20, ...rest }) {
  const paths = {
    grid: <><rect x="3" y="3" width="7" height="7" rx="1.6"/><rect x="14" y="3" width="7" height="7" rx="1.6"/><rect x="3" y="14" width="7" height="7" rx="1.6"/><rect x="14" y="14" width="7" height="7" rx="1.6"/></>,
    chart: <><path d="M4 3v17h17"/><path d="m7 14 4-5 4 2 6-7"/></>,
    bars: <><path d="M4 3v17h17"/><path d="M8 16v-5m5 5V6m5 10V9"/></>,
    plus: <path d="M12 5v14M5 12h14"/>,
    arrow: <path d="M5 12h14m-5-5 5 5-5 5"/>,
    chevron: <path d="m9 5 7 7-7 7"/>,
    close: <path d="m6 6 12 12M6 18 18 6"/>,
    upload: <><path d="M12 16V3m-5 5 5-5 5 5"/><path d="M4 15v5h16v-5"/></>,
    download: <><path d="M12 3v13m-5-5 5 5 5-5"/><path d="M4 17v4h16v-4"/></>,
    lock: <><rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3M12 14v3"/></>,
    book: <><path d="M4 4h6a3 3 0 0 1 3 3v14a4 4 0 0 0-4-2H4zM13 7a3 3 0 0 1 3-3h4v15h-3a4 4 0 0 0-4 2"/></>,
    logout: <><path d="M9 4H4v16h5m7-12 4 4-4 4M9 12h11"/></>,
    folder: <path d="M3 7V5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/>,
    edit: <><path d="m15 4 5 5M4 20l5-1L21 7a2 2 0 0 0-4-4L5 15z"/></>,
    search: <><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/></>,
    check: <path d="m5 12 4 4L19 6"/>,
    server: <><rect x="3" y="3" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="7" rx="2"/><path d="M7 6.5h.01M7 17.5h.01M11 6.5h6M11 17.5h6"/></>,
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...rest}>{paths[name] || paths.chart}</svg>;
}

function normalizeServer(value) {
  const trimmed = value.trim().replace(/\/+$/, '');
  if (!trimmed) throw new Error('Enter the HTTPS URL of your deployed Flask server.');
  let url;
  try { url = new URL(trimmed); } catch { throw new Error('Enter a complete server URL, such as https://experiments.example.com.'); }
  const local = ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname);
  if (url.protocol !== 'https:' && !(local && url.protocol === 'http:')) throw new Error('Use HTTPS for your server. HTTP is allowed only on localhost.');
  if (url.username || url.password || url.search || url.hash) throw new Error('The server URL must not contain credentials, a query, or a fragment.');
  return trimmed;
}

function readStored(key, storageName = 'localStorage') {
  try { return window[storageName].getItem(key) || ''; } catch { return ''; }
}

function useRoute() {
  const parse = () => window.location.hash.replace(/^#\/?/, '').split('/').filter(Boolean).map(part => { try { return decodeURIComponent(part); } catch { return part; } });
  const [route, setRoute] = useState(parse);
  useEffect(() => { const listener = () => setRoute(parse()); window.addEventListener('hashchange', listener); return () => window.removeEventListener('hashchange', listener); }, []);
  return route;
}

const projectHref = id => `#/projects/${encodeURIComponent(id)}`;
const experimentHref = (projectId, id) => `${projectHref(projectId)}/experiments/${encodeURIComponent(id)}`;
const pretty = key => key.replace(/_/g, ' ').replace(/\b\w/g, char => char.toUpperCase());
const formatNumber = value => value !== 0 && Math.abs(value) < 0.001 ? value.toExponential(2) : Math.abs(value) >= 1000 ? new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 2 }).format(value) : new Intl.NumberFormat('en', { maximumFractionDigits: 4 }).format(value);

function Notice({ children, type = 'error' }) { return children ? <div className={`notice ${type}`} role={type === 'error' ? 'alert' : 'status'}>{children}</div> : null; }
function Field({ label, help, children, className = '' }) { return <label className={`field ${className}`}><span className="field-label">{label}</span>{children}{help && <span className="field-help">{help}</span>}</label>; }

function Modal({ title, subtitle, children, onClose, wide = false }) {
  const ref = useRef(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const previous = document.activeElement;
    const dialog = ref.current;
    dialog?.querySelector('input, select, textarea, button')?.focus();
    const keydown = event => {
      if (event.key === 'Escape') closeRef.current();
      if (event.key !== 'Tab') return;
      const items = [...dialog.querySelectorAll('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href]')].filter(item => item.getClientRects().length);
      if (!items.length) return;
      if (event.shiftKey && document.activeElement === items[0]) { event.preventDefault(); items.at(-1).focus(); }
      else if (!event.shiftKey && document.activeElement === items.at(-1)) { event.preventDefault(); items[0].focus(); }
    };
    document.addEventListener('keydown', keydown);
    document.body.style.overflow = 'hidden';
    return () => { document.removeEventListener('keydown', keydown); document.body.style.overflow = ''; previous?.focus(); };
  }, []);
  return <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}><section ref={ref} className={`modal ${wide ? 'wide' : ''}`} role="dialog" aria-modal="true" aria-labelledby="dialog-title"><div className="modal-heading"><div><h2 id="dialog-title">{title}</h2>{subtitle && <p>{subtitle}</p>}</div><button className="icon-button" onClick={onClose} aria-label="Close dialog"><Icon name="close"/></button></div>{children}</section></div>;
}

function Login({ initialServer, onConnect, configError }) {
  const [server, setServer] = useState(initialServer);
  const [code, setCode] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  async function submit(event) {
    event.preventDefault(); setError(''); setBusy(true);
    try { await onConnect(normalizeServer(server), code); } catch (error) { setError(error.message); } finally { setBusy(false); }
  }
  return <main className="login-page"><div className="login-story"><a className="brand" href="/"><span className="brand-mark">nf<span>.</span></span><span>Naihe Feng<span className="brand-subtitle">Research workspace</span></span></a><div className="story-copy"><p className="eyebrow">EXPERIMENTS / RESEARCH IN PROGRESS</p><h1>Make every<br/>experiment<br/><em>tell a story.</em></h1><p>A focused workspace for research projects, experiment results, and the metrics that matter.</p><div className="story-features"><span><Icon name="folder" size={17}/> Organize projects</span><span><Icon name="chart" size={17}/> Compare results</span></div></div><div className="story-footer"><span className="status-dot"/> Research, measured.</div></div><div className="login-main"><div className="login-box"><div className="login-symbol"><Icon name="lock" size={25}/></div><p className="eyebrow">PRIVATE WORKSPACE</p><h2>Welcome to Experiments</h2><p className="muted">Connect to your experiment server and enter an access code to open the workspace.</p><form onSubmit={submit}><Field label="Server URL" help="Your Flask API URL. This address is remembered on this device."><input type="url" placeholder="https://your-experiment-server.com" value={server} onChange={event => setServer(event.target.value)} required autoComplete="url"/></Field><Field label="Access code" help="Your session stays in this browser tab."><input type="password" placeholder="Enter your access code" value={code} onChange={event => setCode(event.target.value)} required autoComplete="current-password"/></Field><Notice>{error}</Notice><button className="button primary full" disabled={busy} type="submit">{busy ? 'Connecting…' : 'Open workspace'}<Icon name="arrow" size={18}/></button></form>{!initialServer && <div className="setup-note"><Icon name="server" size={19}/><p><strong>Server setup needed</strong>This page is hosted on GitHub Pages. Deploy the included Flask server, then enter its HTTPS address above.</p></div>}{configError && <p className="small muted">Server configuration could not be loaded. You can enter the address manually.</p>}<a href="/" className="back-home">← Back to personal website</a></div><p className="login-bottom">A simple place to keep your research in view.</p></div></main>;
}

function CreateDialog({ kind, api, project, onClose, onDone }) {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function submit(event) {
    event.preventDefault(); setBusy(true); setError('');
    try {
      const result = await api(kind === 'project' ? '/projects' : `/projects/${encodeURIComponent(project.id)}/experiments`, { method: 'POST', body: { name: name.trim(), description: description.trim() } });
      onDone(result); onClose();
    } catch (error) { setError(error.message); } finally { setBusy(false); }
  }
  return <Modal title={`New ${kind}`} subtitle={kind === 'project' ? 'Give a research direction a home.' : `Add an experiment to ${project.name}.`} onClose={onClose}><form onSubmit={submit}><Field label={`${pretty(kind)} name`}><input value={name} onChange={event => setName(event.target.value)} maxLength={200} placeholder={kind === 'project' ? 'e.g. Document retrieval' : 'e.g. Teacher recall'} required/></Field><Field label="Description (optional)"><textarea value={description} onChange={event => setDescription(event.target.value)} placeholder="What are you exploring?" rows="3" maxLength={4000}/></Field><Notice>{error}</Notice><div className="modal-actions"><button type="button" className="button secondary" onClick={onClose}>Cancel</button><button className="button primary" disabled={busy || !name.trim()}>{busy ? 'Creating…' : `Create ${kind}`}</button></div></form></Modal>;
}

function MetricsDialog({ metrics, api, onClose, onDone }) {
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({ id: '', name: '', description: '', x_key: 'x' });
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const change = key => event => setForm({ ...form, [key]: event.target.value });
  async function submit(event) {
    event.preventDefault(); setError(''); setBusy(true);
    try { await api('/metrics', { method: 'POST', body: form }); await onDone(); setCreating(false); setForm({ id: '', name: '', description: '', x_key: 'x' }); } catch (error) { setError(error.message); } finally { setBusy(false); }
  }
  return <Modal title="Metric library" subtitle="Consistent definitions across every experiment." onClose={onClose}><div className="metric-list">{metrics.map(metric => <article className="metric-entry" key={metric.id}><div><strong>{metric.name}</strong><span className={`tag ${metric.builtin ? '' : 'custom'}`}>{metric.builtin ? 'Built in' : 'Custom'}</span></div><p>{metric.description || 'No description yet.'}</p><code>{metric.id}</code><span className="small muted"> · x-axis: {metric.x_key}</span></article>)}</div>{creating ? <form onSubmit={submit} className="metric-form"><h3>Define a custom metric</h3><div className="form-grid"><Field label="Display name"><input value={form.name} onChange={change('name')} required maxLength={200} placeholder="e.g. Semantic similarity"/></Field><Field label="Metric ID" help="Lowercase letters, numbers, and underscores."><input value={form.id} onChange={change('id')} required pattern="[a-z][a-z0-9_]*" maxLength={64} placeholder="semantic_similarity"/></Field></div><Field label="Description"><textarea value={form.description} onChange={change('description')} required rows="2" maxLength={4000} placeholder="What does this metric measure?"/></Field><Field label="X-axis data key" help="The key holding x values in each data row. All other numeric fields become plotted series."><input value={form.x_key} onChange={change('x_key')} required pattern="[a-z][a-z0-9_]*" maxLength={64}/></Field><Notice>{error}</Notice><div className="modal-actions"><button type="button" className="button secondary" onClick={() => setCreating(false)}>Cancel</button><button className="button primary" disabled={busy}>{busy ? 'Saving…' : 'Save metric'}</button></div></form> : <div className="modal-actions"><button className="button primary" onClick={() => setCreating(true)}><Icon name="plus" size={17}/>Define metric</button></div>}</Modal>;
}

function UploadDialog({ api, experiment, metrics, existing, onClose, onDone }) {
  const config = existing?.plot || {};
  const [mode, setMode] = useState('plot');
  const [metric, setMetric] = useState(existing?.metric_id || 'ndcg_at_k');
  const [dataText, setDataText] = useState('');
  const [form, setForm] = useState({ type: config.type || 'bar', title: config.title || '', description: config.description || '', x_label: config.x_label || '', y_label: config.y_label || '', y_min: config.y_min ?? '', y_max: config.y_max ?? '' });
  const [advanced, setAdvanced] = useState(JSON.stringify({ ...(config.series_labels ? { series_labels: config.series_labels } : {}), ...(config.colors ? { colors: config.colors } : {}) }, null, 2));
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const chosenMetric = metrics.find(item => item.id === metric);
  const change = key => event => setForm({ ...form, [key]: event.target.value });
  async function readFile(event) {
    const file = event.target.files?.[0];
    if (!file) return;
    if (file.size > 4 * 1024 * 1024) { setError('Choose a JSON file smaller than 4 MB.'); return; }
    try { setDataText(await file.text()); setError(''); } catch { setError('Could not read this file. Try pasting the JSON instead.'); }
  }
  async function submit(event) {
    event.preventDefault(); setError(''); setBusy(true);
    try {
      if (mode === 'import' && !existing) {
        const result = await api('/import', { method: 'POST', body: JSON.parse(dataText) });
        onDone(result); onClose(); return;
      }
      if (existing) {
        await savePlotOptions(api, existing.id, { form, advanced, metric: chosenMetric, data: existing.data });
      } else {
        const parsed = JSON.parse(dataText);
        let data = parsed;
        let finalMetric = metric;
        let requestPlot = {};
        if (!Array.isArray(parsed)) {
          if (!parsed || typeof parsed !== 'object') throw new Error('Data must be a JSON array of rows or a plot request object.');
          finalMetric = parsed.metric || metric;
          data = parsed.data ?? parsed[finalMetric];
          // Explicit settings in an uploaded request are preserved; the preview explains this precedence.
          requestPlot = parsed.plot || {};
        }
        if (!Array.isArray(data) || data.length === 0) throw new Error('Provide a nonempty array of data rows, or an object with a data array.');
        const finalPlot = buildPlotOptions({ form, advanced, metric: metrics.find(item => item.id === finalMetric), data, requestPlot });
        await api(`/experiments/${encodeURIComponent(experiment.id)}/plots`, { method: 'POST', body: { metric: finalMetric, data, plot: finalPlot } });
      }
      onDone(); onClose();
    } catch (error) { setError(error instanceof SyntaxError ? `Invalid JSON: ${error.message}` : error.message); } finally { setBusy(false); }
  }
  return <Modal title={existing ? 'Edit plot details' : 'Add experiment data'} subtitle={existing ? 'Give the result the context it needs.' : `Upload results to ${experiment.name}.`} onClose={onClose} wide><form onSubmit={submit}>{!existing && <><div className="tabs"><button className={mode === 'plot' ? 'active' : ''} type="button" onClick={() => setMode('plot')}>Add plot</button><button className={mode === 'import' ? 'active' : ''} type="button" onClick={() => setMode('import')}>Import complete request</button></div>{mode === 'import' ? <p className="help-panel">Paste the complete API request containing <code>project</code>, <code>experiment</code>, and <code>metrics</code>. The import creates or reuses its named project and experiment.</p> : <Field label="Metric"><select value={metric} onChange={event => setMetric(event.target.value)}>{metrics.map(item => <option key={item.id} value={item.id}>{item.name} ({item.id})</option>)}</select><span className="field-help">{chosenMetric?.description}</span></Field>}<Field label={mode === 'import' ? 'Import request (JSON)' : 'Result data (JSON)'} help={mode === 'plot' ? `Each row needs ${chosenMetric?.x_key || 'x'} and at least one numeric series. Every numeric series is plotted automatically. A full plot request can include metric, data, and plot; its options override the fields below.` : 'All changes in a complete request are saved together.'}><textarea className="code-input" value={dataText} onChange={event => setDataText(event.target.value)} rows="7" required placeholder={mode === 'import' ? '{"project":{"name":"My project"},"experiment":{"name":"Trial 1"},"metrics":[{"metric":"ndcg_at_k","data":[{"k":1,"model_ndcg":0.7}],"plot":{"type":"bar"}}]}' : `[{"${chosenMetric?.x_key || 'k'}": 1, "model_a": 0.7, "model_b": 0.8}]`}/></Field><label className="file-upload"><Icon name="upload" size={17}/><span>Choose a JSON file</span><input type="file" accept=".json,application/json" onChange={readFile}/></label></>}{(mode === 'plot' || existing) && <><div className="form-divider"><span>Plot options</span><span>Optional</span></div><div className="form-grid"><Field label="Chart type"><select value={form.type} onChange={change('type')}><option value="bar">Grouped bar chart</option><option value="line">Line chart</option></select></Field><Field label="Plot title"><input value={form.title} onChange={change('title')} placeholder={chosenMetric?.name || 'Metric comparison'} maxLength={200}/></Field></div><Field label="Description"><textarea value={form.description} onChange={change('description')} rows="2" placeholder="Add context, observations, or how this result was measured." maxLength={4000}/></Field><div className="form-grid"><Field label="X-axis description"><input value={form.x_label} onChange={change('x_label')} placeholder={chosenMetric?.x_key === 'k' ? 'Retrieval cutoff (k)' : chosenMetric?.x_key || 'X'} maxLength={200}/></Field><Field label="Y-axis description"><input value={form.y_label} onChange={change('y_label')} placeholder="Score" maxLength={200}/></Field><Field label="Y-axis minimum"><input type="number" step="any" value={form.y_min} onChange={change('y_min')} placeholder="Automatic"/></Field><Field label="Y-axis maximum"><input type="number" step="any" value={form.y_max} onChange={change('y_max')} placeholder="Automatic"/></Field></div><details className="advanced"><summary>Series labels, colors, and advanced options</summary><p className="field-help">JSON values override matching fields above. Removing a label or color removes its saved override. Use <code>{'{}'}</code> to clear all labels and colors while keeping the form values. JSON is checked when you save.</p><p className="field-help">For example: <code>{'{"series_labels":{"model_a":"Model A"},"colors":{"model_a":"#4b69df"}}'}</code></p><textarea aria-label="Advanced plot options JSON" className="code-input" rows="4" value={advanced} onChange={event => setAdvanced(event.target.value)}/></details></>}<Notice>{error}</Notice><div className="modal-actions"><button type="button" className="button secondary" onClick={onClose}>Cancel</button><button className="button primary" disabled={busy}><Icon name={existing ? 'check' : 'upload'} size={17}/>{busy ? 'Saving…' : existing ? 'Save details' : mode === 'import' ? 'Import request' : 'Save plot'}</button></div></form></Modal>;
}

function Chart({ record, metric }) {
  const config = record.plot || {};
  const [type, setType] = useState(config.type || 'bar');
  const [hidden, setHidden] = useState(new Set());
  const svgRef = useRef(null);
  useEffect(() => setType(config.type || 'bar'), [config.type]);
  const sourceRows = Array.isArray(record.data) ? record.data : [];
  const xKey = config.x_key || metric?.x_key || (record.metric_id === 'ndcg_at_k' ? 'k' : 'x');
  const numericX = sourceRows.every(row => typeof row[xKey] === 'number' && Number.isFinite(row[xKey]));
  const rows = numericX ? [...sourceRows].sort((a, b) => a[xKey] - b[xKey]) : sourceRows;
  const series = [...new Set(rows.flatMap(row => Object.keys(row).filter(key => key !== xKey && typeof row[key] === 'number' && Number.isFinite(row[key]))))];
  const visible = series.filter(key => !hidden.has(key));
  const values = rows.flatMap(row => visible.map(key => row[key]).filter(value => typeof value === 'number' && Number.isFinite(value)));
  const minValue = values.length ? values.reduce((min, value) => Math.min(min, value), Infinity) : 0;
  const maxValue = values.length ? values.reduce((max, value) => Math.max(max, value), -Infinity) : 1;
  const fixedMin = typeof config.y_min === 'number' && Number.isFinite(config.y_min);
  const fixedMax = typeof config.y_max === 'number' && Number.isFinite(config.y_max);
  let minimum = fixedMin ? config.y_min : Math.min(0, minValue);
  const paddedMax = Number.isFinite(maxValue * 1.1) ? maxValue * 1.1 : maxValue;
  let maximum = fixedMax ? config.y_max : metric?.builtin && maxValue <= 1 && maxValue > 0 ? 1 : maxValue > 0 ? paddedMax : 0;
  if (maximum <= minimum) {
    if (fixedMax && !fixedMin) minimum = maximum - Math.max(1, Math.abs(maximum) * 0.1);
    else maximum = minimum + Math.max(1, Math.abs(minimum) * 0.1);
  }
  const yScale = Math.max(Math.abs(minimum), Math.abs(maximum)) || 1;
  const width = Math.max(700, Math.min(12000, rows.length * (type === 'bar' ? Math.max(55, visible.length * 16) : 40)));
  const height = 330;
  const margin = { left: 72, right: 24, top: 24, bottom: 64 };
  const chartW = width - margin.left - margin.right;
  const chartH = height - margin.top - margin.bottom;
  const slot = chartW / Math.max(1, rows.length);
  const xMin = numericX && rows.length ? rows[0][xKey] : 0;
  const xMax = numericX && rows.length ? rows.at(-1)[xKey] : 0;
  const x = index => type === 'line' && numericX && xMax > xMin ? margin.left + 16 + (chartW - 32) * (rows[index][xKey] - xMin) / (xMax - xMin) : margin.left + slot * (index + 0.5);
  const y = value => margin.top + chartH * (1 - (value / yScale - minimum / yScale) / (maximum / yScale - minimum / yScale));
  const baseline = Math.max(minimum, Math.min(maximum, 0));
  const color = key => {
    const candidate = config.colors && Object.hasOwn(config.colors, key) ? config.colors[key] : null;
    return typeof candidate === 'string' && /^(#[0-9a-f]{3,8}|[a-z]{1,30}|rgba?\([0-9.,% ]+\)|hsla?\([0-9.,% ]+\))$/i.test(candidate) ? candidate : COLORS[series.indexOf(key) % COLORS.length];
  };
  const label = key => config.series_labels && Object.hasOwn(config.series_labels, key) ? config.series_labels[key] : pretty(key);
  const title = config.title || metric?.name || pretty(record.metric_id || 'Results');
  const clipId = `plot-${String(record.id).replace(/[^a-zA-Z0-9_-]/g, '')}`;
  const ticks = Array.from({ length: 6 }, (_, i) => minimum * (1 - i / 5) + maximum * i / 5);
  const outside = values.some(value => value < minimum || value > maximum);
  function toggleSeries(key) { setHidden(previous => { const next = new Set(previous); next.has(key) ? next.delete(key) : next.add(key); return next; }); }
  function download() {
    const clone = svgRef.current.cloneNode(true);
    const namespace = 'http://www.w3.org/2000/svg';
    const columns = Math.max(1, Math.floor(chartW / 210));
    const headerHeight = 65 + Math.ceil(visible.length / columns) * 21;
    const drawing = document.createElementNS(namespace, 'g');
    drawing.setAttribute('transform', `translate(0 ${headerHeight})`);
    while (clone.firstChild) drawing.appendChild(clone.firstChild);
    clone.removeAttribute('style');
    clone.setAttribute('viewBox', `0 0 ${width} ${height + headerHeight}`);
    clone.setAttribute('width', String(width));
    clone.setAttribute('height', String(height + headerHeight));
    const background = document.createElementNS(namespace, 'rect');
    background.setAttribute('width', '100%'); background.setAttribute('height', '100%'); background.setAttribute('fill', '#ffffff');
    clone.appendChild(background);
    const heading = document.createElementNS(namespace, 'text');
    heading.setAttribute('x', String(margin.left)); heading.setAttribute('y', '31'); heading.setAttribute('fill', '#24334a'); heading.setAttribute('font-size', '17'); heading.setAttribute('font-weight', '600');
    heading.textContent = title.length > 80 ? `${title.slice(0, 77)}…` : title;
    clone.appendChild(heading);
    visible.forEach((key, index) => {
      const itemX = margin.left + (index % columns) * (chartW / columns);
      const itemY = 57 + Math.floor(index / columns) * 21;
      const swatch = document.createElementNS(namespace, 'rect');
      swatch.setAttribute('x', String(itemX)); swatch.setAttribute('y', String(itemY - 8)); swatch.setAttribute('width', '8'); swatch.setAttribute('height', '8'); swatch.setAttribute('rx', '2'); swatch.setAttribute('fill', color(key));
      const text = document.createElementNS(namespace, 'text');
      text.setAttribute('x', String(itemX + 14)); text.setAttribute('y', String(itemY)); text.setAttribute('fill', '#607087'); text.setAttribute('font-size', '11');
      const name = label(key); text.textContent = name.length > 29 ? `${name.slice(0, 26)}…` : name;
      const fullName = document.createElementNS(namespace, 'title'); fullName.textContent = name; text.appendChild(fullName);
      clone.appendChild(swatch); clone.appendChild(text);
    });
    clone.appendChild(drawing);
    const xml = new XMLSerializer().serializeToString(clone);
    const blob = new Blob([xml], { type: 'image/svg+xml;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href = url; a.download = `${title.replace(/[^a-z0-9_-]+/gi, '-').toLowerCase() || 'experiment'}.svg`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  if (!rows.length || !series.length) return <div className="notice">No numeric result series are available for this plot.</div>;
  return <><div className="chart-toolbar"><div className="legend">{series.map(key => <button key={key} type="button" className={hidden.has(key) ? 'hidden-series' : ''} onClick={() => toggleSeries(key)} aria-pressed={!hidden.has(key)} title={`Toggle ${label(key)}`}><span style={{ backgroundColor: color(key) }}/>{label(key)}</button>)}</div><div className="chart-actions"><div className="segmented" aria-label="Chart type"><button aria-label="Bar chart" aria-pressed={type === 'bar'} className={type === 'bar' ? 'active' : ''} onClick={() => setType('bar')}><Icon name="bars" size={17}/></button><button aria-label="Line chart" aria-pressed={type === 'line'} className={type === 'line' ? 'active' : ''} onClick={() => setType('line')}><Icon name="chart" size={17}/></button></div><button className="icon-button" title="Download chart as SVG" aria-label="Download chart as SVG" onClick={download}><Icon name="download" size={18}/></button></div></div><div className="chart-scroll"><svg xmlns="http://www.w3.org/2000/svg" ref={svgRef} className="data-chart" style={{ minWidth: Math.min(width, 700), width: rows.length > 12 ? width : '100%' }} viewBox={`0 0 ${width} ${height}`} role="img" aria-labelledby={`${clipId}-title ${clipId}-description`} fontFamily="Arial, sans-serif"><title id={`${clipId}-title`}>{title}</title><desc id={`${clipId}-description`}>{config.description || `${type === 'bar' ? 'Grouped bar' : 'Line'} chart comparing ${series.map(label).join(', ')}. Exact values are available in the data table below.`}</desc><rect width={width} height={height} fill="#ffffff"/><defs><clipPath id={clipId}><rect x={margin.left} y={margin.top} width={chartW} height={chartH}/></clipPath></defs>{ticks.map((value, i) => <g key={i}><line x1={margin.left} x2={width - margin.right} y1={y(value)} y2={y(value)} stroke="#e8edf4" strokeDasharray={i ? '3 4' : '0'}/><text x={margin.left - 14} y={y(value) + 4} textAnchor="end" fill="#8190a5" fontSize="11">{formatNumber(value)}</text></g>)}<text x="15" y={margin.top + chartH / 2} textAnchor="middle" transform={`rotate(-90, 15, ${margin.top + chartH / 2})`} fill="#607087" fontSize="11">{config.y_label || metric?.name || 'Score'}</text><g clipPath={`url(#${clipId})`}>{type === 'bar' ? rows.map((row, index) => {
      const groupWidth = Math.min(slot * 0.65, visible.length * 46);
      const barWidth = groupWidth / Math.max(1, visible.length);
      return visible.map((key, seriesIndex) => typeof row[key] === 'number' && Number.isFinite(row[key]) ? <rect key={`${index}-${key}`} x={x(index) - groupWidth / 2 + seriesIndex * barWidth + 2} y={Math.min(y(baseline), y(row[key]))} width={Math.max(1, barWidth - 4)} height={Math.max(0, Math.abs(y(row[key]) - y(baseline)))} fill={color(key)} rx="3"><title>{`${xKey}=${row[xKey]} · ${label(key)}: ${row[key]}`}</title></rect> : null);
    }) : visible.map(key => {
      let penDown = false;
      const path = rows.map((row, index) => { if (typeof row[key] !== 'number' || !Number.isFinite(row[key])) { penDown = false; return ''; } const command = penDown ? 'L' : 'M'; penDown = true; return `${command}${x(index)},${y(row[key])}`; }).join(' ');
      return <g key={key}><path d={path} fill="none" stroke={color(key)} strokeWidth="2.5" strokeLinejoin="round"/>{rows.map((row, index) => typeof row[key] === 'number' && Number.isFinite(row[key]) ? <circle key={index} cx={x(index)} cy={y(row[key])} r="4" fill="#fff" stroke={color(key)} strokeWidth="2.5"><title>{`${xKey}=${row[xKey]} · ${label(key)}: ${row[key]}`}</title></circle> : null)}</g>;
    })}</g>{rows.map((row, index) => <text key={index} x={x(index)} y={height - 39} textAnchor="middle" fill="#607087" fontSize="11">{String(row[xKey] ?? '—').slice(0, 30)}</text>)}<text x={margin.left + chartW / 2} y={height - 10} textAnchor="middle" fill="#607087" fontSize="11">{config.x_label || pretty(xKey)}</text></svg></div>{outside && <p className="chart-note">Some values fall outside the configured y-axis range. Open the data table to view their full values.</p>}<details className="data-details"><summary>View data <span>{rows.length} {rows.length === 1 ? 'point' : 'points'} · {series.length} {series.length === 1 ? 'series' : 'series'}</span></summary><div className="table-scroll"><table><caption className="sr-only">Exact data for {title}</caption><thead><tr><th scope="col">{config.x_label || xKey}</th>{series.map(key => <th scope="col" key={key}>{label(key)}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={index}><th scope="row">{String(row[xKey] ?? '—')}</th>{series.map(key => <td key={key}>{typeof row[key] === 'number' ? String(row[key]) : '—'}</td>)}</tr>)}</tbody></table></div></details></>;
}

function PlotCard({ record, metric, onEdit }) { return <article className="plot-card"><div className="plot-heading"><div><p className="eyebrow">{metric?.name || pretty(record.metric_id || 'Metric')}</p><h3>{record.plot?.title || metric?.name || 'Metric comparison'}</h3>{record.plot?.description && <p className="plot-description">{record.plot.description}</p>}</div><button className="icon-button" onClick={onEdit} aria-label="Edit plot details" title="Edit plot details"><Icon name="edit" size={18}/></button></div><Chart record={record} metric={metric}/></article>; }

function Empty({ title, description, action, onAction, icon = 'chart' }) { return <div className="empty-state"><div className="empty-icon"><Icon name={icon} size={29}/></div><h3>{title}</h3><p>{description}</p>{action && <button className="button primary" onClick={onAction}><Icon name="plus" size={17}/>{action}</button>}</div>; }

function Workspace({ server, token, onLogout }) {
  const route = useRoute();
  const projectId = route[0] === 'projects' ? route[1] : null;
  const experimentId = route[2] === 'experiments' ? route[3] : null;
  const [projects, setProjects] = useState([]);
  const [metrics, setMetrics] = useState([]);
  const [project, setProject] = useState(null);
  const [experiment, setExperiment] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [dialog, setDialog] = useState(null);
  const [revision, setRevision] = useState(0);
  const [search, setSearch] = useState('');
  const [toast, setToast] = useState('');
  async function api(path, options = {}) {
    const controller = new AbortController();
    const cancel = () => controller.abort();
    options.signal?.addEventListener('abort', cancel, { once: true });
    if (options.signal?.aborted) controller.abort();
    const timeout = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch(`${server}/api${path}`, { ...options, signal: controller.signal, headers: { Authorization: `Bearer ${token}`, ...(options.body ? { 'Content-Type': 'application/json' } : {}) }, ...(options.body ? { body: JSON.stringify(options.body) } : {}) });
      const body = await response.json().catch(() => ({}));
      if (options.signal?.aborted) throw new DOMException('Request cancelled.', 'AbortError');
      if (response.status === 401) { onLogout('Your session has expired. Please enter your access code again.'); throw new Error('Your session has expired.'); }
      if (!response.ok) throw new Error(typeof body.error === 'string' ? body.error : body.error?.message || body.message || `The server returned ${response.status}.`);
      return body;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('The server took too long to respond. Please try again.');
      if (error instanceof TypeError) throw new Error('Cannot reach the experiment server. Check the server address, connection, and allowed website origin (CORS).');
      throw error;
    } finally { clearTimeout(timeout); options.signal?.removeEventListener('abort', cancel); }
  }
  useEffect(() => {
    let active = true; setLoading(true); setError(''); setSearch('');
    const controller = new AbortController();
    const options = { signal: controller.signal };
    async function loadExperiment() {
      let cursor = '0';
      let result;
      const plots = [];
      const visited = new Set();
      do {
        if (!active) return null;
        if (visited.has(cursor)) throw new Error('The server returned a repeated page cursor. Please retry.');
        visited.add(cursor);
        result = await api(`/experiments/${encodeURIComponent(experimentId)}?limit=1&cursor=${encodeURIComponent(cursor)}`, options);
        if (!active) return null;
        plots.push(...(result.plots || []));
        cursor = result.next_cursor;
      } while (cursor != null);
      return { ...result, plots, next_cursor: null };
    }
    Promise.all([api('/projects', options), api('/metrics', options), projectId ? api(`/projects/${encodeURIComponent(projectId)}`, options) : null, experimentId ? loadExperiment() : null]).then(([allProjects, allMetrics, selectedProject, selectedExperiment]) => {
      if (!active) return;
      setProjects(allProjects.projects || []); setMetrics(allMetrics.metrics || []); setProject(selectedProject); setExperiment(selectedExperiment);
      if (selectedExperiment && selectedExperiment.project_id !== projectId) setError('This experiment does not belong to the selected project. Return to Projects and open it again.');
    }).catch(error => { if (active) setError(error.message); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; controller.abort(); };
  }, [projectId, experimentId, revision]);
  useEffect(() => { if (toast) { const timer = setTimeout(() => setToast(''), 4500); return () => clearTimeout(timer); } }, [toast]);
  function refresh(message = 'Saved to your workspace.') { setRevision(value => value + 1); setToast(message); }
  function importDone(result) { refresh(); if (result?.project_id && result?.experiment_id) window.location.hash = experimentHref(result.project_id, result.experiment_id); }
  const visibleProjects = projects.filter(item => `${item.name} ${item.description || ''}`.toLowerCase().includes(search.toLowerCase()));
  const visibleExperiments = (project?.experiments || []).filter(item => `${item.name} ${item.description || ''}`.toLowerCase().includes(search.toLowerCase()));
  const totalExperiments = projects.reduce((sum, item) => sum + (item.experiment_count || 0), 0);
  return <div className="workspace"><aside className="sidebar"><a className="brand" href="/"><span className="brand-mark">nf<span>.</span></span><span>Naihe Feng<span className="brand-subtitle">Research workspace</span></span></a><p className="nav-label">WORKSPACE</p><nav className="side-nav" aria-label="Workspace"><a href="#/" className="selected"><Icon name="grid"/>Projects<span>{projects.length}</span></a><button onClick={() => setDialog({ kind: 'metrics' })}><Icon name="book"/>Metric library</button></nav>{projectId && project && <div className="sidebar-project"><p className="nav-label">CURRENT PROJECT</p><a href={projectHref(project.id)}><Icon name="folder" size={16}/><span>{project.name}</span></a>{project.experiments?.slice(0, 8).map(item => <a className={`experiment-nav ${item.id === experimentId ? 'current' : ''}`} key={item.id} href={experimentHref(project.id, item.id)}><span className="nav-dot"/>{item.name}</a>)}</div>}<div className="sidebar-bottom"><div className="connection"><span className="status-dot"/><div><strong>Connected workspace</strong><span title={server}>{new URL(server).hostname}</span></div><Icon name="lock" size={15}/></div><div className="sidebar-links"><a href="/">Personal website ↗</a><button onClick={() => onLogout()}><Icon name="logout" size={16}/>Sign out</button></div></div></aside><div className="workspace-main"><header className="topbar"><div className="breadcrumbs"><a href="#/">Experiments</a>{projectId && <><Icon name="chevron" size={13}/><a href={projectHref(projectId)}>{project?.name || 'Project'}</a></>}{experimentId && <><Icon name="chevron" size={13}/><span>{experiment?.name || 'Experiment'}</span></>}</div><div className="access-badge"><span className="status-dot"/>Root access</div></header><main className="content"><div className="page-heading"><div><p className="eyebrow">{experimentId ? 'EXPERIMENT RESULTS' : projectId ? 'PROJECT OVERVIEW' : 'YOUR RESEARCH, ORGANIZED'}</p><h1>{experimentId ? experiment?.name || 'Experiment' : projectId ? project?.name || 'Project' : 'Projects'}</h1><p>{experimentId ? experiment?.description || 'Explore results, compare methods, and keep the details in view.' : projectId ? project?.description || 'A collection of experiments working toward one research question.' : 'A clear view of your experiments, from first run to final result.'}</p></div><button className="button primary" disabled={loading || !!error} onClick={() => setDialog({ kind: experimentId ? 'upload' : projectId ? 'experiment' : 'project' })}><Icon name={experimentId ? 'upload' : 'plus'} size={18}/>{experimentId ? 'Add data' : projectId ? 'New experiment' : 'New project'}</button></div>{error && <div className="error-row"><Notice>{error}</Notice><button className="button secondary" onClick={() => setRevision(value => value + 1)}>Retry</button></div>}{loading ? <div className="loading-state" role="status"><span className="spinner"/>Loading your workspace…</div> : !error && <>{!projectId ? <><div className="overview-grid"><div className="stat-card"><div className="stat-icon lavender"><Icon name="folder"/></div><span>Projects</span><strong>{projects.length.toString().padStart(2, '0')}</strong></div><div className="stat-card"><div className="stat-icon teal"><Icon name="chart"/></div><span>Experiments</span><strong>{totalExperiments.toString().padStart(2, '0')}</strong></div><button className="stat-card metric-stat" onClick={() => setDialog({ kind: 'metrics' })}><div className="stat-icon amber"><Icon name="book"/></div><span>Defined metrics</span><strong>{metrics.length.toString().padStart(2, '0')}</strong><Icon name="arrow" size={18}/></button></div><div className="section-heading"><h2>All projects <span>{projects.length}</span></h2><label className="search"><Icon name="search" size={17}/><input aria-label="Search projects" value={search} onChange={event => setSearch(event.target.value)} placeholder="Find a project…"/></label></div>{projects.length === 0 ? <Empty title="A new idea starts here" description="Create your first project, then add experiments and their results." action="Create project" icon="folder" onAction={() => setDialog({ kind: 'project' })}/> : visibleProjects.length === 0 ? <Empty title="No matching projects" description="Try a different project name." icon="search"/> : <div className="project-grid">{visibleProjects.map((item, index) => <a key={item.id} href={projectHref(item.id)} className="project-card"><div className="project-top"><span className={`project-icon color-${index % 3}`}><Icon name="folder" size={22}/></span><span className="tag">Research project</span></div><h3>{item.name}</h3><p>{item.description || 'Explore the experiments and results in this project.'}</p><div className="project-card-footer"><span><Icon name="chart" size={15}/>{item.experiment_count || 0} {(item.experiment_count || 0) === 1 ? 'experiment' : 'experiments'}</span><Icon name="arrow" size={19}/></div></a>)}</div>}</> : !experimentId ? <><div className="section-heading"><h2>Experiments <span>{project?.experiments?.length || 0}</span></h2><label className="search"><Icon name="search" size={17}/><input aria-label="Search experiments" value={search} onChange={event => setSearch(event.target.value)} placeholder="Find an experiment…"/></label></div>{!project?.experiments?.length ? <Empty title="Ready for your first experiment" description="Name an experiment to start collecting metrics, plots, and observations." action="New experiment" onAction={() => setDialog({ kind: 'experiment' })}/> : !visibleExperiments.length ? <Empty title="No matching experiments" description="Try another search." icon="search"/> : <div className="experiment-list">{visibleExperiments.map((item, index) => <a key={item.id} className="experiment-row" href={experimentHref(project.id, item.id)}><div className="experiment-number">{String(index + 1).padStart(2, '0')}</div><div className="experiment-info"><h3>{item.name}</h3><p>{item.description || 'View metric comparisons and recorded results.'}</p></div><span className="tag">{item.plot_count || 0} {item.plot_count === 1 ? 'plot' : 'plots'}</span><Icon name="arrow" size={20}/></a>)}</div>}</> : <><div className="results-context"><div><Icon name="chart" size={17}/><strong>{experiment?.plots?.length || 0} {(experiment?.plots?.length || 0) === 1 ? 'plot' : 'plots'}</strong><span> · </span><span>Switch views or explore exact values below</span></div><button className="text-button" onClick={() => setDialog({ kind: 'metrics' })}>Metric definitions<Icon name="arrow" size={14}/></button></div>{!experiment?.plots?.length ? <Empty title="Let the results speak" description="Paste or upload JSON data to create a line or bar chart. All numeric result series are picked up automatically." action="Add experiment data" onAction={() => setDialog({ kind: 'upload' })}/> : <div className="plot-grid">{experiment.plots.map(record => <PlotCard key={record.id} record={record} metric={metrics.find(item => item.id === record.metric_id)} onEdit={() => setDialog({ kind: 'upload', existing: record })}/>)}</div>}</>}</>}<footer className="content-footer"><span>Naihe Feng · Experiment workspace</span><span>Projects / Experiments / Insights</span></footer></main></div>{toast && <div className="toast" role="status"><Icon name="check" size={18}/>{toast}</div>}{dialog?.kind === 'project' && <CreateDialog kind="project" api={api} onClose={() => setDialog(null)} onDone={result => { refresh('Project created.'); if (result.id) window.location.hash = projectHref(result.id); }}/>}{dialog?.kind === 'experiment' && project && <CreateDialog kind="experiment" api={api} project={project} onClose={() => setDialog(null)} onDone={result => { refresh('Experiment created.'); if (result.id) window.location.hash = experimentHref(project.id, result.id); }}/>}{dialog?.kind === 'metrics' && <MetricsDialog metrics={metrics} api={api} onClose={() => setDialog(null)} onDone={async () => { const data = await api('/metrics'); setMetrics(data.metrics || []); setToast('Metric definition saved.'); }}/>}{dialog?.kind === 'upload' && experiment && <UploadDialog api={api} experiment={experiment} metrics={metrics} existing={dialog.existing} onClose={() => setDialog(null)} onDone={importDone}/>}</div>;
}

function App() {
  const [ready, setReady] = useState(false);
  const [server, setServer] = useState('');
  const [token, setToken] = useState('');
  const [configError, setConfigError] = useState(false);
  const [sessionNotice, setSessionNotice] = useState('');
  useEffect(() => {
    async function initialize() {
      let configured = '';
      try { const response = await fetch('./config.json', { cache: 'no-store' }); if (!response.ok) throw new Error(); const config = await response.json(); configured = config.apiBaseUrl || ''; } catch { setConfigError(true); }
      const selected = readStored(SERVER_KEY) || configured;
      if (selected) { try { const normalized = normalizeServer(selected); setServer(normalized); const stored = JSON.parse(readStored(SESSION_KEY, 'sessionStorage') || '{}'); if (stored.server === normalized && typeof stored.token === 'string') setToken(stored.token); } catch { /* An invalid saved address can be corrected on the login page. */ } }
      setReady(true);
    }
    initialize();
  }, []);
  async function connect(address, code) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch(`${address}/api/auth/session`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code }), signal: controller.signal });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof data.error === 'string' ? data.error : data.error?.message || data.message || 'The access code could not be verified.');
      if (!data.token) throw new Error('The server did not return a session token. Check that this is the experiment API URL.');
      try { localStorage.setItem(SERVER_KEY, address); sessionStorage.setItem(SESSION_KEY, JSON.stringify({ server: address, token: data.token })); } catch { /* The active session still works when browser storage is disabled. */ }
      setServer(address); setToken(data.token); setSessionNotice('');
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('The server took too long to respond. It may still be starting; please try again.');
      if (error instanceof TypeError) throw new Error('Cannot reach the server. Verify its HTTPS address and configure its allowed origin for this website.');
      throw error;
    } finally { clearTimeout(timeout); }
  }
  function logout(message = '') { try { sessionStorage.removeItem(SESSION_KEY); } catch {} setToken(''); setSessionNotice(message); }
  if (!ready) return <div className="initial-loading" role="status"><span className="spinner"/>Opening Experiments…</div>;
  return token ? <Workspace server={server} token={token} onLogout={logout}/> : <>{sessionNotice && <div className="session-notice" role="status">{sessionNotice}</div>}<Login initialServer={server} onConnect={connect} configError={configError}/></>;
}

createRoot(document.getElementById('root')).render(<App/>);
