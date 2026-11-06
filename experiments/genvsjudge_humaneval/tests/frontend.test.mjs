import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const source = await readFile(new URL('../app.js', import.meta.url), 'utf8');
const { normalizeServerUrl, normalizeName, progressValues, canPreserveDraft } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

test('server origin normalization keeps sessions isolated by endpoint', () => {
  assert.equal(normalizeServerUrl(' https://example.lambda-url.us-east-1.on.aws/ '), 'https://example.lambda-url.us-east-1.on.aws');
  assert.notEqual(normalizeServerUrl('https://first.example'), normalizeServerUrl('https://second.example'));
  for (const value of ['http://example.com', 'https://user:pass@example.com', 'https://example.com/api', 'https://example.com/?name=x', 'https://example.com/#test', 'javascript:alert(1)']) {
    assert.throws(() => normalizeServerUrl(value));
  }
});

test('blank names rejected, normalized spaces preserved without inventing identity', () => {
  assert.equal(normalizeName('  Alex   Chen  '), 'Alex Chen');
  assert.throws(() => normalizeName('  '));
  assert.throws(() => normalizeName('a'.repeat(81)));
});

test('zero minimum remains zero despite a large total', () => {
  const progress = progressValues({ total_questions: 300, target_reviews: 3, min_reviews: 0, total_reviews: 1000, questions_at_target: 299, remaining_reviews: 3 });
  assert.equal(progress.complete, false);
  assert.equal(progress.min_reviews, 0);
});

test('completion depends on all questions reaching the minimum', () => {
  assert.equal(progressValues({ total_questions: 300, target_reviews: 3, min_reviews: 3, total_reviews: 904, questions_at_target: 300, remaining_reviews: 0 }).complete, true);
  assert.throws(() => progressValues({ total_questions: 300, target_reviews: 3, min_reviews: -1, total_reviews: 0, questions_at_target: 0, remaining_reviews: 900 }));
  assert.throws(() => progressValues({ total_questions: 299, target_reviews: 3, min_reviews: 3, total_reviews: 897, questions_at_target: 299, remaining_reviews: 0 }));
});

test('reconnect cannot carry a judgment into a different study or changed evidence', () => {
  const question = { id: 'nq-0001', dataset: 'nq', question: 'What is the capital?', answer: 'Paris', documents: { A: 'Document one.', B: 'Document two.' }, assignment_token: 'old-token' };
  const renewed = { ...question, assignment_token: 'new-token' };
  assert.equal(canPreserveDraft('study-v1', 'study-v1', question, renewed), true);
  assert.equal(canPreserveDraft('study-v1', 'study-v2', question, renewed), false);
  assert.equal(canPreserveDraft('', '', question, renewed), false);
  for (const key of ['id', 'dataset', 'question', 'answer']) {
    assert.equal(canPreserveDraft('study-v1', 'study-v1', question, { ...renewed, [key]: 'different' }), false, key);
  }
  for (const key of ['A', 'B']) {
    assert.equal(canPreserveDraft('study-v1', 'study-v1', question, { ...renewed, documents: { ...question.documents, [key]: 'Changed passage.' } }), false, key);
  }
  assert.equal(canPreserveDraft('study-v1', 'study-v1', question, { ...renewed, documents: { A: question.documents.B, B: question.documents.A } }), false);
  assert.equal(canPreserveDraft('study-v1', 'study-v1', question, null), false);
});
