const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');

function element() {
    return {
        listeners: {}, classList: {add() {}, remove() {}, toggle() {}},
        addEventListener(type, listener) { this.listeners[type] = listener; },
        append() {}, appendChild() {}, replaceChildren() {}, setAttribute() {},
        scrollIntoView() {}, click() {},
    };
}

const selectors = new Map();
for (const selector of [
    '[data-image-attachments]', '[data-image-picker]', '[data-image-dropzone]',
    '[data-image-list]', '[data-image-count]', '[data-image-status]', '[data-image-menu]',
    '[data-image-select]', '[data-image-paste]', '[data-image-open]', '[name="csrf_token"]',
]) selectors.set(selector, element());
selectors.get('[data-image-attachments]').querySelector = selector => selectors.get(selector);
selectors.get('[name="csrf_token"]').value = 'csrf';
const picker = selectors.get('[data-image-picker]');
picker.files = [];
Object.defineProperty(picker, 'value', {set(value) {if (value === '') this.files = [];}});

let inFlight = 0;
let maxInFlight = 0;
let failedOnce = false;
let failedAt = 0;
let retryElapsed = 0;
const attempts = new Map();
const chunkUrls = [];
const completions = [];
const sandbox = {
    window: {}, document: {createElement: element}, crypto: webcrypto, AbortController, setTimeout, clearTimeout,
    URL: {createObjectURL: () => 'blob:video', revokeObjectURL() {}},
    fetch: async url => {
        if (url === '/api/media/status') return {ok: true, json: async () => ({configured: true})};
        if (url.endsWith('/complete')) {
            completions.push(url);
            return {ok: true, json: async () => ({success: true})};
        }
        if (url.includes('/chunks/')) {
            chunkUrls.push(url);
            const key = url.slice(0, url.indexOf('?'));
            attempts.set(key, (attempts.get(key) || 0) + 1);
            inFlight++;
            maxInFlight = Math.max(maxInFlight, inFlight);
            await new Promise(resolve => setTimeout(resolve, 3));
            inFlight--;
            if (!failedOnce && url.includes('/chunks/1?')) {
                failedOnce = true;
                failedAt = Date.now();
                return {ok: false, status: 429, headers: {get: () => '2'},
                    json: async () => ({message: '다시 시도', retry_after: 0.25})};
            }
            if (url.includes('/chunks/1?') && attempts.get(key) === 2) retryElapsed = Date.now() - failedAt;
            return {ok: true, json: async () => ({success: true,
                uploaded_chunks: url.includes('chunk_offset=20971520') ? [0] : []})};
        }
        return {ok: true, json: async () => ({success: true})};
    },
    Event: class {constructor(type) {this.type = type;}},
};
for (const file of ['media-upload-queue.js', 'media-sha256.js', 'image-attachments.js']) {
    vm.runInNewContext(fs.readFileSync('api/static/js/' + file, 'utf8'), sandbox);
}

const uploader = sandbox.window.DicoImageAttachments.mount({
    querySelector: selector => selectors.get(selector), addEventListener() {},
}, {kind: 'posts', ensureDraft: async () => 17});

const fileSize = 21 * 1024 * 1024;
const video = name => ({
    name, size: fileSize, type: 'video/mp4',
    slice(start, end) {return {size: end - start, arrayBuffer: async () => new ArrayBuffer(end - start)};},
});
picker.files = [video('one.mp4'), video('two.mp4')];
picker.listeners.change();

(async () => {
    await uploader.ready();
    assert.equal(uploader.hasFiles(), true);
    assert.equal(maxInFlight, 1);
    assert.equal(completions.length, 2);
    assert(chunkUrls.every(url => url.includes('count=1&chunk_size=52428800')));

    await uploader.uploadAll('posts', 17, 'csrf');
    assert.equal(completions.length, 2);
    const repeated = Array.from(attempts.values()).filter(count => count > 1);
    assert.deepEqual(repeated, [2]);
    assert(retryElapsed >= 1900, '429 Retry-After header should be observed');
    const previousRequests = attempts.size;
    picker.files = [{...video('too-large.mp4'), size: 10 * 1024 ** 3 + 1}];
    picker.listeners.change();
    await uploader.ready();
    assert.equal(attempts.size, previousRequests);
    picker.files = [video('canceled.mp4')];
    picker.listeners.change();
    uploader.stop();
    await uploader.ready();
    assert.equal(attempts.size, previousRequests);
    console.log('Two 21 MiB videos use one upload slot, 50 MiB logical chunks, and retry a failed request.');
})().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
