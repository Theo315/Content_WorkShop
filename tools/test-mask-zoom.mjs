import test from 'node:test';
import assert from 'node:assert/strict';
import {
  wheelZoom, clampZoom, fitImageScale, anchoredScroll, bindWheelZoom,
  MIN_MASK_ZOOM, MAX_MASK_ZOOM,
} from '../frontend/src/mask-zoom.ts';

test('wheel up zooms in, wheel down zooms out, repeated gestures stay bounded', () => {
  assert.ok(wheelZoom(1, -120) > 1);
  assert.ok(wheelZoom(1, 120) < 1);
  let zoom = 1;
  for (let i = 0; i < 100; i++) zoom = wheelZoom(zoom, -120);
  assert.equal(zoom, MAX_MASK_ZOOM);
  for (let i = 0; i < 100; i++) zoom = wheelZoom(zoom, 120);
  assert.equal(zoom, MIN_MASK_ZOOM);
  assert.equal(clampZoom(100), MAX_MASK_ZOOM);
  assert.equal(clampZoom(0), MIN_MASK_ZOOM);
});

test('trackpad increments are smooth and delta modes are normalized', () => {
  assert.ok(wheelZoom(1, -1) > 1 && wheelZoom(1, -1) < 1.01);
  assert.equal(wheelZoom(1, -3, 1), wheelZoom(1, -48, 0));
  assert.equal(wheelZoom(1, -0.1, 2, 600), wheelZoom(1, -60, 0));
  assert.equal(wheelZoom(1, -100000), wheelZoom(1, -240));
  for (const delta of [0, NaN, Infinity, -Infinity]) assert.equal(wheelZoom(1, delta), 1);
});

test('in and out preserve the same image point under an off-center cursor', () => {
  for (const factor of [0.25, 0.9, 1.2, 3, 16]) {
    const before = {left: -230, top: -170, width: 900, height: 640};
    const after = {left: 40, top: 80, width: 900 * factor, height: 640 * factor};
    const pointer = {x: 351, y: 217};
    const scroll = {left: 284, top: 123};
    const next = anchoredScroll(before, after, pointer.x, pointer.y, scroll.left, scroll.top);
    const renderedX = after.left + (pointer.x - before.left) / before.width * after.width - (next.left - scroll.left);
    const renderedY = after.top + (pointer.y - before.top) / before.height * after.height - (next.top - scroll.top);
    assert.ok(Math.abs(renderedX - pointer.x) < 1e-8);
    assert.ok(Math.abs(renderedY - pointer.y) < 1e-8);
  }
});

test('padding gestures use the nearest image edge without invalid coordinates', () => {
  const frame = {left: 100, top: 120, width: 500, height: 300};
  const after = {...frame, width: 1000, height: 600};
  assert.deepEqual(anchoredScroll(frame, after, 0, 0, 0, 0), {left: 0, top: 0});
  assert.deepEqual(anchoredScroll(frame, after, 1000, 1000, 0, 0), {left: 500, top: 300});
});

test('fit mode fits tall and wide images on both axes, leaving scrollbar space', () => {
  for (const [width, height, vw, vh] of [[2048, 2048, 1082, 440], [4000, 800, 700, 440], [800, 4000, 360, 400]]) {
    const scale = fitImageScale(width, height, vw, vh);
    assert.ok(width * scale + 24 <= vw - 16 + 1e-8);
    assert.ok(height * scale + 24 <= vh - 16 + 1e-8);
  }
});

function surface() {
  let listener;
  return {
    clientHeight: 600,
    addEventListener(type, value, options) {
      assert.equal(type, 'wheel'); assert.equal(options.passive, false); listener = value;
    },
    removeEventListener(type, value) {
      assert.equal(type, 'wheel'); assert.equal(value, listener); listener = undefined;
    },
    fire(deltaY) {
      let prevented = false;
      listener?.({deltaY, deltaMode: 0, clientX: 321, clientY: 187,
        preventDefault() { prevented = true; }});
      return prevented;
    },
  };
}

test('native wheel listener suppresses page scrolling, passes cursor and cleans up', () => {
  const target = surface();
  let zoom = 1;
  const cleanup = bindWheelZoom(target, () => zoom, (next, event) => {
    zoom = next;
    assert.equal(event.clientX, 321); assert.equal(event.clientY, 187);
  });
  assert.equal(target.fire(-120), true);
  assert.ok(zoom > 1);
  assert.equal(target.fire(120), true);
  assert.ok(Math.abs(zoom - 1) < 0.0002);
  assert.equal(target.fire(0), false);
  cleanup();
  assert.equal(target.fire(-120), false);
});

test('zoom is blocked during a brush stroke and at limits without page scrolling', () => {
  const target = surface();
  const cleanup = bindWheelZoom(target, () => 1, () => assert.fail('must not change mid-stroke'), () => true);
  assert.equal(target.fire(-120), true);
  cleanup();
  const cleanupMax = bindWheelZoom(target, () => MAX_MASK_ZOOM, () => assert.fail('must not render at limit'));
  assert.equal(target.fire(-120), true);
  cleanupMax();
});
