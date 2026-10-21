import test from 'node:test';
import assert from 'node:assert/strict';
import { plateLabel } from '../frontend/src/plate-label.ts';

test('old references retain brand + code and empty brand means code only', () => {
  assert.equal(plateLabel({brand_name: 'Asturcolor'}, '575'), 'Asturcolor 575');
  assert.equal(plateLabel({brand_name: ''}, '575'), '575');
});

test('all plate modes keep brand, code and literal custom text independent', () => {
  const base = {brand_name: 'My Paint', plate_text: 'CUSTOM 42'};
  for (const [mode, expected] of [['brand_code', 'My Paint G44'], ['code', 'G44'], ['brand', 'My Paint'], ['custom', 'CUSTOM 42'], ['blank', '']]) {
    assert.equal(plateLabel({...base, plate_text_mode: mode}, 'G44'), expected);
  }
  assert.equal(plateLabel({...base, plate_text_mode: 'custom', plate_text: '{code}'}, 'G44'), '{code}');
});

test('custom labels support Ukrainian, whitespace and an empty plate', () => {
  assert.equal(plateLabel({brand_name: '', plate_text_mode: 'custom', plate_text: '  Моя\n фарба\t 42  '}, 'G44'), 'Моя фарба 42');
  assert.equal(plateLabel({brand_name: 'Ignored', plate_text_mode: 'custom'}, 'G44'), '');
});

test('code updates per item; fixed labels never acquire a code and do not mutate config', () => {
  const config = {brand_name: 'Custom', plate_text_mode: 'brand'};
  assert.equal(plateLabel(config, 'A'), plateLabel(config, 'B'));
  assert.equal(plateLabel({...config, plate_text_mode: 'code'}, 'B'), 'B');
  assert.deepEqual(config, {brand_name: 'Custom', plate_text_mode: 'brand'});
});
