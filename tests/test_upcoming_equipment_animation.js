const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const css = fs.readFileSync(path.join(root, 'app/static/navigation.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
const reducedStart = css.indexOf('@media (prefers-reduced-motion: reduce)');
const normalCss = css.slice(0, reducedStart);
const reducedCss = css.slice(reducedStart);
const rules = text => [...text.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map(match => ({
  selectors: match[1].trim().split(',').map(selector => selector.trim()), body: match[2],
}));

test('equipment badge uses the same gentle animation as the blocking card', () => {
  const rule = rules(normalCss).find(rule => rule.selectors.includes('.upcoming-equipment.present')
    && /\banimation\s*:/.test(rule.body));
  assert.ok(rule);
  assert.ok(rule.selectors.includes('.blocking-equipment-card.has-equipment'));
  assert.match(rule.body, /animation:\s*blocking-equipment-attention 2\.2s ease-in-out infinite/);
  assert.match(normalCss, /@keyframes blocking-equipment-attention/);
});

test('empty, unverified and error badges remain static', () => {
  for (const state of ['empty', 'unknown', 'error']) {
    const animated = rules(normalCss).some(rule => /\banimation\s*:/.test(rule.body)
      && (rule.selectors.includes(`.upcoming-equipment.${state}`) || rule.selectors.includes('.upcoming-equipment')));
    assert.equal(animated, false, state);
  }
});

test('reduced-motion preference disables badge animation', () => {
  assert.notEqual(reducedStart, -1);
  const rule = rules(reducedCss).find(rule => rule.selectors.includes('.upcoming-equipment.present'));
  assert.ok(rule);
  assert.match(rule.body, /animation:\s*none\s*!important/);
});

test('stylesheet URL is refreshed so browsers receive the new animation', () => {
  const base = fs.readFileSync(path.join(root, 'app/templates/base.html'), 'utf8');
  assert.match(base, /navigation\.css\?v=equipment-pulse-20261008/);
});
