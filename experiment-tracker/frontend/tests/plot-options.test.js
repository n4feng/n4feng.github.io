import test from 'node:test';
import assert from 'node:assert/strict';
import { buildPlotOptions, savePlotOptions } from '../src/plot-options.js';

const metric = { id: 'ndcg_at_k', name: 'NDCG@k', x_key: 'k' };
const data = [{ k: 1, base_ndcg: 0.6, weighted_ndcg: 0.7 }];
const form = {
  type: 'bar', title: 'Original title', description: 'Original caption',
  x_label: 'k', y_label: 'Score', y_min: '0', y_max: '1',
};
const options = (advanced, extra = {}) => ({ form, advanced, metric, data, ...extra });

test('Save sends explicit JSON values instead of stale form values', async () => {
  const replacement = {
    type: 'line', title: 'Revised title', description: 'New caption', x_key: 'k',
    x_label: 'Cutoff', y_label: 'Normalized gain', y_min: 0.4, y_max: 0.9,
    series_labels: { base_ndcg: 'Base model' }, colors: { weighted_ndcg: '#12ab34' },
  };
  const calls = [];
  const saved = await savePlotOptions(async (...args) => {
    calls.push(args);
    return { id: 'plot-1', data, plot: args[1].body.plot };
  }, 'plot-1', options(JSON.stringify(replacement)));
  assert.deepEqual(calls, [['/plots/plot-1', { method: 'PATCH', body: { plot: replacement } }]]);
  assert.deepEqual(saved.plot, replacement);
  assert.deepEqual(saved.data, data);
});

test('Deleting maps sends explicit empty maps and cleared bounds', () => {
  const result = buildPlotOptions(options('{}', { form: { ...form, y_min: '', y_max: '' } }));
  assert.deepEqual(result, {
    ...form, x_key: 'k', y_min: null, y_max: null, series_labels: {}, colors: {},
  });
  const partial = buildPlotOptions(options('{"series_labels":{"base_ndcg":"Base"}}'));
  assert.deepEqual(partial.series_labels, { base_ndcg: 'Base' });
  assert.deepEqual(partial.colors, {});
});

test('Reopening with saved maps retains them and allows normal form edits', () => {
  const config = buildPlotOptions(options('{"title":"JSON title","colors":{"base_ndcg":"blue"}}'));
  const advanced = JSON.stringify({ series_labels: config.series_labels, colors: config.colors });
  const reopened = buildPlotOptions(options(advanced, { form: { ...config, title: 'Edited in form' } }));
  assert.equal(reopened.title, 'Edited in form');
  assert.deepEqual(reopened.colors, { base_ndcg: 'blue' });
});

test('Explicit JSON null bounds override numbers in the form', () => {
  const result = buildPlotOptions(options('{"y_min":null,"y_max":null}'));
  assert.equal(result.y_min, null);
  assert.equal(result.y_max, null);
});

test('Invalid JSON or unsupported settings never reach the save request', async () => {
  let requests = 0;
  const api = async () => { requests++; };
  for (const advanced of [
    '', ' ', '{"type":"line",}', '{', 'null', '[]', '"line"',
    '{"type":"pie"}', '{"title":12}', '{"title":null}', '{"x_key":"epoch"}',
    '{"y_min":"0.5"}', '{"y_max":1e999}', '{"y_min":true}', '{"y_min":0.9,"y_max":0.1}',
    '{"series_labels":[]}', '{"colors":null}', '{"colors":{"missing":"red"}}',
    '{"series_labels":{"base_ndcg":""}}', '{"colors":{"base_ndcg":"bad;color"}}',
    '{"unknown_option":true}', '{"__proto__":{}}',
  ]) {
    await assert.rejects(savePlotOptions(api, 'plot-1', options(advanced)), undefined, advanced);
  }
  assert.equal(requests, 0);
});

test('Save propagates API errors instead of reporting success', async () => {
  await assert.rejects(savePlotOptions(async () => { throw new Error('Storage unavailable'); }, 'plot-1', options('{}')), /Storage unavailable/);
});

test('Uploaded plot options retain precedence and use their resolved metric', () => {
  const result = buildPlotOptions(options('{"type":"bar"}', {
    metric: { id: 'loss', name: 'Loss', x_key: 'epoch' },
    data: [{ epoch: 1, train: 2.3 }],
    requestPlot: { type: 'line', colors: { train: '#abc' }, x_label: 'Epoch' },
  }));
  assert.equal(result.x_key, 'epoch');
  assert.equal(result.type, 'line');
  assert.equal(result.x_label, 'Epoch');
  assert.deepEqual(result.colors, { train: '#abc' });
});

test('Validation preserves literal series names and matches Unicode text limits', () => {
  const title = '😀'.repeat(300);
  const result = buildPlotOptions(options(JSON.stringify({ title, series_labels: JSON.parse('{"__proto__":"Literal series"}') }), {
    data: [JSON.parse('{"k":1,"__proto__":0.5}')],
  }));
  assert.equal(result.title, title);
  assert.equal(Object.hasOwn(result.series_labels, '__proto__'), true);
  assert.equal(result.series_labels.__proto__, 'Literal series');
  assert.throws(() => buildPlotOptions(options(JSON.stringify({ title: title + '😀' }))), /at most 300/);
});
