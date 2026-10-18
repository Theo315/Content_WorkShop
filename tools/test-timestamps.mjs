import test from 'node:test';
import assert from 'node:assert/strict';
import { parseTimestampStart, parseTimestampStep, planLastWrittenAt, zipTimestamp } from '../frontend/src/timestamps.ts';
import JSZip from '../frontend/node_modules/jszip/lib/index.js';

test('Kyiv input converts with summer and winter offsets', () => {
  assert.equal(new Date(parseTimestampStart('2026-10-04T09:00')).toISOString(), '2026-10-04T06:00:00.000Z');
  assert.equal(new Date(parseTimestampStart('2026-12-04T09:00')).toISOString(), '2026-12-04T07:00:00.000Z');
  assert.throws(() => parseTimestampStart('2026-02-30T09:00'));
  assert.throws(() => parseTimestampStart('2026-03-29T03:30'));
  assert.throws(() => parseTimestampStart('1979-12-01T09:00'));
});
test('step range is cumulative, fixed or randomized as original HexPh', () => {
  assert.deepEqual(parseTimestampStep('8–6'), [6, 8]);
  assert.deepEqual(parseTimestampStep('0,5'), [0.5, 0.5]);
  assert.throws(() => parseTimestampStep('-3'));
  assert.throws(() => parseTimestampStep('6-8-10'));
  const dates = planLastWrittenAt({ enabled: true, start: '2026-10-04T09:00', step: '6-8' }, 3, () => 0.5);
  assert.deepEqual(dates, ['2026-10-04T06:00:00.000Z', '2026-10-04T06:07:00.000Z', '2026-10-04T06:14:00.000Z']);
  assert.deepEqual(planLastWrittenAt({enabled:false,start:'',step:''},2), [undefined,undefined]);
});
test('ZIP stores the selected local LastWriteTime, with DOS two-second precision', async () => {
  const archive = new JSZip();
  archive.file('photo.png', 'png', {date: zipTimestamp('2026-10-04T06:00:07.000Z')});
  archive.file('sds.pdf', 'pdf', {date: zipTimestamp('2026-10-04T06:07:00.000Z')});
  const loaded = await JSZip.loadAsync(await archive.generateAsync({type:'uint8array'}));
  assert.equal(loaded.file('photo.png').date.toISOString(), '2026-10-04T09:00:06.000Z');
  assert.equal(loaded.file('sds.pdf').date.toISOString(), '2026-10-04T09:07:00.000Z');
});
