import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { GroupMemory } from '../src/memory.js';

test('persists per-group messages and retrieves related memory', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'wx-bridge-memory-'));
  try {
    const first = new GroupMemory({ stateDir: dir, maxResults: 3 });
    first.add({ group: '群A', id: 'm1', sender: '小明', text: '周六下午三点在咖啡店集合' });
    first.add({ group: '群B', id: 'm2', sender: '小红', text: '周六下午三点开会' });
    assert.match(first.search('群A', '咖啡店几点集合'), /小明：周六下午三点在咖啡店集合/);
    assert.equal(first.search('群A', '开会'), '（无相关长期记忆）');

    const second = new GroupMemory({ stateDir: dir, maxResults: 3 });
    assert.match(second.search('群A', '咖啡店'), /咖啡店/);
    assert.equal(second.search('群C', '咖啡店'), '（无）');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
