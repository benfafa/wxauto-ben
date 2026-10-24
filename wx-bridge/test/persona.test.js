import test from 'node:test';
import assert from 'node:assert/strict';
import { buildPrompt, personaFor } from '../src/bridge.js';
import { buildProactivePrompt } from '../src/proactive.js';

test('消息里带「喵」切猫娘，否则默认损友', () => {
  assert.equal(personaFor('今天好累喵'), '猫娘');
  assert.equal(personaFor('喵喵喵'), '猫娘');
  assert.equal(personaFor('今天好累'), '损友');
  assert.equal(personaFor(''), '损友');
  assert.equal(personaFor(null), '损友');
});

test('提示词把本轮人格写在开头，两种语气都指得到', () => {
  const base = { group: '群A', sender: '张三', text: '在吗', context: '（无）', memory: '（无）' };
  const tough = buildPrompt({ ...base, persona: '损友' });
  assert.match(tough, /【本轮人格】损友/);
  assert.doesNotMatch(tough, /【本轮人格】猫娘/);
  const cat = buildPrompt({ ...base, persona: '猫娘' });
  assert.match(cat, /【本轮人格】猫娘/);
  assert.match(cat, /喵/);
  // 默认值也是损友，不会因为漏传 persona 就变成猫娘
  assert.match(buildPrompt(base), /【本轮人格】损友/);
});

test('主动插话固定轻损，不开骂', () => {
  const prompt = buildProactivePrompt({ group: '群A', context: '闲聊', memory: '（无）' });
  assert.match(prompt, /【本轮人格】损友/);
  assert.match(prompt, /只轻损一句/);
});
