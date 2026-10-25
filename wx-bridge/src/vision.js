export const VISION_DEFAULTS = Object.freeze({ enabled: false, contextWindowMs: 120000, maxImages: 2, maxBytes: 5242880 });

export function normalizeVision(input = {}) {
  if (!input || typeof input !== 'object' || Array.isArray(input)
      || Object.keys(input).some((key) => !Object.hasOwn(VISION_DEFAULTS, key))) throw new Error('vision 配置字段非法');
  const cfg = { ...VISION_DEFAULTS, ...input };
  if (typeof cfg.enabled !== 'boolean') throw new Error('vision.enabled 必须是布尔值');
  for (const [key, min, max] of [['contextWindowMs', 1000, 600000], ['maxImages', 1, 4], ['maxBytes', 1024, 5242880]]) {
    if (!Number.isSafeInteger(cfg[key]) || cfg[key] < min || cfg[key] > max) throw new Error(`vision.${key} 必须是 ${min}~${max} 的整数`);
  }
  return cfg;
}

export function validateImages(images = [], config) {
  const cfg = normalizeVision(config);
  if (!Array.isArray(images) || images.length > cfg.maxImages || (!cfg.enabled && images.length)) throw new Error('vision 图片数量或开关非法');
  return images.map((image) => {
    if (!image || typeof image !== 'object' || Array.isArray(image)
        || Object.keys(image).some((key) => !['mediaType', 'data', 'name'].includes(key))
        || !['image/png', 'image/jpeg', 'image/webp', 'image/gif'].includes(image.mediaType)
        || typeof image.name !== 'string' || !/^[A-Za-z0-9_-]{1,64}\.(png|jpg|jpeg|webp|gif)$/.test(image.name)
        || typeof image.data !== 'string' || !image.data.length
        || image.data.length > Math.ceil(cfg.maxBytes / 3) * 4
        || image.data.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(image.data)) throw new Error('vision 图片 schema 非法');
    const bytes = Buffer.from(image.data, 'base64');
    if (!bytes.length || bytes.length > cfg.maxBytes || bytes.toString('base64') !== image.data) throw new Error('vision 图片字节数非法');
    const magic = { 'image/png': bytes.subarray(0, 8).equals(Buffer.from('89504e470d0a1a0a', 'hex')),
      'image/jpeg': bytes.subarray(0, 3).equals(Buffer.from('ffd8ff', 'hex')),
      'image/gif': ['GIF87a', 'GIF89a'].includes(bytes.subarray(0, 6).toString()),
      'image/webp': bytes.subarray(0, 4).toString() === 'RIFF' && bytes.subarray(8, 12).toString() === 'WEBP' };
    if (!magic[image.mediaType]) throw new Error('vision 图片 MIME 与 magic 不符');
    return { type: 'image', mediaType: image.mediaType, data: image.data, name: image.name };
  });
}

// Metadata only; snapshot candidates at enqueue time, never at agent execution time.
export class RecentImages {
  constructor(config) { this.cfg = normalizeVision(config); this.events = []; this.seq = 0; }
  clear() { this.events = []; }
  add(event, now = Date.now()) {
    if (!this.cfg.enabled) return;
    this.events = this.events.filter((item) => now - item.observedAt <= this.cfg.contextWindowMs);
    if (event.kind !== 'image' || !event.group || !event.sender || event.sender === '未知') return;
    this.seq += 1;
    this.events.push({ group: event.group, sender: event.sender, imageId: event.imageId || null,
      timestamp: Number(event.timestamp) > 0 ? Number(event.timestamp) * 1000 : now, observedAt: now, seq: this.seq });
    this.events = this.events.slice(-500);
    return this.seq;
  }
  // 梗常常是「甲发图、乙 @ 机器人吐槽」，所以取全群窗口内的图，只把触发者的图排在前面。
  candidates(group, sender, at, now = Date.now()) {
    if (!this.cfg.enabled) return [];
    const inWindow = this.events.filter((item) => item.group === group
      && item.timestamp <= at && at - item.timestamp <= this.cfg.contextWindowMs
      && now - item.observedAt <= this.cfg.contextWindowMs);
    const byNewest = (left, right) => right.timestamp - left.timestamp || right.seq - left.seq;
    const mine = inWindow.filter((item) => item.sender === sender).sort(byNewest);
    const others = inWindow.filter((item) => item.sender !== sender).sort(byNewest);
    return [...mine, ...others]
      .slice(0, this.cfg.maxImages)
      .sort((left, right) => left.timestamp - right.timestamp || left.seq - right.seq)
      .map((item) => ({ ...item }));
  }
}
