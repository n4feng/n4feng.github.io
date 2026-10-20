import test from 'node:test';
import assert from 'node:assert/strict';
import { discoverSeries, orderSeries, moveSeries, saveSeriesOrder } from '../src/series-order.js';

test('Discovery includes every numeric series in encounter order without changing data', () => {
  const rows = [
    { k: 3, cosine_ndcg: 0.7, base_ndcg: 0.8, note: 'trial', invalid: Infinity },
    { k: 1, weighted_ndcg: 0.6, base_ndcg: 0.5, invalid: NaN },
  ];
  const before = structuredClone(rows);
  assert.deepEqual(discoverSeries(rows, 'k'), ['cosine_ndcg', 'base_ndcg', 'weighted_ndcg']);
  assert.deepEqual(rows, before);
});

test('An explicit preference orders columns and appends all remaining series', () => {
  const detected = ['base_ndcg', 'cosine_ndcg', 'weighted_ndcg'];
  assert.deepEqual(orderSeries(detected, ['weighted_ndcg']), ['weighted_ndcg', 'base_ndcg', 'cosine_ndcg']);
  assert.deepEqual(orderSeries(detected, ['cosine_ndcg', 'base_ndcg', 'weighted_ndcg']), ['cosine_ndcg', 'base_ndcg', 'weighted_ndcg']);
  assert.deepEqual(orderSeries(detected, ['missing', 'weighted_ndcg', 'weighted_ndcg']), ['weighted_ndcg', 'base_ndcg', 'cosine_ndcg']);
  assert.deepEqual(orderSeries(detected), detected);
  assert.deepEqual(orderSeries(detected, []), detected);
  assert.deepEqual(orderSeries(detected, null), detected);
  assert.deepEqual(detected, ['base_ndcg', 'cosine_ndcg', 'weighted_ndcg']);
});

test('Moving a column in either direction places it at the target index', () => {
  const series = ['cosine_ndcg', 'base_ndcg', 'weighted_ndcg'];
  assert.deepEqual(moveSeries(series, 'cosine_ndcg', 'weighted_ndcg'), ['base_ndcg', 'weighted_ndcg', 'cosine_ndcg']);
  assert.deepEqual(moveSeries(series, 'weighted_ndcg', 'cosine_ndcg'), ['weighted_ndcg', 'cosine_ndcg', 'base_ndcg']);
  assert.deepEqual(moveSeries(series, 'base_ndcg', 'cosine_ndcg'), ['base_ndcg', 'cosine_ndcg', 'weighted_ndcg']);
  assert.deepEqual(moveSeries(series, 'missing', 'cosine_ndcg'), series);
  assert.deepEqual(moveSeries(series, 'base_ndcg', 'missing'), series);
  assert.deepEqual(moveSeries(series, 'base_ndcg', 'base_ndcg'), series);
  assert.deepEqual(series, ['cosine_ndcg', 'base_ndcg', 'weighted_ndcg']);
});

test('Saving order sends only the ordered array and returns the confirmed record', async () => {
  const order = ['cosine_ndcg', 'base_ndcg', 'weighted_ndcg'];
  const record = { id: 'plot / 1', plot: { title: 'Keep title', colors: { base_ndcg: 'blue' }, series_order: order }, data: [{ k: 1, base_ndcg: 0.5 }] };
  const calls = [];
  const saved = await saveSeriesOrder(async (...args) => { calls.push(args); return record; }, record.id, order);
  assert.deepEqual(calls, [['/plots/plot%20%2F%201', { method: 'PATCH', body: { plot: { series_order: order } } }]]);
  assert.equal(saved, record);
  assert.notEqual(calls[0][1].body.plot.series_order, order);
});

test('Invalid order values never issue a save request', async () => {
  let requests = 0;
  for (const order of [null, {}, 'base_ndcg', [null], [4], [''], ['base_ndcg', 'base_ndcg']]) {
    await assert.rejects(saveSeriesOrder(async () => { requests++; }, 'plot-1', order), /plot\.series_order/);
  }
  assert.equal(requests, 0);
});

test('Save reports old backend support and unconfirmed order instead of false success', async () => {
  const order = ['cosine_ndcg', 'base_ndcg'];
  await assert.rejects(saveSeriesOrder(async () => { throw new Error('Unknown plot options: series_order'); }, 'plot-1', order), /Deploy the updated Lambda backend/);
  await assert.rejects(saveSeriesOrder(async () => ({ plot: {} }), 'plot-1', order), /Deploy the updated Lambda backend/);
  await assert.rejects(saveSeriesOrder(async () => ({ plot: { series_order: ['base_ndcg', 'cosine_ndcg'] } }), 'plot-1', order), /different column order/);
  await assert.rejects(saveSeriesOrder(async () => ({ plot: { series_order: ['cosine_ndcg'] } }), 'plot-1', order), /different column order/);
  await assert.rejects(saveSeriesOrder(async () => { throw new Error('Storage unavailable'); }, 'plot-1', order), /Storage unavailable/);
});

test('Save accepts an explicit empty array to reset automatic order', async () => {
  const result = await saveSeriesOrder(async (path, options) => ({ plot: options.body.plot }), 'plot-1', []);
  assert.deepEqual(result.plot.series_order, []);
});
