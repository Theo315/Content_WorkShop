import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { prepareThemeWave, clearThemeWave } from '../frontend/src/theme-wave.ts';

function root(theme) {
  const properties = new Map();
  return { dataset: {theme}, properties, style: {
    setProperty: (name, value) => properties.set(name, value),
    removeProperty: name => { const value = properties.get(name) ?? ''; properties.delete(name); return value; },
  }};
}

test('dark and light use the same prepared reveal, including repeated switches', () => {
  const target = root('dark');
  for (const theme of ['light', 'dark', 'light', 'dark', 'light', 'dark']) {
    prepareThemeWave(target, 1224, 32, 1280, 720);
    target.dataset.theme = theme;
    assert.equal(target.dataset.themeTransition, 'wave');
    assert.equal(target.properties.get('--theme-wave-x'), '1224px');
    assert.equal(target.properties.get('--theme-wave-y'), '32px');
    const radius = parseFloat(target.properties.get('--theme-wave-radius'));
    for (const [x, y] of [[0, 0], [1280, 0], [0, 720], [1280, 720]])
      assert.ok(radius > Math.hypot(1224 - x, 32 - y));
    clearThemeWave(target);
    assert.equal(target.dataset.themeTransition, undefined);
    assert.equal(target.properties.size, 0);
    assert.equal(target.dataset.theme, theme);
  }
});

test('keyboard/touch origins and out-of-viewport coordinates stay bounded', () => {
  const target = root('light');
  prepareThemeWave(target, -10, 1000, 360, 800);
  assert.equal(target.properties.get('--theme-wave-x'), '0px');
  assert.equal(target.properties.get('--theme-wave-y'), '800px');
  clearThemeWave(target);
  clearThemeWave(target);
  assert.equal(target.properties.size, 0);
});

test('CSS starts a two-way wave at pseudo-tree creation and honors reduced motion', () => {
  const css = readFileSync(new URL('../frontend/src/theme-transition.css', import.meta.url), 'utf8');
  assert.match(css, /html\[data-theme-transition="wave"\]::view-transition-new\(root\)\s*\{[^}]*animation:\s*theme-wave-reveal 620ms/s);
  assert.match(css, /from\s*\{\s*clip-path:\s*circle\(0px at/);
  assert.match(css, /to\s*\{\s*clip-path:\s*circle\(var\(--theme-wave-radius\)/);
  assert.match(css, /prefers-reduced-motion:\s*reduce[\s\S]*animation:\s*none !important/);
});
