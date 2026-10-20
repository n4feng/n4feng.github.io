export function discoverSeries(rows, xKey) {
  return [...new Set(rows.flatMap(row => Object.keys(row).filter(key => (
    key !== xKey && typeof row[key] === 'number' && Number.isFinite(row[key])
  ))))];
}

export function orderSeries(detected, preferred = []) {
  const known = new Set(detected);
  return [...new Set([
    ...(Array.isArray(preferred) ? preferred.filter(key => known.has(key)) : []),
    ...detected,
  ])];
}

export function moveSeries(series, fromKey, toKey) {
  const result = [...series];
  const from = result.indexOf(fromKey);
  const to = result.indexOf(toKey);
  if (from < 0 || to < 0 || from === to) return result;
  result.splice(from, 1);
  result.splice(to, 0, fromKey);
  return result;
}

export function validateSeriesOrder(value, known = null) {
  if (!Array.isArray(value)) throw new Error('plot.series_order must be an array of series names.');
  const seen = new Set();
  for (const key of value) {
    if (typeof key !== 'string' || !key) throw new Error('plot.series_order must contain nonempty series names.');
    if (seen.has(key)) throw new Error(`plot.series_order contains a duplicate series: ${key}.`);
    if (known && !known.has(key)) throw new Error(`plot.series_order contains an unknown series: ${key}.`);
    seen.add(key);
  }
  return [...value];
}

const UPDATE_SERVER = 'Your server does not support saving column order yet. Deploy the updated Lambda backend, then try Save order again.';

export async function saveSeriesOrder(api, plotId, seriesOrder) {
  const order = validateSeriesOrder(seriesOrder);
  let record;
  try {
    record = await api(`/plots/${encodeURIComponent(plotId)}`, {
      method: 'PATCH', body: { plot: { series_order: order } },
    });
  } catch (error) {
    if (/unknown plot options.*series_order/i.test(error.message || '')) throw new Error(UPDATE_SERVER);
    throw error;
  }
  if (!Array.isArray(record?.plot?.series_order)) throw new Error(UPDATE_SERVER);
  if (record.plot.series_order.length !== order.length || record.plot.series_order.some((key, index) => key !== order[index])) {
    throw new Error('The server returned a different column order. Your order has not been confirmed; try saving again.');
  }
  return record;
}
