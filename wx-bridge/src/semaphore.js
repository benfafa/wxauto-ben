export class Semaphore {
  constructor(limit) {
    const n = Number(limit);
    this.limit = Number.isFinite(n) ? Math.max(1, Math.floor(n)) : 1;
    this.active = 0;
    this.queue = [];
  }

  async acquire() {
    if (this.active < this.limit) {
      this.active += 1;
      return;
    }
    await new Promise((resolve) => this.queue.push(resolve));
  }

  release() {
    const next = this.queue.shift();
    if (next) next();
    else this.active = Math.max(0, this.active - 1);
  }
}
