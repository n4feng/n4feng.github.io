const DATASETS = { nq: 'Natural Questions', hotpotqa: 'HotpotQA', msmarco: 'MS MARCO' };
const STORAGE_PREFIX = 'genvsjudge-human-evaluation-v1';

export function normalizeServerUrl(value) {
  let url;
  try { url = new URL(value.trim()); } catch { throw new Error('Enter the complete HTTPS Lambda server URL supplied by the organizer.'); }
  if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash || !/^\/*$/.test(url.pathname)) {
    throw new Error('Use an HTTPS server origin without a path, query, password, or fragment.');
  }
  return url.origin;
}

export function normalizeName(value) {
  const name = value.normalize('NFKC').trim().replace(/\s+/g, ' ');
  if (!name || name.length > 80) throw new Error('Enter your name (up to 80 characters).');
  return name;
}

export function progressValues(stats) {
  const names = ['total_questions', 'target_reviews', 'min_reviews', 'total_reviews', 'questions_at_target', 'remaining_reviews'];
  if (!stats || names.some((key) => !Number.isSafeInteger(stats[key]) || stats[key] < 0) || stats.total_questions !== 300 || stats.target_reviews < 1 || stats.questions_at_target > stats.total_questions) {
    throw new Error('The server returned invalid progress counts. Please contact the study organizer.');
  }
  return { ...stats, complete: stats.min_reviews >= stats.target_reviews };
}

export function canPreserveDraft(previousStudyId, nextStudyId, previous, next) {
  return Boolean(previousStudyId && previousStudyId === nextStudyId && previous && next
    && previous.id === next.id
    && previous.dataset === next.dataset
    && previous.question === next.question
    && previous.answer === next.answer
    && previous.documents?.A === next.documents?.A
    && previous.documents?.B === next.documents?.B);
}

class ApiError extends Error {
  constructor(message, status = 0) { super(message); this.status = status; }
}

function safeStorage(storage, action, key, value) {
  try { return window[storage][action](key, value); } catch { return null; }
}

export function bootAudit() {
  const $ = (id) => document.getElementById(id);
  const state = { server: '', name: '', token: '', studyId: '', question: null, pending: null, saved: false, busy: false, connected: false, stats: null, reviewerCompleted: 0 };
  let polling = null;
  const text = (id, value) => { $(id).textContent = String(value); };
  const tokenKey = (server) => `${STORAGE_PREFIX}:session:${server}`;

  function message(value = '', isError = false) {
    const element = $(isError ? 'error' : 'notice');
    element.textContent = value;
    element.hidden = !value;
  }

  function setBusy(busy) {
    state.busy = busy;
    $('join-button').disabled = busy;
    $('submit-button').disabled = busy;
    $('skip-button').disabled = busy || !!state.pending || state.saved;
    $('change-session').disabled = busy;
    $('return-button').disabled = busy;
    $('finish-change-session').disabled = busy;
    $('ratings-a').disabled = busy || !!state.pending || state.saved;
    $('ratings-b').disabled = busy || !!state.pending || state.saved;
    $('review-notes').disabled = busy || !!state.pending || state.saved;
  }

  async function api(path, { method = 'GET', body, authorized = true } = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 45000);
    const headers = { Accept: 'application/json' };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (authorized && state.token) headers.Authorization = `Bearer ${state.token}`;
    let response;
    try {
      response = await fetch(`${state.server}${path}`, {
        method, headers, ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
        signal: controller.signal, cache: 'no-store', credentials: 'omit', referrerPolicy: 'no-referrer',
      });
      let result;
      try { result = await response.json(); } catch { throw new ApiError('The server did not return an audit response. Check the Lambda URL with the organizer.', response.status); }
      if (!response.ok) throw new ApiError(result.error || `The server returned an error (${response.status}).`, response.status);
      return result;
    } catch (error) {
      if (error instanceof ApiError) throw error;
      if (error.name === 'AbortError') throw new ApiError('The server took too long to respond. Please try again.');
      throw new ApiError('Could not reach the audit server. Check your connection and the URL. The organizer may need to check the server’s CORS configuration.');
    } finally { clearTimeout(timeout); }
  }

  function renderStats(rawStats) {
    const stats = progressValues(rawStats);
    state.stats = stats;
    text('minimum-label', `Minimum reviews across all ${stats.total_questions} questions`);
    text('minimum-count', stats.min_reviews);
    text('target-count', stats.target_reviews);
    text('coverage-target', stats.target_reviews);
    text('covered-count', stats.questions_at_target);
    text('question-count', stats.total_questions);
    text('remaining-count', stats.remaining_reviews);
    text('total-count', stats.total_reviews);
    $('coverage-progress').max = stats.total_questions;
    $('coverage-progress').value = stats.questions_at_target;
    text('progress-status', stats.complete ? 'Every question has reached the target' : 'Live counts · refresh every 30 seconds');
    text('minimum-description', stats.complete ? 'The three-reviewer target has been reached across the study. Additional independent reviews are welcome.' : 'The least-reviewed question sets this number. Questions with no reviews are included.');
    const datasetProgress = $('dataset-progress');
    datasetProgress.replaceChildren();
    for (const [key, label] of Object.entries(DATASETS)) {
      const counts = stats.by_dataset?.[key];
      if (!counts) continue;
      const item = document.createElement('span');
      const dot = document.createElement('i');
      dot.setAttribute('aria-hidden', 'true');
      const name = document.createElement('strong');
      name.textContent = label;
      item.append(dot, name, document.createTextNode(`min ${counts.min_reviews} · ${counts.questions_at_target}/${counts.total_questions} at target`));
      datasetProgress.append(item);
    }
  }

  function renderPersonal(count) {
    if (!Number.isSafeInteger(count) || count < 0) throw new Error('The server returned an invalid reviewer count.');
    state.reviewerCompleted = count;
    text('personal-count', count);
  }

  function showReconnect(error) {
    state.connected = false;
    if (polling) clearInterval(polling);
    polling = null;
    $('join-panel').hidden = false;
    $('review-panel').hidden = true;
    $('finished-panel').hidden = true;
    text('join-title', 'Reconnect to continue');
    text('join-button', 'Reconnect to the audit');
    $('reviewer-name').value = state.name;
    $('server-url').value = state.server;
    message(`${error.message} Reconnect with the same name and server to continue. Your current selections are preserved.`, true);
    $('reviewer-name').focus();
  }

  function handleError(error) {
    if (error.status === 401 && state.name) showReconnect(error);
    else message(error.message || 'Something went wrong. Please try again.', true);
  }

  function renderQuestion(question) {
    if (!question || typeof question.id !== 'string' || typeof question.question !== 'string' || typeof question.answer !== 'string' || typeof question.documents?.A !== 'string' || typeof question.documents?.B !== 'string' || typeof question.assignment_token !== 'string') {
      throw new Error('The server returned an incomplete question. Please contact the organizer.');
    }
    state.question = question;
    state.pending = null;
    state.saved = false;
    $('review-form').reset();
    text('dataset-label', DATASETS[question.dataset] || question.dataset);
    text('question-title', question.question);
    text('reference-answer', question.answer);
    text('question-id', `Question ${question.id}`);
    text('document-a', question.documents.A);
    text('document-b', question.documents.B);
    $('document-a').scrollTop = 0;
    $('document-b').scrollTop = 0;
    text('submit-button', 'Save & next question →');
    text('submission-help', 'Choose one judgment for each document. Your review will be saved before the next question appears.');
    $('review-panel').hidden = false;
    $('finished-panel').hidden = true;
    $('join-panel').hidden = true;
    text('connected-name', state.name);
  }

  function renderFinished() {
    state.question = null;
    state.pending = null;
    state.saved = false;
    $('review-panel').hidden = true;
    $('finished-panel').hidden = false;
    $('join-panel').hidden = true;
    const completed = state.reviewerCompleted;
    text('finished-description', state.stats?.complete
      ? `You have reviewed ${completed} questions, and every question in the study has reached the minimum target. Thank you for helping us assess the evidence.`
      : `You have reviewed all ${completed} questions available to you. Some questions still need other independent reviewers: the study-wide minimum is ${state.stats?.min_reviews ?? '—'} of ${state.stats?.target_reviews ?? 3}. Thank you for your contribution.`);
    $('finished-panel').focus({ preventScroll: true });
  }

  async function loadNext(exclude = '') {
    const result = await api(`/api/next${exclude ? `?exclude=${encodeURIComponent(exclude)}` : ''}`);
    renderStats(result.stats);
    renderPersonal(result.reviewer_completed);
    if (result.question === null) renderFinished();
    else {
      renderQuestion(result.question);
      if (exclude && result.question.id === exclude) message('This is your only remaining unreviewed question. You can use “Unsure” if you cannot confidently judge it.');
      $('question-title').focus({ preventScroll: true });
      $('review-panel').scrollIntoView({ behavior: 'instant', block: 'start' });
    }
  }

  function startPolling() {
    if (polling) clearInterval(polling);
    polling = setInterval(async () => {
      if (!state.connected || state.busy || document.hidden) return;
      try { renderStats(await api('/api/stats', { authorized: false })); }
      catch { text('progress-status', 'Counts may be outdated · reconnecting on next refresh'); }
    }, 30000);
  }

  $('join-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (state.busy) return;
    message('', true);
    try {
      const server = normalizeServerUrl($('server-url').value);
      const name = normalizeName($('reviewer-name').value);
      const sameReviewer = server === state.server && name.toLocaleLowerCase() === state.name.toLocaleLowerCase();
      if (state.pending && !sameReviewer) throw new Error('A review is awaiting confirmation. Reconnect with the same name and server and retry that review before switching.');
      setBusy(true);
      state.server = server;
      const health = await api('/api/health', { authorized: false });
      if (health.ok !== true || health.question_count !== 300) throw new Error('This endpoint is not a ready 300-question audit. Please ask the organizer to verify the deployed study.');
      const session = await api('/api/session', { method: 'POST', body: { name }, authorized: false });
      if (!session.token || !session.name || typeof session.study_id !== 'string' || !session.study_id || session.study_id !== health.study_id) throw new Error('The server returned an invalid reviewer session.');
      const previousStudyId = state.studyId;
      const studyChanged = Boolean(previousStudyId && previousStudyId !== session.study_id);
      if (studyChanged) {
        state.question = null;
        state.pending = null;
        state.saved = false;
        $('review-form').reset();
      }
      state.token = session.token;
      state.name = session.name;
      state.studyId = session.study_id;
      state.connected = true;
      safeStorage('sessionStorage', 'setItem', tokenKey(server), JSON.stringify({ token: state.token, name: state.name, study_id: session.study_id }));
      safeStorage('localStorage', 'setItem', `${STORAGE_PREFIX}:last-connection`, JSON.stringify({ server, name: state.name }));
      $('join-panel').hidden = true;
      $('finished-panel').hidden = true;
      if (sameReviewer && state.question && (state.pending || !state.saved)) {
        const refreshed = await api(`/api/next?question_id=${encodeURIComponent(state.question.id)}`);
        renderStats(refreshed.stats);
        renderPersonal(refreshed.reviewer_completed);
        if (canPreserveDraft(previousStudyId, state.studyId, state.question, refreshed.question)) {
          state.question = refreshed.question;
          if (state.pending) state.pending.assignment_token = refreshed.question.assignment_token;
          $('review-panel').hidden = false;
          text('connected-name', state.name);
          message(state.pending ? 'Reconnected. Retry your pending review to confirm it was saved.' : 'Reconnected. Your current selections are preserved.');
        } else if (refreshed.question) {
          renderQuestion(refreshed.question);
          message('The question, reference answer, or document presentation changed after reconnecting. Your previous selections were cleared; please review this item again before submitting.');
        } else {
          renderFinished();
        }
      } else {
        await loadNext();
        message(studyChanged
          ? 'This server now hosts a different study. Your previous selections were cleared and will not be submitted to the new study. Please contact the organizer if an earlier submission was awaiting confirmation.'
          : 'Welcome, and thank you for taking part. Questions with fewer reviews are prioritized.');
      }
      startPolling();
    } catch (error) {
      $('join-panel').hidden = false;
      handleError(error);
    } finally { setBusy(false); }
  });

  $('review-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (state.busy || !state.question) return;
    message('', true);
    if (state.saved) {
      setBusy(true);
      try { await loadNext(); } catch (error) { handleError(error); } finally { setBusy(false); }
      return;
    }
    if (!state.pending) {
      if (!$('review-form').reportValidity()) return;
      const form = new FormData($('review-form'));
      state.pending = {
        question_id: state.question.id,
        assignment_token: state.question.assignment_token,
        ratings: { A: form.get('rating_a'), B: form.get('rating_b') },
        notes: $('review-notes').value.trim(),
      };
    }
    setBusy(true);
    text('submit-button', 'Saving your review…');
    let confirmed = false;
    try {
      const result = await api('/api/responses', { method: 'POST', body: state.pending });
      if (result.saved !== true) throw new Error('The server did not confirm that your review was saved. Retry to check safely.');
      confirmed = true;
      state.pending = null;
      state.saved = true;
      renderStats(result.stats);
      renderPersonal(result.reviewer_completed);
      message(result.duplicate ? 'Your review was already saved. Thank you — it has only been counted once.' : 'Review saved. Thank you for your careful judgment.');
      await loadNext();
    } catch (error) {
      if (error.status === 409) {
        state.pending = null;
        state.saved = true;
        text('submit-button', 'Load next question →');
        text('submission-help', 'A review from this name already exists for this question. Your previous saved review has been preserved.');
      } else if (confirmed) {
        text('submit-button', 'Load next question →');
        text('submission-help', 'Your review is saved. Try loading the next question when your connection is ready.');
      } else {
        text('submit-button', 'Retry saving this review →');
        text('submission-help', 'Saving has not been confirmed. Your selections are locked so retrying sends the same review and cannot count it twice.');
      }
      handleError(error);
    } finally { setBusy(false); }
  });

  $('skip-button').addEventListener('click', async () => {
    if (state.busy || state.pending || !state.question) return;
    message('', true);
    setBusy(true);
    try {
      const previous = state.question.id;
      await loadNext(previous);
      if (state.question?.id !== previous) message('Question skipped. No review was recorded; it remains available for review.');
    } catch (error) { handleError(error); } finally { setBusy(false); }
  });

  function changeSession() {
    if (state.busy) return;
    if (state.pending) { message('Please retry your pending submission before changing reviewer or server.', true); return; }
    state.connected = false;
    if (polling) clearInterval(polling);
    polling = null;
    $('join-panel').hidden = false;
    $('review-panel').hidden = true;
    $('finished-panel').hidden = true;
    text('join-title', 'Reviewer & server');
    text('join-button', 'Continue reviewing →');
    $('reviewer-name').value = state.name;
    $('server-url').value = state.server;
    $('reviewer-name').focus();
  }
  $('change-session').addEventListener('click', changeSession);
  $('finish-change-session').addEventListener('click', changeSession);
  $('return-button').addEventListener('click', async () => {
    if (state.busy) return;
    message('', true);
    setBusy(true);
    try { await loadNext(); } catch (error) { handleError(error); } finally { setBusy(false); }
  });

  try {
    const saved = JSON.parse(safeStorage('localStorage', 'getItem', `${STORAGE_PREFIX}:last-connection`) || 'null');
    if (saved?.server && saved?.name) {
      $('server-url').value = normalizeServerUrl(saved.server);
      $('reviewer-name').value = normalizeName(saved.name);
      text('join-button', 'Continue reviewing →');
    }
  } catch { /* A stale local preference should never block participation. */ }
  window.addEventListener('beforeunload', (event) => {
    if (state.pending) { event.preventDefault(); event.returnValue = ''; }
  });
}

if (typeof document !== 'undefined') bootAudit();
