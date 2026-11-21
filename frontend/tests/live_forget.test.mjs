// Design review Z: live.js after forget(): nothing of a conversation known to be deleted is held there, taken in again
// or sent from there. Each step waits for the event it names (subscribe); no timer, no sleep.
import test from 'node:test';
import assert from 'node:assert/strict';
import { ApiError } from '../src/api.js';
import { clear, deleted, forget, recheck, send, subscribe, unsavedAnswer } from '../src/live.js';

function backend(t) { // a turn's stream the test pushes to; every other request answers 404 not_found
  let controller; const asked = [];
  const body = new ReadableStream({ start(c) { controller = c; } });
  t.mock.method(globalThis, 'fetch', async (path) => { asked.push(path);
    return path.endsWith('/stream') ? new Response(body) : new Response('{"code":"not_found"}', { status: 404 }); });
  return { asked, push: (...events) => events.forEach((event) => controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`))), end: () => controller.close() };
}
const heard = (type) => new Promise((resolve) => { const stop = subscribe((id, turn, event) => { if (event === type) { stop(); resolve(); } }); });
const held = (conversationId) => { // whether a finished turn of it is held: clear() says so only then
  const events = []; const stop = subscribe((id, turn, event) => events.push(event)); clear(conversationId); stop();
  return events.includes('cleared'); };

test('a forgotten conversation holds nothing and takes nothing in again', async (t) => {
  const server = backend(t);
  const admitted = send('z1', '/api/conversations/z1/message/stream', {}, 'DELETED-QUESTION');
  server.push({ type: 'run_started', run_id: 'rz1' }); await admitted;
  const kept = heard('chat_response');
  server.push({ type: 'chat_response', content: 'DELETED-ANSWER', result_saved: false }); await kept;
  assert.deepEqual(unsavedAnswer('rz1'), { text: 'DELETED-ANSWER', cut: false });
  forget('z1');
  assert.equal(unsavedAnswer('rz1'), null, 'its answer shown but not saved');
  const later = heard('chat_response'); // an event of its stream that was on its way
  server.push({ type: 'chat_response', content: 'DELETED-ANSWER-2', result_saved: false }); await later;
  assert.equal(unsavedAnswer('rz1'), null, 'an answer that came after');
  const ended = heard('run_finished');
  server.push({ type: 'run_finished', status: 'cancelled' }); await ended;
  assert.equal(held('z1'), false, 'its live turn');
});

test('nothing is sent to a forgotten conversation', async (t) => {
  const server = backend(t);
  forget('z2');
  await assert.rejects(send('z2', '/api/runs/rz2/continue', {}, 'Q'), (error) => error.code === 'not_found');
  assert.deepEqual(server.asked, []);
});

test('a conversation whose record answers 404 when its stream ends is forgotten', async (t) => {
  const server = backend(t); // its record's read (live.js drain, at the stream's end) answers 404: it was deleted meanwhile
  const admitted = send('z3', '/api/conversations/z3/message/stream', {}, 'DELETED-QUESTION');
  server.push({ type: 'run_started', run_id: 'rz3' }); await admitted;
  const kept = heard('chat_response');
  server.push({ type: 'chat_response', content: 'DELETED-ANSWER', result_saved: false }); await kept;
  const forgotten = heard('deleted');
  server.push({ type: 'run_finished', status: 'cancelled' }); server.end(); await forgotten;
  assert.equal(unsavedAnswer('rz3'), null);
  assert.equal(held('z3'), false);
});

test('as a deletion settles, a conversation held here whose record answers 404 is forgotten', async (t) => {
  const server = backend(t); // the turn has ended and waits to be handed over; its conversation went with its project
  const admitted = send('z4', '/api/conversations/z4/message/stream', {}, 'DELETED-QUESTION');
  server.push({ type: 'run_started', run_id: 'rz4' }); await admitted;
  const ended = heard('run_finished');
  server.push({ type: 'chat_response', content: 'DELETED-ANSWER', result_saved: false }, { type: 'run_finished', status: 'succeeded' }); await ended;
  const forgotten = heard('deleted');
  recheck(); await forgotten;
  assert.equal(unsavedAnswer('rz4'), null);
  assert.equal(held('z4'), false);
});

test('only a record\'s own 404 not_found counts as deleted', () => { // the whole-batch review (F4)
  assert.equal(deleted(new ApiError(404, 'not_found')), true);
  assert.equal(deleted(new ApiError(404, 'http_error')), false);
  assert.equal(deleted(new ApiError(503, 'not_found')), false);
});

test('as a deletion settles, an answer held after its live turn was cleared is forgotten too', async (t) => {
  const server = backend(t); // the turn was handed over (cleared), its answer not saved stays shown by its run
  const admitted = send('z5', '/api/conversations/z5/message/stream', {}, 'DELETED-QUESTION');
  server.push({ type: 'run_started', run_id: 'rz5' }); await admitted;
  const ended = heard('run_finished');
  server.push({ type: 'chat_response', content: 'DELETED-ANSWER', result_saved: false }, { type: 'run_finished', status: 'succeeded' }); await ended;
  clear('z5');
  assert.equal(held('z5'), false);
  assert.deepEqual(unsavedAnswer('rz5'), { text: 'DELETED-ANSWER', cut: false });
  const forgotten = heard('deleted');
  recheck(); await forgotten;
  assert.equal(unsavedAnswer('rz5'), null);
});
