import test from 'node:test';
import assert from 'node:assert/strict';
import { catalogWithConcurrentSaves, rememberSavedReference, referenceSaveWarnings, upsertReference } from '../frontend/src/auto-reference.ts';

test('auto-saved references appear immediately without replacing the current catalog or mutating it', () => {
  const original = [{id: 'builtin', name: 'Existing'}];
  const saved = {id: 'custom', name: 'Uploaded reference'};
  assert.deepEqual(upsertReference(original, saved), [original[0], saved]);
  assert.deepEqual(original, [{id: 'builtin', name: 'Existing'}]);
});

test('deduped or refreshed references replace their catalog entry in place, never duplicate it', () => {
  const previous = [{id: 'a', name: 'First'}, {id: 'b', name: 'Second'}];
  const fresh = {id: 'a', name: 'First', image: {url: 'fresh'}};
  assert.deepEqual(upsertReference(previous, fresh), [fresh, previous[1]]);
  assert.deepEqual(upsertReference(upsertReference(previous, fresh), fresh), [fresh, previous[1]]);
  assert.equal(previous[0].image, undefined);
});

test('a delayed catalog response cannot hide a reference saved during the fetch', () => {
  const oldCatalog = [{id: 'builtin'}];
  const saved = {id: 'new', image: {url: 'durable'}};
  assert.deepEqual(catalogWithConcurrentSaves(oldCatalog, [{revision: 2, template: saved}], 1), [...oldCatalog, saved]);
  assert.deepEqual(oldCatalog, [{id: 'builtin'}]);
});

test('a later catalog refresh honors deletion instead of resurrecting earlier automatic saves', () => {
  const catalogAfterDeletion = [{id: 'builtin'}];
  const previousSave = {revision: 2, template: {id: 'deleted'}};
  assert.equal(catalogWithConcurrentSaves(catalogAfterDeletion, [previousSave], 2), catalogAfterDeletion);
});

test('later generated colors preserve the first successful newly-saved message', () => {
  const first = {template: {id: 'a'}, created: true};
  const duplicate = {template: {id: 'a'}, created: false};
  assert.equal(rememberSavedReference(first, duplicate).created, true);
  assert.equal(rememberSavedReference(null, duplicate).created, false);
  assert.equal(rememberSavedReference(first, {template: {id: 'b'}, created: false}).created, false);
  assert.equal(rememberSavedReference(duplicate, first).created, true);
});

test('reference-save failure remains a visible result warning without losing existing warnings', () => {
  const existing = ['Plate was not found'];
  assert.deepEqual(referenceSaveWarnings(existing, 'Reference could not be saved'), [...existing, 'Reference could not be saved']);
  assert.equal(referenceSaveWarnings(existing), existing);
  assert.deepEqual(referenceSaveWarnings(existing, existing[0]), existing);
  assert.deepEqual(existing, ['Plate was not found']);
});
