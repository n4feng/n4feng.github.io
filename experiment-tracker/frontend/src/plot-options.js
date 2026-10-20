const FIELDS = new Set([
  'type', 'title', 'description', 'x_key', 'x_label', 'y_label',
  'y_min', 'y_max', 'series_labels', 'colors',
]);
const COLOR = /^(#[0-9a-fA-F]{3,8}|[a-zA-Z]{1,30}|rgba?\([0-9.,% ]+\)|hsla?\([0-9.,% ]+\))$/;

function object(value, label) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${label} must be a JSON object.`);
  }
  return value;
}

function text(value, label, maximum, required = false) {
  if (typeof value !== 'string') throw new Error(`${label} must be text.`);
  const cleaned = value.trim();
  if (required && !cleaned) throw new Error(`${label} must not be empty.`);
  if ([...cleaned].length > maximum) throw new Error(`${label} must be at most ${maximum} characters.`);
  return cleaned;
}

export function buildPlotOptions({ form, advanced, metric, data, requestPlot = {} }) {
  let extra;
  try {
    extra = JSON.parse(advanced);
  } catch (error) {
    throw new Error(`Invalid JSON: ${error.message} Use {} for no advanced options.`);
  }
  object(extra, 'Advanced plot options');
  object(requestPlot, 'Uploaded plot options');
  const unknown = [...new Set([...Object.keys(extra), ...Object.keys(requestPlot)])].filter(key => !FIELDS.has(key));
  if (unknown.length) throw new Error(`Unknown plot options: ${unknown.sort().join(', ')}.`);
  if (!metric?.x_key) throw new Error('The metric definition is unavailable. Reload the experiment and try again.');

  // Send every supported field so PATCH cannot retain deleted map overrides.
  // Explicit JSON settings take precedence over the visible form controls.
  const plot = {
    type: form.type,
    title: form.title,
    description: form.description,
    x_key: metric.x_key,
    x_label: form.x_label,
    y_label: form.y_label,
    y_min: form.y_min === '' ? null : Number(form.y_min),
    y_max: form.y_max === '' ? null : Number(form.y_max),
    series_labels: {},
    colors: {},
    ...extra,
    ...requestPlot,
  };
  if (!['bar', 'line'].includes(plot.type)) throw new Error("plot.type must be 'bar' or 'line'.");
  if (plot.x_key !== metric.x_key) throw new Error(`plot.x_key must match the metric's x-axis field ('${metric.x_key}').`);
  for (const key of ['title', 'description', 'x_label', 'y_label']) {
    plot[key] = text(plot[key], `plot.${key}`, key === 'description' ? 5000 : 300);
  }
  for (const key of ['y_min', 'y_max']) {
    if (plot[key] !== null && (typeof plot[key] !== 'number' || !Number.isFinite(plot[key]))) {
      throw new Error(`plot.${key} must be a finite number or null.`);
    }
  }
  if (plot.y_min !== null && plot.y_max !== null && plot.y_min >= plot.y_max) {
    throw new Error('The y-axis maximum must be larger than the minimum.');
  }
  const series = data ? new Set(data.flatMap(row => Object.keys(row).filter(key => key !== metric.x_key))) : null;
  for (const key of ['series_labels', 'colors']) {
    const mapping = object(plot[key], `plot.${key}`);
    plot[key] = Object.fromEntries(Object.entries(mapping).map(([name, value]) => {
      if (series && !series.has(name)) throw new Error(`plot.${key} contains an unknown series: ${name}.`);
      const label = text(value, `plot.${key}.${name}`, 100, true);
      if (key === 'colors' && !COLOR.test(label)) {
        throw new Error('Plot colors must be CSS color names, hex, rgb(), or hsl() values.');
      }
      return [name, label];
    }));
  }
  return plot;
}

export async function savePlotOptions(api, plotId, options) {
  const plot = buildPlotOptions(options);
  return api(`/plots/${encodeURIComponent(plotId)}`, { method: 'PATCH', body: { plot } });
}
